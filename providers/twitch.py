"""Twitch provider: chat messages and announcements in the connected channel's chat.

Twitch has no feed to post to; what a streamer schedules is a message to their own chat room,
either a plain chat message or a highlighted announcement. Both are sent as the connected
user to their own channel.

Documentation checked 2026-10-24:

- Authorization code grant (``https://id.twitch.tv/oauth2/authorize`` / ``/oauth2/token``):
  https://dev.twitch.tv/docs/authentication/getting-tokens-oauth/
- Send Chat Message (``POST /helix/chat/messages``, scope ``user:write:chat``, 500 characters,
  200 with ``is_sent`` / ``drop_reason``): https://dev.twitch.tv/docs/api/reference/#send-chat-message
- Send Chat Announcement (``POST /helix/chat/announcements``, scope ``moderator:manage:announcements``,
  500 characters, colours blue/green/orange/purple/primary, 204): https://dev.twitch.tv/docs/api/reference/#send-chat-announcement
- Get Users (``GET /helix/users``): https://dev.twitch.tv/docs/api/reference/#get-users
"""

from __future__ import annotations

import logging
from urllib.parse import urlencode

from .base import SocialProvider
from .exceptions import APIError, OAuthError, PublishError
from .types import (
    AccountProfile,
    AuthType,
    MediaType,
    OAuthTokens,
    PostSetting,
    PostType,
    PublishContent,
    PublishResult,
    RateLimitConfig,
)

logger = logging.getLogger(__name__)

AUTH_URL = "https://id.twitch.tv/oauth2/authorize"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"
REVOKE_URL = "https://id.twitch.tv/oauth2/revoke"
API_BASE = "https://api.twitch.tv/helix"
SCOPES = ["user:write:chat", "moderator:manage:announcements"]
MAX_MESSAGE_LENGTH = 500

ANNOUNCEMENT_COLORS = (
    ("primary", "Channel colour"),
    ("blue", "Blue"),
    ("green", "Green"),
    ("orange", "Orange"),
    ("purple", "Purple"),
)


