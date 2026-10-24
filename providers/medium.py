"""Medium provider implementation.

Medium stopped issuing new integration tokens and no longer accepts new integrations
(https://github.com/Medium/medium-api-docs, archived), but tokens created before that keep working, so this
provider serves accounts whose owner already holds one. Authentication is that token, sent as a Bearer token;
there is no OAuth flow, so it uses ``AuthType.SESSION`` and stores the token as the account's access token.
"""

from __future__ import annotations

import logging

from .blog import BlogProvider
from .exceptions import PublishError
from .types import (
    AccountProfile,
    AuthType,
    ConnectField,
    MediaRules,
    MediaType,
    PostSetting,
    PostType,
    PublishContent,
    PublishResult,
    RateLimitConfig,
)

logger = logging.getLogger(__name__)

API_BASE = "https://api.medium.com/v1"

# Medium ignores a title over 100 characters and every tag past the third or over 25 characters.
MAX_TITLE_LENGTH = 100
MAX_TAGS = 3
MAX_TAG_LENGTH = 25

_IMAGE_RULES = MediaRules(
    source="https://github.com/Medium/medium-api-docs",
    media_types=frozenset({"image"}),
)


class MediumProvider(BlogProvider):
    """Medium provider using a self-issued integration token (no OAuth)."""

    profile_url_template = "https://medium.com/@{handle}"

    max_tags = MAX_TAGS
    max_tag_length = MAX_TAG_LENGTH
    max_title_length = MAX_TITLE_LENGTH

    media_rules = {PostType.IMAGE: _IMAGE_RULES}

    writing_style = "A long-form reader: a clear headline, a strong opening paragraph and short sections. Tags rather than hashtags, no emoji clutter."

    connect_url_name = "connect_medium"
    supports_connection_link = False
    connect_intro = (
        "Medium no longer issues new integration tokens, so this works only if you already created one "
        "(Settings → Security and apps → Integration tokens)."
    )
    connect_notice = (
        "Posts publish as Medium stories (title + Markdown body). If your Settings page has no Integration tokens "
        "section, your account can't use Medium's API and can't be connected here."
    )
    connect_fields = (
        ConnectField(
            key="token",
            label="Integration token",
            kind="password",
            placeholder="your Medium integration token",
            help="Medium → Settings → Security and apps → Integration tokens. You can revoke it there at any time.",
        ),
    )

    post_settings = (
        PostSetting(
            key="publish_status",
            label="Visibility",
            kind="select",
            choices=(("public", "Public"), ("draft", "Draft"), ("unlisted", "Unlisted")),
            help="Public by default. A draft waits in Medium for you to finish and publish it.",
        ),
        PostSetting(
            key="publication",
            label="Publication",
            kind="select",
            options_from_provider=True,
            help="Submit the story to a publication you write for instead of your own profile.",
        ),
        PostSetting(
            key="canonical_url",
            label="Canonical URL",
            kind="text",
            placeholder="https://example.com/original-post",
            help="Where this article was first published, so search engines credit the original.",
        ),
        PostSetting(
            key="notify_followers",
            label="Notify followers",
            kind="checkbox",
            help="Email your followers when the story goes live.",
        ),
    )

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    @property
    def platform_name(self) -> str:
        return "Medium"

    @property
    def auth_type(self) -> AuthType:
        return AuthType.SESSION

    @property
    def max_caption_length(self) -> int:
        return 50000

    @property
    def supported_post_types(self) -> list[PostType]:
        return [PostType.ARTICLE, PostType.TEXT, PostType.LINK, PostType.IMAGE]

    @property
    def supported_media_types(self) -> list[MediaType]:
        return [MediaType.JPEG, MediaType.PNG, MediaType.GIF, MediaType.WEBP]

    @property
    def required_scopes(self) -> list[str]:
        return []

    @property
    def rate_limits(self) -> RateLimitConfig:
        # Medium documents no limit; stay conservative for a long-form platform.
        return RateLimitConfig(requests_per_hour=100, requests_per_day=1000, publish_per_day=25)

    @classmethod
    def credentials_for_account(cls, account) -> dict:
        # The author id is the account's platform id, so publishing needs no extra lookup.
        return {"author_id": account.account_platform_id}

    # ------------------------------------------------------------------
    # Profile
    # ------------------------------------------------------------------

    def get_profile(self, access_token: str) -> AccountProfile:
        """Validate the token and return the user it belongs to."""
        data = self._request("GET", f"{API_BASE}/me", access_token=access_token).json().get("data") or {}
        username = data.get("username", "")
        return AccountProfile(
            platform_id=str(data["id"]),
            name=data.get("name") or username,
            handle=username,
            avatar_url=data.get("imageUrl") or None,
            follower_count=0,  # not exposed by the API
            extra=data,
        )

    def connect_with_form(self, values: dict) -> tuple[AccountProfile, str]:
        token = values["token"]
        return self.get_profile(token), token

    # ------------------------------------------------------------------
    # Post settings
    # ------------------------------------------------------------------

    @classmethod
    def validate_post_settings(cls, settings: dict) -> str:
        return super().validate_post_settings(settings) or cls.validate_canonical_url(settings)

    def get_setting_options(self, access_token: str, key: str, depends_value: str = "") -> list[dict]:
        """The publications the user can submit stories to."""
        if key != "publication":
            raise NotImplementedError(f"Medium has no live options for {key}")
        author_id = self.credentials.get("author_id") or self.get_profile(access_token).platform_id
        data = self._request("GET", f"{API_BASE}/users/{author_id}/publications", access_token=access_token).json()
        return [{"id": item["id"], "name": item.get("name") or item["id"]} for item in data.get("data") or []]

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    def publish_post(self, access_token: str, content: PublishContent) -> PublishResult:
        """Publish a story with POST /users/{id}/posts (or /publications/{id}/posts)."""
        title = self._require_title(content)
        extra = content.extra
        error = self.validate_post_settings(extra)
        if error:
            raise PublishError(error, platform=self.platform_name, retryable=False)

        # Medium takes no cover field: images in the Markdown are imported from their URLs.
        images = "".join(f"![]({url})\n\n" for url in self._image_urls(content))
        story: dict = {
            "title": title,
            "contentFormat": "markdown",
            "content": f"# {title}\n\n{images}{self._body(content)}",
            "publishStatus": extra.get("publish_status") or "public",
        }
        tags = self._tags(content)
        if tags:
            story["tags"] = tags
        canonical = self._canonical_url(content)
        if canonical:
            story["canonicalUrl"] = canonical
        if extra.get("notify_followers"):
            story["notifyFollowers"] = True

        publication = str(extra.get("publication") or "")
        if publication:
            path = f"/publications/{publication}/posts"
        else:
            author_id = self.credentials.get("author_id") or self.get_profile(access_token).platform_id
            path = f"/users/{author_id}/posts"
        data = (
            self._request("POST", f"{API_BASE}{path}", access_token=access_token, json=story).json().get("data") or {}
        )
        if not data.get("id"):
            raise PublishError(
                "Medium accepted the request but returned no story id",
                platform=self.platform_name,
                raw_response=data,
            )
        return PublishResult(platform_post_id=str(data["id"]), url=data.get("url"), extra=data)
