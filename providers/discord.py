"""Discord provider: post to server channels through the platform bot.

The user installs the platform's bot into a server with OAuth2 (``bot`` +
``guilds`` scopes) and picks which text channels become accounts. Every post is
then made by the bot, authenticated with ``PLATFORM_DISCORD_BOT_TOKEN``; the
user's own access token is only used to learn which server the bot joined and
is revoked straight away. The account's stored "token" is the channel id, which
is not a secret.

API reference (Discord API v10, checked against the current docs):

- OAuth2 bot flow, token response with the ``guild`` object, token revocation:
  https://docs.discord.com/developers/topics/oauth2
- Create Message (``content`` up to 2000 characters, ``message_reference``,
  ``flags``, ``allowed_mentions``, multipart ``files[n]`` with ``payload_json``,
  25 MiB request size): https://docs.discord.com/developers/resources/message#create-message
- Delete Message: https://docs.discord.com/developers/resources/message#delete-message
- Guild channels: https://docs.discord.com/developers/resources/guild#get-guild-channels
- Rate limits (429 body ``retry_after``, ``Retry-After``) and the User-Agent
  requirement: https://docs.discord.com/developers/topics/rate-limits
- Permission bits: https://docs.discord.com/developers/topics/permissions
"""

from __future__ import annotations

import json
import logging
import math
from contextlib import ExitStack
from dataclasses import replace
from urllib.parse import urlencode

from django.conf import settings

from .base import SocialProvider
from .exceptions import APIError, OAuthError, PublishError, RateLimitError
from .types import (
    AccountProfile,
    AuthType,
    CommentResult,
    MediaRules,
    MediaType,
    OAuthTokens,
    PostSetting,
    PostType,
    PublishContent,
    PublishResult,
    RateLimitConfig,
    ThreadCapability,
    ThreadMode,
)

logger = logging.getLogger(__name__)

API_BASE = "https://discord.com/api/v10"
AUTH_URL = "https://discord.com/oauth2/authorize"
TOKEN_URL = "https://discord.com/api/oauth2/token"
REVOKE_URL = "https://discord.com/api/oauth2/token/revoke"
CDN_BASE = "https://cdn.discordapp.com"

MAX_CONTENT_LENGTH = 2000
MAX_ATTACHMENTS = 10

# View Channel (1<<10) + Send Messages (1<<11) + Embed Links (1<<14) +
# Attach Files (1<<15) + Read Message History (1<<16, needed to reply).
BOT_PERMISSIONS = (1 << 10) | (1 << 11) | (1 << 14) | (1 << 15) | (1 << 16)

# Channel types the bot posts into: guild text (0) and announcement (5).
# https://docs.discord.com/developers/resources/channel#channel-object-channel-types
POSTABLE_CHANNEL_TYPES = (0, 5)

# Message flags: https://docs.discord.com/developers/resources/message#message-object-message-flags
FLAG_SUPPRESS_EMBEDS = 1 << 2
FLAG_SUPPRESS_NOTIFICATIONS = 1 << 12

# The documented request-size ceiling. A server's own upload limit can be lower
# or higher with boosts, so a file size only warns; Discord has the final say.
_ATTACHMENT_BYTES = 25 * 1024 * 1024
_DISCORD_MEDIA = MediaRules(
    source="https://docs.discord.com/developers/resources/message#create-message",
    max_items=MAX_ATTACHMENTS,
    media_types=frozenset({"image", "video", "gif"}),
    max_image_bytes=_ATTACHMENT_BYTES,
    max_video_bytes=_ATTACHMENT_BYTES,
    advisory=frozenset({"max_image_bytes", "max_video_bytes"}),
)

