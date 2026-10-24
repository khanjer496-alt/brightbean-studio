"""Hashnode provider implementation.

Hashnode's public API is GraphQL (https://gql.hashnode.com). Authentication is a personal access token
(Account settings → Developer), sent raw in the ``Authorization`` header; there is no OAuth flow, so this
provider uses ``AuthType.SESSION`` and stores the token as the account's access token. Hashnode requires the
publication to be on a Pro plan for API access.
"""

from __future__ import annotations

import logging
import re

from .blog import BlogProvider
from .exceptions import APIError, PublishError
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

API_URL = "https://gql.hashnode.com"

MAX_TAGS = 5
MAX_TITLE_LENGTH = 250
MAX_SUBTITLE_LENGTH = 150

_COVER_RULES = MediaRules(
    source="https://apidocs.hashnode.com/",
    media_types=frozenset({"image"}),
    # Only the first image is sent, as the cover.
    published_items=1,
)

_PUBLICATIONS_QUERY = "query { me { publications(first: 50) { edges { node { id title } } } } }"
_ME_QUERY = "query { me { id name username profilePicture } }"
_PUBLISH_MUTATION = "mutation PublishPost($input: PublishPostInput!) { publishPost(input: $input) { post { id url } } }"


def tag_slug(name: str) -> str:
    """``Machine Learning`` as ``machine-learning``, the slug Hashnode keys a tag by."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


class HashnodeProvider(BlogProvider):
    """Hashnode provider using a personal access token (no OAuth)."""

    profile_url_template = "https://hashnode.com/@{handle}"

    max_tags = MAX_TAGS
    max_title_length = MAX_TITLE_LENGTH

    media_rules = {PostType.IMAGE: _COVER_RULES}

    writing_style = "A developer audience: a clear, specific headline, a short hook and practical sections with code where it helps. Tags rather than hashtags."

    connect_url_name = "connect_hashnode"
    supports_connection_link = False
    connect_intro = "Hashnode authenticates with a personal access token from Account settings → Developer."
    connect_notice = (
        "Hashnode only lets its API publish to publications on a Pro plan. Posts publish as articles "
        "(title + Markdown body) to the publication you pick in the composer. You can revoke the token at any time."
    )
    connect_fields = (
        ConnectField(
            key="token",
            label="Personal access token",
            kind="password",
            placeholder="your Hashnode access token",
            help="Hashnode → Account settings → Developer → Generate new token.",
        ),
    )

    post_settings = (
        PostSetting(
            key="publication",
            label="Publication",
            kind="select",
            options_from_provider=True,
            help="Where the article is published. Optional when you have a single publication.",
        ),
        PostSetting(key="subtitle", label="Subtitle", kind="text", max_length=MAX_SUBTITLE_LENGTH),
        PostSetting(
            key="canonical_url",
            label="Canonical URL",
            kind="text",
            placeholder="https://example.com/original-post",
            help="Where this article was first published, so search engines credit the original.",
        ),
    )

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    @property
    def platform_name(self) -> str:
        return "Hashnode"

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
        return RateLimitConfig(requests_per_hour=500, requests_per_day=5000, publish_per_day=50)

    # ------------------------------------------------------------------
    # GraphQL
    # ------------------------------------------------------------------

    def _gql(self, access_token: str, query: str, variables: dict | None = None) -> dict:
        """Run one query or mutation and return its ``data``.

        GraphQL answers errors with HTTP 200 and an ``errors`` array, so the body is checked too.
        """
        resp = self._request(
            "POST",
            API_URL,
            headers={"Authorization": access_token, "Content-Type": "application/json"},
            json={"query": query, **({"variables": variables} if variables else {})},
        )
        body = self._safe_json(resp)
        errors = body.get("errors") or []
        if errors:
            code = str((errors[0].get("extensions") or {}).get("code", ""))
            message = str(errors[0].get("message", ""))[:300]
            if code in ("UNAUTHENTICATED", "FORBIDDEN"):
                raise PublishError(
                    "Hashnode rejected the access token, or the publication isn't on a Pro plan "
                    "(the API requires one). Check the token and the plan.",
                    platform=self.platform_name,
                    retryable=False,
                    raw_response=body,
                )
            raise PublishError(f"Hashnode error: {message}", platform=self.platform_name, raw_response=body)
        return body.get("data") or {}

    # ------------------------------------------------------------------
    # Profile
    # ------------------------------------------------------------------

    def get_profile(self, access_token: str) -> AccountProfile:
        """Validate the token and return the user it belongs to."""
        me = self._gql(access_token, _ME_QUERY).get("me")
        if not me:
            raise PublishError("Hashnode rejected the access token.", platform=self.platform_name, retryable=False)
        return AccountProfile(
            platform_id=str(me["id"]),
            name=me.get("name") or me.get("username", ""),
            handle=me.get("username"),
            avatar_url=me.get("profilePicture") or None,
            follower_count=0,
            extra=me,
        )

    def connect_with_form(self, values: dict) -> tuple[AccountProfile, str]:
        token = values["token"]
        try:
            return self.get_profile(token), token
        except APIError as exc:
            if exc.status_code in (401, 403):
                raise PublishError(
                    "Hashnode rejected the access token.", platform=self.platform_name, retryable=False
                ) from exc
            raise

    # ------------------------------------------------------------------
    # Post settings
    # ------------------------------------------------------------------

    @classmethod
    def validate_post_settings(cls, settings: dict) -> str:
        return super().validate_post_settings(settings) or cls.validate_canonical_url(settings)

    def _publications(self, access_token: str) -> list[dict]:
        me = self._gql(access_token, _PUBLICATIONS_QUERY).get("me") or {}
        edges = (me.get("publications") or {}).get("edges") or []
        return [{"id": edge["node"]["id"], "name": edge["node"].get("title") or edge["node"]["id"]} for edge in edges]

    def get_setting_options(self, access_token: str, key: str, depends_value: str = "") -> list[dict]:
        """The publications the user can publish to."""
        if key != "publication":
            raise NotImplementedError(f"Hashnode has no live options for {key}")
        return self._publications(access_token)

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    def publish_post(self, access_token: str, content: PublishContent) -> PublishResult:
        """Publish an article with the publishPost mutation."""
        title = self._require_title(content)
        extra = content.extra
        error = self.validate_post_settings(extra)
        if error:
            raise PublishError(error, platform=self.platform_name, retryable=False)

        publication = str(extra.get("publication") or "")
        if not publication:
            options = self._publications(access_token)
            if len(options) != 1:
                raise PublishError(
                    "Choose which Hashnode publication to publish to in the composer."
                    if options
                    else "This Hashnode account has no publication to publish to.",
                    platform=self.platform_name,
                    retryable=False,
                )
            publication = options[0]["id"]

        article: dict = {
            "title": title,
            "contentMarkdown": self._body(content),
            "publicationId": publication,
        }
        if extra.get("subtitle"):
            article["subtitle"] = self._require_fit("The subtitle", str(extra["subtitle"]), MAX_SUBTITLE_LENGTH)
        canonical = self._canonical_url(content)
        if canonical:
            article["originalArticleURL"] = canonical
        tags = [{"name": tag, "slug": tag_slug(tag)} for tag in self._tags(content) if tag_slug(tag)]
        if tags:
            article["tags"] = tags
        images = self._image_urls(content)
        if images:
            article["coverImageOptions"] = {"coverImageURL": images[0]}

        post = (self._gql(access_token, _PUBLISH_MUTATION, {"input": article}).get("publishPost") or {}).get(
            "post"
        ) or {}
        if not post.get("id"):
            raise PublishError(
                "Hashnode accepted the request but returned no article id",
                platform=self.platform_name,
                raw_response=post,
            )
        return PublishResult(platform_post_id=str(post["id"]), url=post.get("url"), extra=post)
