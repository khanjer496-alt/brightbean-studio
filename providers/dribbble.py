"""Dribbble provider: design shots (one image, a title, a description and tags).

Documentation checked 2026-10-24:

- OAuth 2 (``https://dribbble.com/oauth/authorize`` / ``/oauth/token``, scopes ``public`` and ``upload``;
  the token response carries no expiry or refresh token): https://developer.dribbble.com/v2/oauth/
- Create a shot (``POST https://api.dribbble.com/v2/shots``, scope ``upload``, the user must be a player or a
  team; ``image`` exactly 400x300 or 800x600, at most 8 MB, GIF/JPG/PNG; ``title`` required; at most 12 tags;
  video shots are not supported; answers 202 with a ``Location`` header that returns 404 until the shot exists):
  https://developer.dribbble.com/v2/shots/
"""

from __future__ import annotations

import logging
import os
from urllib.parse import urlencode, urlsplit

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
    PublishState,
    PublishStatus,
    RateLimitConfig,
)

logger = logging.getLogger(__name__)

AUTH_URL = "https://dribbble.com/oauth/authorize"
TOKEN_URL = "https://dribbble.com/oauth/token"
API_BASE = "https://api.dribbble.com/v2"
SCOPES = ["public", "upload"]
MAX_TAGS = 12
# The two sizes the shots endpoint accepts.
SHOT_SIZES = frozenset({(400, 300), (800, 600)})

_DRIBBBLE_DOCS = "https://developer.dribbble.com/v2/shots/"
DRIBBBLE_MEDIA_RULES = {
    PostType.IMAGE: MediaRules(
        source=_DRIBBBLE_DOCS,
        max_items=1,
        media_types=frozenset({"image", "gif"}),
        min_aspect_ratio=4 / 3,
        max_aspect_ratio=4 / 3,
        min_width=400,
        min_height=300,
        max_width=800,
        max_height=600,
        max_image_bytes=8 * 1024 * 1024,
    ),
}


def parse_tags(raw) -> list[str]:
    tags: list[str] = []
    for part in str(raw or "").replace("\n", ",").split(","):
        tag = part.strip().lstrip("#").strip()
        if tag and tag.lower() not in {t.lower() for t in tags}:
            tags.append(tag)
    return tags


