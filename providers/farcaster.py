"""Farcaster provider through the customer's own Neynar API key and an approved signer.

Farcaster is a protocol, not one company's API: casts are signed by a "signer" key that the
account owner approved on chain. Neynar (https://neynar.com) is the managed service that holds
signers and submits casts, and it bills API use to the key's owner, so the customer brings
their own (the same arrangement as X). Nothing about Farcaster is free of a third party we
could use instead: the hubs accept casts only signed by a key the user approved, and holding
or generating that key for someone is exactly what a managed signer service is for.

The customer enters, on the connect page, their Neynar API key (stored as this workspace's
encrypted ``PlatformCredential``) and the UUID of an *approved* signer made with that key
(stored encrypted as the account's token). The signer UUID only works with the key that
created it.

Documentation checked 2026-10-24:

- Publish a cast (``POST /v2/farcaster/cast``: ``signer_uuid``, ``text``, ``embeds`` at most 2,
  ``parent``, ``channel_id``, ``idem``; ``x-api-key`` header): https://docs.neynar.com/reference/publish-cast
- Delete a cast (``DELETE /v2/farcaster/cast``): https://docs.neynar.com/reference/delete-cast
- Look up a signer (``GET /v2/farcaster/signer``; status ``approved`` and the ``fid``):
  https://docs.neynar.com/reference/lookup-signer
- Users by FID (``GET /v2/farcaster/user/bulk``): https://docs.neynar.com/reference/fetch-bulk-users
- Text is limited to 320 bytes by the protocol (a longer "cast" is a Farcaster Pro feature):
  https://hackmd.io/@farcasterxyz/BJeFoxdy3
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import replace

from .base import SocialProvider
from .exceptions import APIError, OAuthError, PublishError
from .types import (
    AccountProfile,
    AuthType,
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

API_BASE = "https://api.neynar.com/v2/farcaster"
MAX_CAST_BYTES = 320
MAX_EMBEDS = 2

_CHANNEL = re.compile(r"[a-z0-9][a-z0-9-]{0,49}")
_HASH = re.compile(r"(0x)?[0-9a-fA-F]{40}")

_FARCASTER_DOCS = "https://docs.neynar.com/reference/publish-cast"
FARCASTER_MEDIA_RULES = {
    # Images are embedded by link; a cast carries two embeds in all.
    PostType.IMAGE: MediaRules(source=_FARCASTER_DOCS, max_items=MAX_EMBEDS, media_types=frozenset({"image", "gif"})),
}


def normalize_channel(raw) -> str:
    """A Farcaster channel id (``dev``, not ``/dev``), or "" when it can't be one."""
    value = str(raw or "").strip().lstrip("/").lower()
    return value if _CHANNEL.fullmatch(value) else ""


