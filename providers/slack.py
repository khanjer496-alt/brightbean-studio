"""Slack provider: post to workspace channels as the platform's Slack app.

The user installs the app into a workspace with OAuth v2 and picks the channels
that become accounts. Every channel account of a workspace shares that
workspace's bot token (``xoxb-``), which is why disconnecting one channel never
revokes the token, and why token rotation has to stay off: a rotated refresh
token would break the sibling channels that stored the same one.

A message is posted with ``chat.postMessage``. Files can't ride on it, so media
goes through the current external-upload flow (``files.getUploadURLExternal``,
a raw POST of the bytes, ``files.completeUploadExternal``; ``files.upload`` is
retired) into the thread of that message.

API reference (checked against the current docs):

- OAuth v2 install and ``oauth.v2.access``:
  https://docs.slack.dev/authentication/installing-with-oauth
  https://docs.slack.dev/reference/methods/oauth.v2.access
- ``chat.postMessage`` (``thread_ts``, ``mrkdwn``, error codes, ~1 message a
  second per channel, ``Retry-After``):
  https://docs.slack.dev/reference/methods/chat.postMessage
- Uploading files: https://docs.slack.dev/messaging/working-with-files
  https://docs.slack.dev/reference/methods/files.getUploadURLExternal
  https://docs.slack.dev/reference/methods/files.completeUploadExternal
- ``conversations.list``: https://docs.slack.dev/reference/methods/conversations.list
- ``auth.revoke`` (revokes one token, not the install):
  https://docs.slack.dev/reference/methods/auth.revoke
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import replace
from urllib.parse import urlencode, urlparse

from .base import SocialProvider
from .exceptions import APIError, OAuthError, PublishError, RateLimitError, TokenExpiredError
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

API_BASE = "https://slack.com/api"
AUTH_URL = "https://slack.com/oauth/v2/authorize"

# Slack truncates a message above 40,000 characters and recommends keeping
# ``text`` under 4,000.
MAX_TEXT_LENGTH = 4000

SCOPES = ["chat:write", "files:write", "channels:read", "groups:read"]

# The API docs state no file count or size limit, so only the kinds of file are declared.
_SLACK_MEDIA = MediaRules(
    source="https://docs.slack.dev/messaging/working-with-files",
    media_types=frozenset({"image", "video", "gif", "document"}),
)

# Slack tokens a message may keep: user, channel and special mentions and
# links. Everything else has &, < and > escaped, as the formatting docs ask.
_SLACK_TOKEN = re.compile(r"<(?:[@#!][^<>\s|]+|https?://[^<>\s|]+|mailto:[^<>\s|]+)(?:\|[^<>]*)?>")

_AUTH_ERRORS = {"invalid_auth", "not_authed", "token_revoked", "token_expired", "account_inactive"}
_RETRYABLE_ERRORS = {"internal_error", "fatal_error", "service_unavailable", "request_timeout"}
# Slack's error strings that no retry can fix, mapped to what to tell the user.
_PERMANENT_REASONS = {
    "channel_not_found": "Slack can't find this channel any more. Reconnect to pick another.",
    "not_in_channel": "The app isn't in this channel. Invite it with /invite, then publish again.",
    "is_archived": "This Slack channel is archived.",
    "missing_scope": "The Slack app is missing a permission it needs. Reconnect Slack to grant it.",
    "no_permission": "The app isn't allowed to do that in this workspace.",
    "restricted_action": "This channel's settings stop the app from posting.",
    "restricted_action_read_only_channel": "This channel is read-only for the app.",
    "restricted_action_thread_locked": "This thread is locked.",
    "restricted_action_thread_only_channel": "This channel only allows replies in threads.",
    "restricted_action_non_threadable_channel": "This channel doesn't allow threads.",
    "cannot_reply_to_message": "Slack won't let the app reply to that message.",
    "no_text": "Slack needs some text to post.",
    "msg_blocks_too_long": "This message is too long for Slack.",
    "file_uploads_disabled": "File uploads are turned off in this workspace.",
    "file_upload_size_restricted": "A file is larger than this workspace allows.",
    "file_type_not_allowed": "This workspace doesn't allow that file type.",
    "storage_limit_reached": "This workspace's file storage is full.",
    "user_is_external_guest": "The app can't upload files as an external guest.",
}


def to_mrkdwn(text: str) -> str:
    """Escape ``&``, ``<`` and ``>`` in ``text`` except inside Slack's own ``<...>`` tokens."""
    parts, position = [], 0
    for match in _SLACK_TOKEN.finditer(text):
        parts.append(_escape(text[position : match.start()]))
        parts.append(match.group(0))
        position = match.end()
    parts.append(_escape(text[position:]))
    return "".join(parts)


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class SlackProvider(SocialProvider):
    """Slack provider: OAuth v2 install, bot-token posting to channels."""

    media_rules = {PostType.IMAGE: _SLACK_MEDIA, PostType.VIDEO: _SLACK_MEDIA}

    writing_style = (
        "Workplace-friendly and scannable: a bold headline, short paragraphs or bullets, and links. "
        "Slack mrkdwn applies (*bold*, _italic_, `code`); emoji are welcome, hashtags are not."
    )

    # Later parts are replies in the thread of the first message (Slack threads are flat).
    thread = ThreadCapability(mode=ThreadMode.REPLY_CHAIN, max_parts=25, media_per_part=True)

    has_destination_picker = True
    supports_connection_link = False
    no_destinations_hint = (
        "Slack only lists channels the app has been added to: open the channel, type /invite and pick the app, "
        "then connect again."
    )

    post_settings = (
        PostSetting(
            key="no_unfurl",
            label="Don't expand links",
            kind="checkbox",
            help="Links stay as plain links instead of unfurling into previews.",
        ),
    )

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    @property
    def platform_name(self) -> str:
        return "Slack"

    @property
    def auth_type(self) -> AuthType:
        return AuthType.OAUTH2

    @property
    def max_caption_length(self) -> int:
        return MAX_TEXT_LENGTH

    @property
    def supported_post_types(self) -> list[PostType]:
        return [PostType.TEXT, PostType.IMAGE, PostType.VIDEO]

    @property
    def supported_media_types(self) -> list[MediaType]:
        return [
            MediaType.JPEG,
            MediaType.PNG,
            MediaType.GIF,
            MediaType.WEBP,
            MediaType.MP4,
            MediaType.MOV,
            MediaType.PDF,
        ]

    @property
    def required_scopes(self) -> list[str]:
        return list(SCOPES)

    @property
    def rate_limits(self) -> RateLimitConfig:
        # About one message a second per channel; workspace-wide limits are far looser.
        return RateLimitConfig(requests_per_hour=3000, requests_per_day=50000, publish_per_day=1000)

    @classmethod
    def credentials_for_account(cls, account) -> dict:
        return {"channel_id": account.account_platform_id}

    # ------------------------------------------------------------------
    # Requests
    # ------------------------------------------------------------------

    def _api(self, method: str, token: str, *, write: bool = False, **kwargs) -> dict:
        """Call a Web API method. Slack answers 200 with ``ok: false`` and an ``error`` string."""
        data = self._request("POST", f"{API_BASE}/{method}", access_token=token, **kwargs).json()
        if data.get("ok"):
            return data
        raise self._error_for_code(method, data, write=write)

    def _error_for_response(self, response):
        error = super()._error_for_response(response)
        if isinstance(error, APIError) and 400 <= (error.status_code or 0) < 500:
            # Slack reports its own failures as 200 + ok:false; a real 4xx is the request itself being refused.
            error.retryable = False
        return error

    def _error_for_code(self, method: str, data: dict, *, write: bool):
        code = str(data.get("error", "unknown_error"))
        if code in _AUTH_ERRORS:
            return TokenExpiredError(
                f"Slack refused the app's access ({code})",
                status_code=401,
                platform=self.platform_name,
                raw_response=data,
                retryable=False,
            )
        if code in ("ratelimited", "rate_limited"):
            return RateLimitError(
                f"Rate limit exceeded for {self.platform_name}", platform=self.platform_name, raw_response=data
            )
        if code in _RETRYABLE_ERRORS:
            return APIError(
                f"Slack {method} failed ({code})",
                status_code=503,
                platform=self.platform_name,
                raw_response=data,
                # The write may have gone through before Slack failed.
                ambiguous_write=write,
            )
        reason = _PERMANENT_REASONS.get(code)
        if reason:
            return PublishError(reason, platform=self.platform_name, raw_response=data, retryable=False)
        return APIError(
            f"Slack {method} failed ({code})",
            status_code=400,
            platform=self.platform_name,
            raw_response=data,
            retryable=False,
        )

    # ------------------------------------------------------------------
    # OAuth
    # ------------------------------------------------------------------

    def get_auth_url(self, redirect_uri: str, state: str, code_verifier: str | None = None) -> str:
        params = {
            "client_id": self.credentials["client_id"],
            "scope": ",".join(self.required_scopes),
            "redirect_uri": redirect_uri,
            "state": state,
        }
        return f"{AUTH_URL}?{urlencode(params)}"

    def exchange_code(self, code: str, redirect_uri: str, code_verifier: str | None = None) -> OAuthTokens:
        resp = self._request(
            "POST",
            f"{API_BASE}/oauth.v2.access",
            data={
                "client_id": self.credentials["client_id"],
                "client_secret": self.credentials["client_secret"],
                "code": code,
                "redirect_uri": redirect_uri,
            },
        )
        data = resp.json()
        if not data.get("ok"):
            raise OAuthError(
                f"Slack token exchange failed ({data.get('error', 'unknown')})",
                platform=self.platform_name,
                raw_response=data,
            )
        if data.get("refresh_token"):
            # Rotation would invalidate the token every sibling channel stored.
            raise OAuthError(
                "Slack token rotation is turned on for this app. Turn it off under the app's "
                "OAuth & Permissions settings, then connect again.",
                platform=self.platform_name,
                raw_response={"error": "token_rotation_enabled"},
            )
        granted = set((data.get("scope") or "").split(","))
        missing = [scope for scope in SCOPES if scope not in granted]
        if missing:
            raise OAuthError(
                f"Slack didn't grant: {', '.join(missing)}. Reconnect and approve every permission.",
                platform=self.platform_name,
                raw_response=data,
            )
        return OAuthTokens(access_token=data["access_token"], scope=data.get("scope"), raw_response=data)

    def list_destinations(self, tokens: OAuthTokens) -> list[dict]:
        """The channels the app is a member of in the workspace it was just installed into."""
        team = (tokens.raw_response or {}).get("team") or {}
        rows: list[dict] = []
        cursor = ""
        for _ in range(10):  # 10 pages of 200 is more channels than a picker can use
            params = {"types": "public_channel,private_channel", "exclude_archived": "true", "limit": "200"}
            if cursor:
                params["cursor"] = cursor
            page = self._request(
                "GET", f"{API_BASE}/conversations.list", access_token=tokens.access_token, params=params
            ).json()
            if not page.get("ok"):
                raise self._error_for_code("conversations.list", page, write=False)
            for channel in page.get("channels", []):
                if not channel.get("is_member") or channel.get("is_archived"):
                    continue
                rows.append(
                    {
                        "id": channel["id"],
                        "name": f"#{channel['name']}",
                        "handle": team.get("name", ""),
                        "category": "Private channel" if channel.get("is_private") else team.get("name", ""),
                        # Every channel of the workspace posts with the same bot token.
                        "access_token": tokens.access_token,
                        "page_id": team.get("id", ""),
                    }
                )
            cursor = (page.get("response_metadata") or {}).get("next_cursor", "")
            if not cursor:
                break
        return rows

    # ------------------------------------------------------------------
    # Profile
    # ------------------------------------------------------------------

    def get_profile(self, access_token: str) -> AccountProfile:
        """Check the token is live and the app is still in the channel."""
        channel_id = self.credentials["channel_id"]
        info = self._api("conversations.info", access_token, data={"channel": channel_id})["channel"]
        if info.get("is_archived"):
            raise PublishError(_PERMANENT_REASONS["is_archived"], platform=self.platform_name, retryable=False)
        if not info.get("is_member", True):
            raise PublishError(_PERMANENT_REASONS["not_in_channel"], platform=self.platform_name, retryable=False)
        team = ""
        try:
            team = self._api("auth.test", access_token).get("team", "")
        except Exception:
            logger.info("Slack workspace name unavailable for channel %s", channel_id)
        return AccountProfile(
            platform_id=str(info["id"]),
            name=f"#{info.get('name', info['id'])}",
            handle=team or None,
            extra={"is_private": bool(info.get("is_private"))},
        )

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    def publish_post(self, access_token: str, content: PublishContent) -> PublishResult:
        """Post the message, then its files into the message's thread."""
        channel = self.credentials["channel_id"]
        text = content.text or ""
        files = list(content.media_files or [])
        if not text.strip() and not files:
            raise PublishError("Slack needs some text or a file to post.", platform=self.platform_name, retryable=False)
        if len(text) > MAX_TEXT_LENGTH:
            raise PublishError(
                f"Slack messages are limited to {MAX_TEXT_LENGTH} characters here.",
                platform=self.platform_name,
                retryable=False,
            )

        # Files can't ride on chat.postMessage, so a post of only files gets its
        # first file's name as the message the files hang from.
        body = {
            "channel": channel,
            "text": to_mrkdwn(text) if text.strip() else os.path.basename(files[0]),
            "mrkdwn": True,
        }
        thread_ts = content.extra.get("thread_ts")
        if thread_ts:
            body["thread_ts"] = str(thread_ts)
        if content.extra.get("no_unfurl"):
            body.update(unfurl_links=False, unfurl_media=False)
        message = self._api("chat.postMessage", access_token, write=True, json=body)
        ts = message["ts"]

        if files:
            self._share_files(access_token, channel, files, thread_ts=str(thread_ts or ts))

        return PublishResult(
            platform_post_id=ts,
            url=self._permalink(access_token, channel, ts),
            extra={"channel": channel, "ts": ts},
        )

    def _share_files(self, access_token: str, channel: str, paths: list[str], *, thread_ts: str) -> None:
        """Upload ``paths`` and share them in ``thread_ts``'s thread with one completeUploadExternal call."""
        uploaded = []
        for path in paths:
            name = os.path.basename(path)
            ticket = self._api(
                "files.getUploadURLExternal",
                access_token,
                data={"filename": name, "length": str(os.path.getsize(path))},
            )
            upload_url = ticket["upload_url"]
            host = urlparse(upload_url).hostname or ""
            if not (host == "slack.com" or host.endswith(".slack.com")):
                raise PublishError(
                    "Slack returned an upload address we won't send files to.",
                    platform=self.platform_name,
                    retryable=False,
                )
            with open(path, "rb") as handle:
                self._request(
                    "POST", upload_url, data=handle, headers={"Content-Type": "application/octet-stream"}, timeout=300
                )
            uploaded.append({"id": ticket["file_id"], "title": name})
        self._api(
            "files.completeUploadExternal",
            access_token,
            write=True,
            json={"files": uploaded, "channel_id": channel, "thread_ts": thread_ts},
        )

    def _permalink(self, access_token: str, channel: str, ts: str) -> str | None:
        try:
            resp = self._request(
                "GET",
                f"{API_BASE}/chat.getPermalink",
                access_token=access_token,
                params={"channel": channel, "message_ts": ts},
            )
            return resp.json().get("permalink") or None
        except Exception:
            logger.info("Slack permalink unavailable for %s %s", channel, ts)
            return None

    def publish_thread_part(
        self,
        access_token: str,
        content: PublishContent,
        *,
        root: PublishResult,
        parent: PublishResult,
    ) -> PublishResult:
        """Publish a thread part as a reply in the first message's thread."""
        return self.publish_post(
            access_token,
            replace(content, extra={**content.extra, "thread_ts": root.platform_post_id}),
        )

    def publish_comment(self, access_token: str, post_id: str, text: str) -> CommentResult:
        """Reply in the thread of a message (the first comment)."""
        message = self._api(
            "chat.postMessage",
            access_token,
            write=True,
            json={
                "channel": self.credentials["channel_id"],
                "text": to_mrkdwn(text),
                "mrkdwn": True,
                "thread_ts": str(post_id),
            },
        )
        return CommentResult(platform_comment_id=message["ts"], extra={"channel": self.credentials["channel_id"]})

    def delete_post(self, access_token: str, post_id: str) -> bool:
        """Delete a message the app posted (``chat.delete``)."""
        return bool(
            self._api(
                "chat.delete", access_token, write=True, json={"channel": self.credentials["channel_id"], "ts": post_id}
            ).get("ok")
        )
