"""Kick provider: chat messages in the connected channel's chat.

Kick's public API publishes nothing but chat (there are no posts to schedule), so a
scheduled "post" here is a message the connected user sends to their own channel's chat.

Documentation checked 2026-10-24:

- OAuth 2.1 with PKCE (``https://id.kick.com/oauth/authorize``, ``/oauth/token``, ``/oauth/revoke``;
  the refresh response carries a new refresh token): https://docs.kick.com/getting-started/generating-tokens-oauth2-flow
- Post chat message (``POST https://api.kick.com/public/v1/chat``, scope ``chat:write``, ``type`` user,
  ``broadcaster_user_id``, 500 characters and at most 2048 bytes): https://docs.kick.com/apis/chat
- Get users (``GET /public/v1/users``, scope ``user:read``): https://docs.kick.com/apis/users
"""

from __future__ import annotations

import base64
import hashlib
import logging
from urllib.parse import urlencode

from .base import SocialProvider
from .exceptions import APIError, OAuthError, PublishError
from .types import (
    AccountProfile,
    AuthType,
    MediaType,
    OAuthTokens,
    PostType,
    PublishContent,
    PublishResult,
    RateLimitConfig,
)

logger = logging.getLogger(__name__)

AUTH_URL = "https://id.kick.com/oauth/authorize"
TOKEN_URL = "https://id.kick.com/oauth/token"
REVOKE_URL = "https://id.kick.com/oauth/revoke"
API_BASE = "https://api.kick.com/public/v1"
SCOPES = ["user:read", "chat:write"]
MAX_MESSAGE_LENGTH = 500
MAX_MESSAGE_BYTES = 2048


def _code_challenge(code_verifier: str) -> str:
    """The S256 PKCE challenge: base64url (no padding) of the verifier's SHA-256 (RFC 7636)."""
    digest = hashlib.sha256(code_verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


class KickProvider(SocialProvider):
    """Kick provider: a chat message to the connected user's own channel."""

    profile_url_template = "https://kick.com/{handle}"
    uses_pkce = True
    accepts_media = False
    needs_local_media = False
    revokes_grant_on_disconnect = True

    writing_style = (
        "Short, energetic and chat-native: one or two lines a viewer can take in at a glance, with the channel's "
        "emotes. No hashtags."
    )

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    @property
    def platform_name(self) -> str:
        return "Kick"

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
        return RateLimitConfig(requests_per_hour=600, requests_per_day=5000, publish_per_day=100)

    # ------------------------------------------------------------------
    # OAuth
    # ------------------------------------------------------------------

    def get_auth_url(self, redirect_uri: str, state: str, code_verifier: str | None = None) -> str:
        if not code_verifier:
            raise OAuthError("Kick needs a PKCE code verifier.", platform=self.platform_name)
        params = {
            "response_type": "code",
            "client_id": self.credentials["client_id"],
            "redirect_uri": redirect_uri,
            "scope": " ".join(self.required_scopes),
            "state": state,
            "code_challenge": _code_challenge(code_verifier),
            "code_challenge_method": "S256",
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
        ).json()
        if "access_token" not in body:
            raise OAuthError(f"Kick {what} failed", platform=self.platform_name, raw_response=body)
        return OAuthTokens(
            access_token=body["access_token"],
            refresh_token=body.get("refresh_token"),
            expires_in=body.get("expires_in"),
            scope=body.get("scope"),
            raw_response=body,
        )

    def exchange_code(self, code: str, redirect_uri: str, code_verifier: str | None = None) -> OAuthTokens:
        if not code_verifier:
            raise OAuthError("Kick needs the PKCE code verifier.", platform=self.platform_name)
        tokens = self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
            },
            "token exchange",
        )
        granted = set((tokens.scope or "").replace(",", " ").split())
        if granted and "chat:write" not in granted:
            raise OAuthError(
                "Kick did not grant permission to send chat messages. Reconnect and approve it.",
                platform=self.platform_name,
                retryable=False,
            )
        return tokens

    def refresh_token(self, refresh_token: str) -> OAuthTokens:
        return self._token_request({"grant_type": "refresh_token", "refresh_token": refresh_token}, "token refresh")

    def revoke_token(self, access_token: str) -> bool:
        self._request("POST", REVOKE_URL, data={"token": access_token, "token_hint_type": "access_token"})
        return True

    # ------------------------------------------------------------------
    # Profile
    # ------------------------------------------------------------------

    def _me(self, access_token: str) -> dict:
        users = self._request("GET", f"{API_BASE}/users", access_token=access_token).json().get("data") or []
        if not users:
            raise OAuthError("Kick did not return the signed-in user", platform=self.platform_name)
        return users[0]

    def get_profile(self, access_token: str) -> AccountProfile:
        user = self._me(access_token)
        name = user.get("name", "")
        return AccountProfile(
            platform_id=str(user["user_id"]),
            name=name,
            handle=name,
            avatar_url=user.get("profile_picture"),
            # The address is not needed and is not kept.
            extra={"user_id": user["user_id"]},
        )

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    @classmethod
    def credentials_for_account(cls, account) -> dict:
        # The channel is the account itself: its user id is the broadcaster.
        return {"user_id": account.account_platform_id}

    def _user_id(self) -> int:
        user_id = str(self.credentials.get("user_id") or "")
        if not user_id.isdigit():
            raise PublishError(
                "This Kick account has no channel attached. Reconnect it.",
                platform=self.platform_name,
                retryable=False,
            )
        return int(user_id)

    def publish_post(self, access_token: str, content: PublishContent) -> PublishResult:
        text = (content.text or "").strip()
        if not text:
            raise PublishError("Kick needs some text to send to chat.", platform=self.platform_name, retryable=False)
        if len(text) > MAX_MESSAGE_LENGTH or len(text.encode()) > MAX_MESSAGE_BYTES:
            raise PublishError(
                f"Kick chat messages are limited to {MAX_MESSAGE_LENGTH} characters ({MAX_MESSAGE_BYTES} bytes).",
                platform=self.platform_name,
                retryable=False,
            )
        user_id = self._user_id()
        data = self._request(
            "POST",
            f"{API_BASE}/chat",
            access_token=access_token,
            json={"content": text, "type": "user", "broadcaster_user_id": user_id},
        ).json()
        sent = data.get("data") or {}
        if sent.get("is_sent") is False:
            raise PublishError("Kick did not send the message.", platform=self.platform_name, retryable=False)
        message_id = str(sent.get("message_id") or "")
        if not message_id:
            raise APIError(
                "Kick accepted the message but returned no id",
                platform=self.platform_name,
                raw_response=data,
                ambiguous_write=True,
            )
        return PublishResult(platform_post_id=message_id)