class FarcasterProvider(SocialProvider):
    """Farcaster casts through Neynar: the customer's API key (credentials) and an approved signer (token)."""

    user_supplied_credentials = True
    connect_url_name = "connect_farcaster"
    needs_local_media = False
    media_rules = FARCASTER_MEDIA_RULES
    supports_connection_link = False
    profile_url_template = "https://farcaster.xyz/{handle}"

    # Each part is a cast replying to the one before it.
    thread = ThreadCapability(mode=ThreadMode.REPLY_CHAIN, max_parts=25, media_per_part=True)

    writing_style = (
        "Short, conversational and crypto-native in tone without jargon for its own sake. Straight to the "
        "point: a cast is 320 bytes. Link or image as an embed, and a channel when one fits."
    )

    post_settings = (
        PostSetting(
            key="channel",
            label="Channel",
            kind="text",
            max_length=50,
            placeholder="e.g. dev",
            help="Optional. The channel id, as it appears after / in the channel's address.",
        ),
    )
    account_default_keys = ("channel",)

    @classmethod
    def validate_post_settings(cls, settings: dict) -> str:
        error = super().validate_post_settings(settings)
        if error:
            return error
        channel = (settings or {}).get("channel")
        if channel and not normalize_channel(channel):
            return "Channel must be a channel id like dev (lowercase letters, numbers and hyphens)."
        return ""

    @classmethod
    def permalink_for(cls, platform_post_id: str, account) -> str:
        handle = (account.account_handle or "").strip().lstrip("@")
        post_hash = (platform_post_id or "").strip()
        if not (re.fullmatch(r"[A-Za-z0-9._-]{1,100}", handle) and _HASH.fullmatch(post_hash)):
            return ""
        return f"https://farcaster.xyz/{handle}/{post_hash}"

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    # A cast is 320 UTF-8 bytes, not characters.
    text_length_unit = "utf8_bytes"

    @property
    def platform_name(self) -> str:
        return "Farcaster"

    @property
    def auth_type(self) -> AuthType:
        return AuthType.SESSION

    @property
    def max_caption_length(self) -> int:
        return MAX_CAST_BYTES

    @property
    def supported_post_types(self) -> list[PostType]:
        return [PostType.TEXT, PostType.IMAGE, PostType.LINK]

    @property
    def supported_media_types(self) -> list[MediaType]:
        return [MediaType.JPEG, MediaType.PNG, MediaType.GIF, MediaType.WEBP]

    @property
    def required_scopes(self) -> list[str]:
        return []

    @property
    def rate_limits(self) -> RateLimitConfig:
        # Neynar's limits depend on the customer's plan; stay modest.
        return RateLimitConfig(requests_per_hour=600, requests_per_day=5000, publish_per_day=100)

    # ------------------------------------------------------------------
    # Requests
    # ------------------------------------------------------------------

    def _headers(self) -> dict:
        api_key = (self.credentials.get("api_key") or "").strip()
        if not api_key:
            raise OAuthError(
                "Add your Neynar API key to connect Farcaster.", platform=self.platform_name, retryable=False
            )
        return {"x-api-key": api_key}

    def _api(self, method: str, path: str, **kwargs):
        return self._request(method, f"{API_BASE}{path}", headers=self._headers(), **kwargs)

    def _error_for_response(self, response):
        if response.status_code == 402:
            return APIError(
                "Neynar says your plan doesn't include this call. Check your Neynar plan and credits.",
                status_code=402,
                platform=self.platform_name,
                raw_response=self._safe_json(response),
                retryable=False,
            )
        if response.status_code == 403:
            return APIError(
                "Neynar refused the request. Check that the API key is the one the signer was created with "
                "and that the signer is approved.",
                status_code=403,
                platform=self.platform_name,
                raw_response=self._safe_json(response),
                retryable=False,
            )
        return super()._error_for_response(response)

    # ------------------------------------------------------------------
    # Connect
    # ------------------------------------------------------------------

    def connect(self, signer_uuid: str) -> tuple[OAuthTokens, AccountProfile]:
        """Check the signer is approved for this key and read its account's profile."""
        signer = self._api("GET", "/signer", params={"signer_uuid": signer_uuid}).json()
        if signer.get("status") != "approved" or not signer.get("fid"):
            raise OAuthError(
                f"That signer is not approved yet (status: {signer.get('status') or 'unknown'}). "
                "Approve it in Farcaster, then connect.",
                platform=self.platform_name,
                retryable=False,
            )
        return OAuthTokens(access_token=signer_uuid), self._profile_for_fid(int(signer["fid"]))

    def _profile_for_fid(self, fid: int) -> AccountProfile:
        users = self._api("GET", "/user/bulk", params={"fids": str(fid)}).json().get("users") or []
        if not users:
            raise OAuthError("Neynar found no Farcaster user for this signer", platform=self.platform_name)
        user = users[0]
        username = user.get("username") or ""
        return AccountProfile(
            platform_id=str(user.get("fid", fid)),
            name=user.get("display_name") or username,
            handle=username,
            avatar_url=user.get("pfp_url"),
            follower_count=int(user.get("follower_count") or 0),
            extra={"fid": fid},
        )

    def get_profile(self, access_token: str) -> AccountProfile:
        """Health check: the signer is still approved, and the account it signs for."""
        _tokens, profile = self.connect(access_token)
        return profile

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    @staticmethod
    def _idem(content: PublishContent, signer_uuid: str, parent: str) -> str | None:
        """Neynar's 16-character idempotency key, stable across retries of the same publish."""
        if not content.idempotency_key:
            return None
        return hashlib.sha256(f"{content.idempotency_key}\0{signer_uuid}\0{parent}".encode()).hexdigest()[:16]

    def publish_post(self, access_token: str, content: PublishContent) -> PublishResult:
        text = content.text or ""
        embeds = [{"url": url} for url in ([content.link_url] if content.link_url else []) + list(content.media_urls)]
        if len(embeds) > MAX_EMBEDS:
            raise PublishError(
                f"A cast can carry {MAX_EMBEDS} images or links at most.",
                platform=self.platform_name,
                retryable=False,
            )
        if not text.strip() and not embeds:
            raise PublishError(
                "Farcaster needs some text or an image to post.", platform=self.platform_name, retryable=False
            )
        if len(text.encode()) > MAX_CAST_BYTES:
            raise PublishError(
                f"Farcaster casts are limited to {MAX_CAST_BYTES} bytes (emoji and non-Latin letters take more "
                "than one).",
                platform=self.platform_name,
                retryable=False,
            )
        parent = str(content.extra.get("_parent_hash") or "")
        body: dict = {"signer_uuid": access_token, "text": text}
        if embeds:
            body["embeds"] = embeds
        if parent:
            body["parent"] = parent
            if content.extra.get("_parent_author_fid"):
                body["parent_author_fid"] = int(content.extra["_parent_author_fid"])
        else:
            channel = normalize_channel(content.extra.get("channel"))
            if channel:
                body["channel_id"] = channel
        idem = self._idem(content, access_token, parent)
        if idem:
            body["idem"] = idem

        data = self._api("POST", "/cast", json=body).json()
        cast = data.get("cast") or {}
        post_hash = cast.get("hash")
        if not post_hash:
            raise APIError(
                "Neynar accepted the cast but returned no hash",
                platform=self.platform_name,
                raw_response=data,
                ambiguous_write=True,
            )
        author = cast.get("author") or {}
        return PublishResult(
            platform_post_id=post_hash,
            url=(f"https://farcaster.xyz/{author['username']}/{post_hash}" if author.get("username") else None),
            extra={"fid": author.get("fid")},
        )

    def publish_thread_part(
        self, access_token: str, content: PublishContent, *, root: PublishResult, parent: PublishResult
    ) -> PublishResult:
        return self.publish_post(
            access_token,
            replace(
                content,
                extra={
                    **content.extra,
                    "_parent_hash": parent.platform_post_id,
                    "_parent_author_fid": parent.extra.get("fid") or root.extra.get("fid"),
                },
            ),
        )

    def delete_post(self, access_token: str, post_id: str) -> bool:
        self._api("DELETE", "/cast", json={"signer_uuid": access_token, "target_hash": post_id})
        return True