class TwitchProvider(SocialProvider):
    """Twitch provider: a chat message or announcement to the connected user's own channel."""

    profile_url_template = "https://www.twitch.tv/{handle}"
    accepts_media = False
    needs_local_media = False
    revokes_grant_on_disconnect = True
    revocation_uses_refresh_token = False

    writing_style = (
        "Short, energetic and chat-native: one or two lines a viewer can take in at a glance, with the channel's "
        "emotes by name. No hashtags."
    )

    post_settings = (
        PostSetting(
            key="kind",
            label="Send as",
            kind="select",
            choices=(("message", "Chat message"), ("announcement", "Announcement")),
            help="An announcement is highlighted in chat for everyone. A chat message appears as from you.",
        ),
        PostSetting(
            key="color",
            label="Announcement colour",
            kind="select",
            choices=ANNOUNCEMENT_COLORS,
            show_when="kind=announcement",
        ),
    )
    account_default_keys = ("kind", "color")
    account_default_choices = {"kind": ("message", "announcement"), "color": tuple(c for c, _ in ANNOUNCEMENT_COLORS)}

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    @property
    def platform_name(self) -> str:
        return "Twitch"

    @property
    def auth_type(self) -> AuthType:
        return AuthType.OAUTH2

    @property
    def max_caption_length(self) -> int:
        return MAX_MESSAGE_LENGTH

    @property
    def supported_post_types(self) -> list[PostType]:
        return [PostType.TEXT]

    @property
    def supported_media_types(self) -> list[MediaType]:
        return []

    @property
    def required_scopes(self) -> list[str]:
        return list(SCOPES)

    @property
    def rate_limits(self) -> RateLimitConfig:
        # One announcement every 2 seconds; chat itself is throttled per channel.
        return RateLimitConfig(requests_per_hour=800, requests_per_day=5000, publish_per_day=100)

    # ------------------------------------------------------------------
    # OAuth
    # ------------------------------------------------------------------

    def get_auth_url(self, redirect_uri: str, state: str, code_verifier: str | None = None) -> str:
        params = {
            "client_id": self.credentials["client_id"],
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(self.required_scopes),
            "state": state,
        }
        return f"{AUTH_URL}?{urlencode(params)}"

    def _token_request(self, form: dict, what: str) -> OAuthTokens:
        body = self._request(
            "POST",
            TOKEN_URL,
            data={
                **form,
                "client_id": self.credentials["client_id"],
                "client_secret": self.credentials["client_secret"],
            },
            ambiguous_on_failure=False,
        ).json()
        if "access_token" not in body:
            raise OAuthError(f"Twitch {what} failed", platform=self.platform_name, raw_response=body)
        return OAuthTokens(
            access_token=body["access_token"],
            refresh_token=body.get("refresh_token"),
            expires_in=body.get("expires_in"),
            scope=" ".join(body["scope"]) if isinstance(body.get("scope"), list) else body.get("scope"),
            raw_response=body,
        )

    def exchange_code(self, code: str, redirect_uri: str, code_verifier: str | None = None) -> OAuthTokens:
        tokens = self._token_request(
            {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri}, "token exchange"
        )
        granted = set((tokens.scope or "").split())
        missing = [scope for scope in SCOPES if scope not in granted]
        if missing:
            raise OAuthError(
                f"Twitch did not grant {', '.join(missing)}. Reconnect and approve every permission.",
                platform=self.platform_name,
                retryable=False,
            )
        return tokens

    def refresh_token(self, refresh_token: str) -> OAuthTokens:
        tokens = self._token_request({"grant_type": "refresh_token", "refresh_token": refresh_token}, "token refresh")
        if tokens.refresh_token:
            return tokens
        return OAuthTokens(
            access_token=tokens.access_token,
            refresh_token=refresh_token,
            expires_in=tokens.expires_in,
            scope=tokens.scope,
            raw_response=tokens.raw_response,
        )

    def revoke_token(self, access_token: str) -> bool:
        self._request("POST", REVOKE_URL, data={"client_id": self.credentials["client_id"], "token": access_token})
        return True

    # ------------------------------------------------------------------
    # Profile
    # ------------------------------------------------------------------

    def _api(self, method: str, path: str, access_token: str, **kwargs):
        return self._request(
            method,
            f"{API_BASE}{path}",
            access_token=access_token,
            headers={"Client-Id": self.credentials["client_id"]},
            **kwargs,
        )

    def _me(self, access_token: str) -> dict:
        users = self._api("GET", "/users", access_token).json().get("data") or []
        if not users:
            raise OAuthError("Twitch did not return the signed-in user", platform=self.platform_name)
        return users[0]

    def get_profile(self, access_token: str) -> AccountProfile:
        user = self._me(access_token)
        return AccountProfile(
            platform_id=str(user["id"]),
            name=user.get("display_name") or user.get("login", ""),
            handle=user.get("login"),
            avatar_url=user.get("profile_image_url"),
            extra=user,
        )

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    @classmethod
    def credentials_for_account(cls, account) -> dict:
        # The channel is the account itself: its user id is both broadcaster and sender.
        return {"user_id": account.account_platform_id}

    def _user_id(self) -> str:
        user_id = str(self.credentials.get("user_id") or "")
        if not user_id.isdigit():
            raise PublishError(
                "This Twitch account has no channel attached. Reconnect it.",
                platform=self.platform_name,
                retryable=False,
            )
        return user_id

    @classmethod
    def validate_post_settings(cls, settings: dict) -> str:
        error = super().validate_post_settings(settings)
        if error:
            return error
        if (settings or {}).get("color") and (settings or {}).get("kind") != "announcement":
            return "An announcement colour only applies to announcements."
        return ""

    def publish_post(self, access_token: str, content: PublishContent) -> PublishResult:
        text = (content.text or "").strip()
        if not text:
            raise PublishError("Twitch needs some text to send to chat.", platform=self.platform_name, retryable=False)
        if len(text) > MAX_MESSAGE_LENGTH:
            raise PublishError(
                f"Twitch chat messages are limited to {MAX_MESSAGE_LENGTH} characters.",
                platform=self.platform_name,
                retryable=False,
            )
        user_id = self._user_id()
        if content.extra.get("kind") == "announcement":
            body: dict = {"message": text}
            if content.extra.get("color"):
                body["color"] = content.extra["color"]
            self._api(
                "POST",
                "/chat/announcements",
                access_token,
                params={"broadcaster_id": user_id, "moderator_id": user_id},
                json=body,
            )
            # The announcement endpoint answers 204 without an id.
            return PublishResult(platform_post_id=f"announcement:{user_id}", extra={"kind": "announcement"})

        data = self._api(
            "POST",
            "/chat/messages",
            access_token,
            json={"broadcaster_id": user_id, "sender_id": user_id, "message": text},
        ).json()
        sent = (data.get("data") or [{}])[0]
        if not sent.get("is_sent"):
            reason = (sent.get("drop_reason") or {}).get("message") or "Twitch dropped the message"
            raise PublishError(
                f"Twitch did not send the message: {reason}", platform=self.platform_name, retryable=False
            )
        message_id = str(sent.get("message_id") or "")
        if not message_id:
            raise APIError(
                "Twitch accepted the message but returned no id",
                platform=self.platform_name,
                raw_response=data,
                ambiguous_write=True,
            )
        return PublishResult(platform_post_id=message_id, extra={"kind": "message"})