# Discord's JSON error codes that no retry can fix, mapped to what to tell the
# user. https://docs.discord.com/developers/topics/opcodes-and-status-codes#json-json-error-codes
_PERMANENT_CODES = {
    10003: "Discord can't find this channel any more. It may have been deleted. Reconnect to pick another.",
    10008: "The message this post should reply to no longer exists.",
    40005: "The attachments are too large for this Discord server.",
    50001: "The bot can't see this channel. Give it View Channel there, or reconnect to pick another channel.",
    50013: "The bot can't post in this channel. It needs View Channel, Send Messages, Attach Files and Embed Links.",
    50035: "Discord rejected this post's content.",
}


class DiscordProvider(SocialProvider):
    """Discord provider: OAuth2 bot install, then bot-token posting to channels."""

    media_rules = {
        PostType.IMAGE: _DISCORD_MEDIA,
        PostType.VIDEO: _DISCORD_MEDIA,
    }

    writing_style = (
        "Casual and community-first, like a post to friends in a server. Markdown works: bold, lists, short "
        "headers and links. Lead with the news, use emoji freely and skip hashtags."
    )

    # Each part is a message replying to the one before it (``message_reference``).
    thread = ThreadCapability(mode=ThreadMode.REPLY_CHAIN, max_parts=25, media_per_part=True)

    has_destination_picker = True
    supports_connection_link = False

    post_settings = (
        PostSetting(
            key="suppress_embeds",
            label="Hide link previews",
            kind="checkbox",
            help="Links in the message won't expand into embeds.",
        ),
        PostSetting(
            key="silent",
            label="Send silently",
            kind="checkbox",
            help="Members aren't notified of the message.",
        ),
    )

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    @property
    def platform_name(self) -> str:
        return "Discord"

    @property
    def auth_type(self) -> AuthType:
        return AuthType.OAUTH2

    @property
    def max_caption_length(self) -> int:
        return MAX_CONTENT_LENGTH

    @property
    def supported_post_types(self) -> list[PostType]:
        return [PostType.TEXT, PostType.IMAGE, PostType.VIDEO]

    @property
    def supported_media_types(self) -> list[MediaType]:
        return [MediaType.JPEG, MediaType.PNG, MediaType.GIF, MediaType.WEBP, MediaType.MP4, MediaType.MOV]

    @property
    def required_scopes(self) -> list[str]:
        return ["bot", "guilds"]

    @property
    def rate_limits(self) -> RateLimitConfig:
        # Per-route buckets, not a daily quota; the global cap is 50 requests a second.
        return RateLimitConfig(requests_per_hour=3000, requests_per_day=50000, publish_per_day=1000)

    @classmethod
    def credentials_for_account(cls, account) -> dict:
        # The server id is kept with the channel when it is connected.
        return {"guild_id": account.webhook_target_id}

    # ------------------------------------------------------------------
    # Requests
    # ------------------------------------------------------------------

    def _bot_headers(self) -> dict:
        token = self.credentials.get("bot_token", "")
        if not token:
            raise PublishError(
                "Discord isn't configured: the platform bot token is missing.",
                platform=self.platform_name,
                retryable=False,
            )
        return {
            "Authorization": f"Bot {token}",
            # Required of every client: "DiscordBot ($url, $versionNumber)".
            "User-Agent": f"DiscordBot ({settings.BRAND_WEBSITE_URL}, 1.0)",
        }

    def _error_for_response(self, response):
        """Discord errors are ``{"message", "code"}``; a 429 adds ``retry_after`` seconds."""
        body = self._safe_json(response)
        body = body if isinstance(body, dict) else {}
        message = str(body.get("message", ""))[:300]
        status = response.status_code
        if status == 429:
            retry_after = body.get("retry_after")
            header = response.headers.get("Retry-After")
            wait = retry_after if retry_after is not None else header
            return RateLimitError(
                f"Rate limit exceeded for {self.platform_name}: {message}",
                retry_after=math.ceil(float(wait)) if wait is not None else None,
                platform=self.platform_name,
                raw_response=body,
            )
        return APIError(
            f"{self.platform_name} API error {status}: {message}",
            status_code=status,
            platform=self.platform_name,
            raw_response=body,
            # A 4xx is the request itself refused; only a server failure is worth another try.
            retryable=not (400 <= status < 500),
        )

    def _bot_request(self, method: str, path: str, **kwargs):
        return self._request(method, f"{API_BASE}{path}", headers=self._bot_headers(), **kwargs)

    def _publish_call(self, method: str, path: str, **kwargs):
        """A write to Discord, turning refusals we can explain into a sentence for the user."""
        try:
            return self._bot_request(method, path, **kwargs)
        except APIError as exc:
            reason = _PERMANENT_CODES.get(exc.raw_response.get("code"))
            if reason:
                raise PublishError(
                    reason, platform=self.platform_name, raw_response=exc.raw_response, retryable=False
                ) from exc
            raise

    # ------------------------------------------------------------------
    # OAuth
    # ------------------------------------------------------------------

    def get_auth_url(self, redirect_uri: str, state: str, code_verifier: str | None = None) -> str:
        params = {
            "client_id": self.credentials["client_id"],
            "scope": " ".join(self.required_scopes),
            "permissions": BOT_PERMISSIONS,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "state": state,
        }
        return f"{AUTH_URL}?{urlencode(params)}"

    def exchange_code(self, code: str, redirect_uri: str, code_verifier: str | None = None) -> OAuthTokens:
        resp = self._request(
            "POST",
            TOKEN_URL,
            data={
                "client_id": self.credentials["client_id"],
                "client_secret": self.credentials["client_secret"],
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
            },
        )
        data = resp.json()
        if "access_token" not in data:
            raise OAuthError("Discord token exchange failed", platform=self.platform_name, raw_response=data)
        return OAuthTokens(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_in=data.get("expires_in"),
            scope=data.get("scope"),
            raw_response=data,
        )

    def _revoke_user_token(self, access_token: str) -> bool:
        """Revoke the user's OAuth token (not the bot, which stays in the server).

        Not ``revoke_token``: an account's stored credential is its channel id, so a
        disconnect has no user token to hand it.
        """
        try:
            self._request(
                "POST",
                REVOKE_URL,
                data={
                    "client_id": self.credentials["client_id"],
                    "client_secret": self.credentials["client_secret"],
                    "token": access_token,
                    "token_type_hint": "access_token",
                },
            )
            return True
        except Exception:
            logger.warning("Failed to revoke Discord user token", exc_info=True)
            return False

    def list_destinations(self, tokens: OAuthTokens) -> list[dict]:
        """The text channels of the server the bot was just installed into."""
        guild = (tokens.raw_response or {}).get("guild") or {}
        if not guild.get("id"):
            raise OAuthError(
                "Discord didn't say which server the bot joined. Try connecting again.",
                platform=self.platform_name,
                raw_response=tokens.raw_response,
            )
        channels = self._bot_request("GET", f"/guilds/{guild['id']}/channels").json()
        icon = guild.get("icon")
        picture = f"{CDN_BASE}/icons/{guild['id']}/{icon}.png" if icon else ""
        rows = [
            {
                "id": channel["id"],
                "name": f"#{channel['name']}",
                "handle": guild.get("name", ""),
                "category": guild.get("name", ""),
                "picture": picture,
                # The bot posts under the account's channel id; see get_profile.
                "access_token": channel["id"],
                "page_id": guild["id"],
            }
            for channel in sorted(channels, key=lambda c: c.get("position", 0))
            if channel.get("type") in POSTABLE_CHANNEL_TYPES
        ]
        # The user's own grant has done its job; the bot needs nothing from it.
        self._revoke_user_token(tokens.access_token)
        return rows

    # ------------------------------------------------------------------
    # Profile
    # ------------------------------------------------------------------

    def get_profile(self, access_token: str) -> AccountProfile:
        """Check the bot can still see the channel (``access_token`` is the channel id)."""
        channel = self._bot_request("GET", f"/channels/{access_token}").json()
        guild_name, picture = "", None
        guild_id = channel.get("guild_id")
        if guild_id:
            try:
                guild = self._bot_request("GET", f"/guilds/{guild_id}").json()
                guild_name = guild.get("name", "")
                if guild.get("icon"):
                    picture = f"{CDN_BASE}/icons/{guild_id}/{guild['icon']}.png"
            except Exception:
                logger.info("Discord server name unavailable for guild %s", guild_id)
        return AccountProfile(
            platform_id=str(channel["id"]),
            name=f"#{channel.get('name', channel['id'])}",
            handle=guild_name or None,
            avatar_url=picture,
            extra={"guild_id": guild_id or ""},
        )

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    def publish_post(self, access_token: str, content: PublishContent) -> PublishResult:
        """Send a message (with attachments) to the channel."""
        text = content.text or ""
        files = list(content.media_files or [])
        if not text.strip() and not files:
            raise PublishError(
                "Discord needs some text or a file to post.", platform=self.platform_name, retryable=False
            )
        if len(text) > MAX_CONTENT_LENGTH:
            raise PublishError(
                f"Discord messages are limited to {MAX_CONTENT_LENGTH} characters.",
                platform=self.platform_name,
                retryable=False,
            )
        if len(files) > MAX_ATTACHMENTS:
            raise PublishError(
                f"Discord takes at most {MAX_ATTACHMENTS} files in one message.",
                platform=self.platform_name,
                retryable=False,
            )

        payload: dict = {
            "content": text,
            # Never ping @everyone or @here from a scheduled post.
            "allowed_mentions": {"parse": ["users", "roles"], "replied_user": False},
        }
        flags = 0
        if content.extra.get("suppress_embeds"):
            flags |= FLAG_SUPPRESS_EMBEDS
        if content.extra.get("silent"):
            flags |= FLAG_SUPPRESS_NOTIFICATIONS
        if flags:
            payload["flags"] = flags
        reply_to = content.extra.get("reply_to_message_id")
        if reply_to:
            payload["message_reference"] = {"message_id": str(reply_to), "fail_if_not_exists": False}

        path = f"/channels/{access_token}/messages"
        if not files:
            message = self._publish_call("POST", path, json=payload).json()
        else:
            with ExitStack() as stack:
                upload = {}
                payload["attachments"] = []
                for index, local_path in enumerate(files):
                    name = local_path.rsplit("/", 1)[-1]
                    upload[f"files[{index}]"] = (name, stack.enter_context(open(local_path, "rb")))
                    payload["attachments"].append({"id": index, "filename": name})
                message = self._publish_call(
                    "POST", path, data={"payload_json": json.dumps(payload)}, files=upload
                ).json()

        message_id = str(message["id"])
        guild_id = self.credentials.get("guild_id")
        return PublishResult(
            platform_post_id=message_id,
            url=f"https://discord.com/channels/{guild_id}/{access_token}/{message_id}" if guild_id else None,
            extra={"channel_id": str(access_token), "guild_id": guild_id or ""},
        )

    def publish_thread_part(
        self,
        access_token: str,
        content: PublishContent,
        *,
        root: PublishResult,
        parent: PublishResult,
    ) -> PublishResult:
        """Publish a thread part as a message replying to the part before it."""
        return self.publish_post(
            access_token,
            replace(content, extra={**content.extra, "reply_to_message_id": parent.platform_post_id}),
        )

    def publish_comment(self, access_token: str, post_id: str, text: str) -> CommentResult:
        """Reply to a message (the first comment)."""
        message = self._publish_call(
            "POST",
            f"/channels/{access_token}/messages",
            json={
                "content": text,
                "allowed_mentions": {"parse": ["users", "roles"], "replied_user": False},
                "message_reference": {"message_id": str(post_id), "fail_if_not_exists": False},
            },
        ).json()
        return CommentResult(platform_comment_id=str(message["id"]), extra={"channel_id": str(access_token)})

    def delete_post(self, access_token: str, post_id: str) -> bool:
        """Delete a message the bot sent (204 on success)."""
        response = self._bot_request("DELETE", f"/channels/{access_token}/messages/{post_id}")
        return response.status_code == 204