class DribbbleProvider(SocialProvider):
    """Dribbble provider: a shot per post, published asynchronously."""

    profile_url_template = "https://dribbble.com/{handle}"
    media_rules = DRIBBBLE_MEDIA_RULES
    publish_is_async = True

    writing_style = (
        "A short, specific description of the design decision behind the work: what problem it solves and how. "
        "Designer-to-designer, no hype. Up to 12 tags."
    )

    post_settings = (
        PostSetting(
            key="tags",
            label="Tags",
            kind="text",
            max_length=300,
            placeholder="ui, dashboard, branding",
            help=f"Separated by commas, at most {MAX_TAGS}.",
        ),
    )
    account_default_keys = ("tags",)

    @classmethod
    def validate_post_settings(cls, settings: dict) -> str:
        error = super().validate_post_settings(settings)
        if error:
            return error
        if len(parse_tags((settings or {}).get("tags"))) > MAX_TAGS:
            return f"Dribbble allows at most {MAX_TAGS} tags."
        return ""

    @classmethod
    def permalink_for(cls, platform_post_id: str, account) -> str:
        shot_id = (platform_post_id or "").strip()
        return f"https://dribbble.com/shots/{shot_id}" if shot_id.isdigit() else ""

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    @property
    def platform_name(self) -> str:
        return "Dribbble"

    @property
    def auth_type(self) -> AuthType:
        return AuthType.OAUTH2

    @property
    def max_caption_length(self) -> int:
        # The shots documentation gives no description limit; this is a guardrail of ours.
        return 4000

    @property
    def supported_post_types(self) -> list[PostType]:
        return [PostType.IMAGE]

    @property
    def supported_media_types(self) -> list[MediaType]:
        return [MediaType.JPEG, MediaType.PNG, MediaType.GIF]

    @property
    def required_scopes(self) -> list[str]:
        return list(SCOPES)

    @property
    def rate_limits(self) -> RateLimitConfig:
        # Third-party wrappers report 60 requests a minute and 1,440 a day for OAuth calls (not in the official docs).
        return RateLimitConfig(requests_per_hour=1440, requests_per_day=1440, publish_per_day=10)

    # ------------------------------------------------------------------
    # OAuth
    # ------------------------------------------------------------------

    def get_auth_url(self, redirect_uri: str, state: str, code_verifier: str | None = None) -> str:
        params = {
            "client_id": self.credentials["client_id"],
            "redirect_uri": redirect_uri,
            "scope": " ".join(self.required_scopes),
            "state": state,
        }
        return f"{AUTH_URL}?{urlencode(params)}"

    def exchange_code(self, code: str, redirect_uri: str, code_verifier: str | None = None) -> OAuthTokens:
        body = self._request(
            "POST",
            TOKEN_URL,
            data={
                "client_id": self.credentials["client_id"],
                "client_secret": self.credentials["client_secret"],
                "code": code,
                "redirect_uri": redirect_uri,
            },
        ).json()
        if "access_token" not in body:
            raise OAuthError("Dribbble token exchange failed", platform=self.platform_name, raw_response=body)
        granted = set(str(body.get("scope") or "").split())
        if "upload" not in granted:
            raise OAuthError(
                "Dribbble did not grant permission to upload shots. Reconnect and approve it.",
                platform=self.platform_name,
                retryable=False,
            )
        return OAuthTokens(access_token=body["access_token"], scope=body.get("scope"), raw_response=body)

    # ------------------------------------------------------------------
    # Profile
    # ------------------------------------------------------------------

    def get_profile(self, access_token: str) -> AccountProfile:
        user = self._request("GET", f"{API_BASE}/user", access_token=access_token).json()
        if not user.get("id"):
            raise OAuthError("Dribbble did not return the signed-in user", platform=self.platform_name)
        login = user.get("login") or ""
        return AccountProfile(
            platform_id=str(user["id"]),
            name=user.get("name") or login,
            handle=login,
            avatar_url=user.get("avatar_url"),
            follower_count=int(user.get("followers_count") or 0),
            extra={"id": user["id"], "can_upload_shot": user.get("can_upload_shot")},
        )

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    @staticmethod
    def _image_size(path: str) -> tuple[int, int]:
        from PIL import Image

        with Image.open(path) as image:
            return image.size

    def publish_post(self, access_token: str, content: PublishContent) -> PublishResult:
        title = (content.title or "").strip()
        if not title:
            raise PublishError(
                "Dribbble requires a title. Set the post title before publishing.",
                platform=self.platform_name,
                retryable=False,
            )
        if len(content.media_files) != 1:
            raise PublishError("A Dribbble shot needs exactly one image.", platform=self.platform_name, retryable=False)
        path = content.media_files[0]
        try:
            size = self._image_size(path)
        except Exception as exc:
            raise PublishError(
                "Dribbble could not read that image.", platform=self.platform_name, retryable=False
            ) from exc
        if size not in SHOT_SIZES:
            raise PublishError(
                f"Dribbble shots must be exactly 400x300 or 800x600 pixels (this image is {size[0]}x{size[1]}).",
                platform=self.platform_name,
                retryable=False,
            )
        tags = parse_tags(content.extra.get("tags"))
        if len(tags) > MAX_TAGS:
            raise PublishError(
                f"Dribbble allows at most {MAX_TAGS} tags.", platform=self.platform_name, retryable=False
            )
        # Multipart parts as (name, value) pairs, because ``tags[]`` repeats.
        fields = [("title", title)]
        if content.text:
            fields.append(("description", content.text))
        fields.extend(("tags[]", tag) for tag in tags)
        with open(path, "rb") as image:
            parts = [("image", (os.path.basename(path), image)), *((name, (None, value)) for name, value in fields)]
            response = self._request("POST", f"{API_BASE}/shots", access_token=access_token, files=parts, timeout=120)
        location = response.headers.get("Location", "")
        shot_id = urlsplit(location).path.rstrip("/").rsplit("/", 1)[-1]
        if not shot_id.isdigit():
            # Accepted without telling us where the shot will be: do not upload it again.
            raise APIError(
                "Dribbble accepted the shot but returned no location",
                platform=self.platform_name,
                ambiguous_write=True,
            )
        return PublishResult(platform_post_id=shot_id, url=f"https://dribbble.com/shots/{shot_id}")

    def check_publish_status(self, access_token: str, handle: str) -> PublishStatus:
        """The shot answers 404 until Dribbble has processed the upload, then 200."""
        try:
            self._request("GET", f"{API_BASE}/shots/{handle}", access_token=access_token)
        except APIError as exc:
            if exc.status_code == 404:
                return PublishStatus(state=PublishState.PENDING)
            raise
        return PublishStatus(state=PublishState.COMPLETE, platform_post_id=handle)
