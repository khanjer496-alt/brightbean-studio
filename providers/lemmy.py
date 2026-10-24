"""Lemmy provider: link, image and text posts to a community on the user's own instance.

Lemmy is federated, so each account names its instance (``SocialAccount.instance_url``) and
signs in with a username and password. Only the JWT the instance returns is kept (encrypted,
like every other token); the password is used once at connect time and never stored. A JWT
the instance revokes (the user logged out everywhere, changed their password) shows up as a
rejected credential and the account asks to be reconnected.

Documentation checked 2026-10-24 (the v3 routes also work on Lemmy 1.0, where they are kept
compatible, and on the 0.19 releases most instances still run):

- API reference: https://join-lemmy.org/api/ and the typed client
  https://join-lemmy.org/lemmy-js-client-docs/main/ (``Login``, ``GetSite``,
  ``GetCommunity``, ``CreatePost``, ``CreateComment``, ``DeletePost``).
- Breaking changes in 1.0 (v3 stays available): https://join-lemmy.org/news/2025-02-03_-_Breaking_Changes_in_Lemmy_1.0
- Limits: titles up to 200 characters, bodies up to 10,000 (the server's defaults).
"""

from __future__ import annotations

import contextlib
import logging
import re
from urllib.parse import urlsplit

from apps.common.validators import is_safe_url

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

MAX_TITLE_LENGTH = 200
MAX_BODY_LENGTH = 10000

# ``name`` or ``name@instance``, as Lemmy's GetCommunity accepts it.
_COMMUNITY = re.compile(r"[A-Za-z0-9_]{1,100}(@[A-Za-z0-9.-]+\.[A-Za-z]{2,})?")

# Error codes Lemmy returns in ``{"error": ...}`` when the login is no longer good.
_AUTH_ERRORS = frozenset(
    {"not_logged_in", "incorrect_login", "invalid_token", "expired_token", "login_token_not_found"}
)

_LEMMY_DOCS = "https://join-lemmy.org/lemmy-js-client-docs/main/types/CreatePost.html"
LEMMY_MEDIA_RULES = {
    # The image is shared as the post's link, so one picture per post.
    PostType.IMAGE: MediaRules(source=_LEMMY_DOCS, max_items=1),
}


def normalize_instance_url(raw: str) -> str:
    """``scheme://host[:port]`` from a typed instance address (a bare host, a URL, or ``user@host``), or ""."""
    value = (raw or "").strip()
    if not value:
        return ""
    if "://" not in value and "@" in value:
        value = value.split("@")[-1]
    if "://" not in value:
        value = f"https://{value}"
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return ""
    return f"{parts.scheme}://{parts.netloc}"


def normalize_community(raw) -> str:
    """The community in ``name`` or ``name@instance`` form (``!`` and ``c/`` prefixes dropped), or ""."""
    value = str(raw or "").strip().lstrip("!")
    value = value.removeprefix("/c/").removeprefix("c/")
    return value if _COMMUNITY.fullmatch(value) else ""


class LemmyProvider(SocialProvider):
    """Lemmy provider (API v3) on the instance named in the credentials' ``instance_url``."""

    works_without_platform_app = True
    connect_url_name = "connect_lemmy"
    accepts_media = True
    needs_local_media = False
    supports_connection_link = False
    media_rules = LEMMY_MEDIA_RULES

    # Later parts are comments on the first post.
    thread = ThreadCapability(
        mode=ThreadMode.COMMENTS, max_parts=10, part_max_length=MAX_BODY_LENGTH, media_per_part=False
    )

    writing_style = (
        "Plain, community-minded and specific to the community's topic. A clear descriptive title carries the "
        "post; the body adds context in Markdown. No hashtags, no sales tone."
    )

    post_settings = (
        PostSetting(
            key="community",
            label="Community",
            kind="text",
            required=True,
            max_length=200,
            placeholder="e.g. selfhosted or selfhosted@lemmy.world",
            help="The community to post to. Add @instance for a community on another instance.",
        ),
        PostSetting(key="nsfw", label="Mark as NSFW", kind="checkbox"),
    )
    account_default_keys = ("community", "nsfw")
    account_default_choices = {"nsfw": (True, False)}

    def __init__(self, credentials: dict | None = None):
        super().__init__(credentials)
        self.instance_url: str = (self.credentials.get("instance_url") or "").rstrip("/")

    @classmethod
    def credentials_for_account(cls, account) -> dict:
        url = account.instance_url
        if not url:
            return {}
        if not is_safe_url(url):
            logger.warning("Lemmy instance URL failed SSRF check for account %s", account.id)
            return {}
        return {"instance_url": url}

    @classmethod
    def permalink_for(cls, platform_post_id: str, account) -> str:
        post_id = (platform_post_id or "").strip()
        base = (account.instance_url or "").rstrip("/")
        return f"{base}/post/{post_id}" if post_id.isdigit() and base else ""

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    @property
    def platform_name(self) -> str:
        return "Lemmy"

    @property
    def auth_type(self) -> AuthType:
        return AuthType.SESSION

    @property
    def max_caption_length(self) -> int:
        return MAX_BODY_LENGTH

    @property
    def supported_post_types(self) -> list[PostType]:
        return [PostType.TEXT, PostType.LINK, PostType.IMAGE]

    @property
    def supported_media_types(self) -> list[MediaType]:
        return [MediaType.JPEG, MediaType.PNG, MediaType.GIF, MediaType.WEBP]

    @property
    def required_scopes(self) -> list[str]:
        return []

    @property
    def rate_limits(self) -> RateLimitConfig:
        # Instances default to 6 posts per 10 minutes and 180 other actions a minute per user.
        return RateLimitConfig(requests_per_hour=3000, requests_per_day=20000, publish_per_day=100)

    # ------------------------------------------------------------------
    # Requests
    # ------------------------------------------------------------------

    def _api(self, method: str, path: str, access_token: str | None = None, **kwargs):
        if not self.instance_url or not is_safe_url(self.instance_url):
            raise PublishError(
                "This Lemmy instance address is missing or not allowed. Reconnect the account.",
                platform=self.platform_name,
                retryable=False,
            )
        return self._request(method, f"{self.instance_url}/api/v3{path}", access_token=access_token, **kwargs)

    def _error_for_response(self, response):
        code = ""
        with contextlib.suppress(Exception):
            code = str((response.json() or {}).get("error") or "")
        if code == "rate_limit_error":
            return RateLimitError(
                "Lemmy rate limit reached for this instance", platform=self.platform_name, raw_response={"error": code}
            )
        if code in _AUTH_ERRORS:
            return APIError(
                "Lemmy rejected the login. Reconnect the account.",
                status_code=401,
                platform=self.platform_name,
                raw_response={"error": code},
            )
        return super()._error_for_response(response)

    # ------------------------------------------------------------------
    # Connect
    # ------------------------------------------------------------------

    def login(self, username_or_email: str, password: str, totp_token: str = "") -> OAuthTokens:
        """Sign in once and keep only the JWT. The password is not retained anywhere."""
        body = {"username_or_email": username_or_email, "password": password}
        if totp_token:
            body["totp_2fa_token"] = totp_token
        data = self._api("POST", "/user/login", json=body).json()
        jwt = data.get("jwt")
        if not jwt:
            hint = "needs a verified email" if data.get("verify_email_sent") else "returned no login token"
            raise OAuthError(f"Lemmy sign-in {hint}", platform=self.platform_name, retryable=False)
        return OAuthTokens(access_token=jwt)

    def get_profile(self, access_token: str) -> AccountProfile:
        data = self._api("GET", "/site", access_token=access_token).json()
        person = (((data.get("my_user") or {}).get("local_user_view") or {}).get("person")) or {}
        if not person.get("id"):
            raise OAuthError("Lemmy did not return the signed-in user", platform=self.platform_name)
        host = urlsplit(self.instance_url).hostname or ""
        name = person.get("name", "")
        return AccountProfile(
            platform_id=f"{person['id']}@{host}",
            name=person.get("display_name") or name,
            handle=f"{name}@{host}" if name and host else name,
            avatar_url=person.get("avatar"),
            extra={"person": person},
        )

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    @classmethod
    def validate_post_settings(cls, settings: dict) -> str:
        error = super().validate_post_settings(settings)
        if error:
            return error
        if not normalize_community((settings or {}).get("community")):
            return "Community must be a name like selfhosted, or selfhosted@lemmy.world."
        return ""

    def _community_id(self, access_token: str, community: str) -> int:
        data = self._api("GET", "/community", access_token=access_token, params={"name": community}).json()
        community_id = ((data.get("community_view") or {}).get("community") or {}).get("id")
        if not community_id:
            raise PublishError(
                f"Lemmy can't find the community {community}.", platform=self.platform_name, retryable=False
            )
        return int(community_id)

    def publish_post(self, access_token: str, content: PublishContent) -> PublishResult:
        extra = content.extra
        title = (content.title or "").strip()
        if not title:
            raise PublishError(
                "Lemmy requires a title. Set the post title before publishing.",
                platform=self.platform_name,
                retryable=False,
            )
        if len(title) > MAX_TITLE_LENGTH:
            raise PublishError(
                f"Lemmy titles are limited to {MAX_TITLE_LENGTH} characters.",
                platform=self.platform_name,
                retryable=False,
            )
        if len(content.text or "") > MAX_BODY_LENGTH:
            raise PublishError(
                f"Lemmy posts are limited to {MAX_BODY_LENGTH} characters.",
                platform=self.platform_name,
                retryable=False,
            )
        community = normalize_community(extra.get("community"))
        if not community:
            raise PublishError(
                "Lemmy needs a community. Set one for this post in the composer.",
                platform=self.platform_name,
                retryable=False,
            )
        payload: dict = {
            "community_id": self._community_id(access_token, community),
            "name": title,
            "nsfw": bool(extra.get("nsfw")),
        }
        if content.text:
            payload["body"] = content.text
        # A link wins; otherwise the attached picture is the post's link.
        link = content.link_url or (content.media_urls[0] if content.media_urls else "")
        if link:
            payload["url"] = link

        data = self._api("POST", "/post", access_token=access_token, json=payload).json()
        post = (data.get("post_view") or {}).get("post") or {}
        if not post.get("id"):
            raise APIError(
                "Lemmy accepted the post but returned no id",
                platform=self.platform_name,
                raw_response=data,
                ambiguous_write=True,
            )
        return PublishResult(
            platform_post_id=str(post["id"]),
            url=post.get("ap_id") or f"{self.instance_url}/post/{post['id']}",
            extra={"community": community},
        )

    def publish_comment(self, access_token: str, post_id: str, text: str) -> CommentResult:
        """Comment on a post (also how later thread parts are posted)."""
        if len(text) > MAX_BODY_LENGTH:
            raise PublishError(
                f"Lemmy comments are limited to {MAX_BODY_LENGTH} characters.",
                platform=self.platform_name,
                retryable=False,
            )
        data = self._api(
            "POST", "/comment", access_token=access_token, json={"post_id": int(post_id), "content": text}
        ).json()
        comment = (data.get("comment_view") or {}).get("comment") or {}
        if not comment.get("id"):
            raise APIError(
                "Lemmy accepted the comment but returned no id",
                platform=self.platform_name,
                raw_response=data,
                ambiguous_write=True,
            )
        return CommentResult(
            platform_comment_id=str(comment["id"]), extra={"url": f"{self.instance_url}/comment/{comment['id']}"}
        )

    def delete_post(self, access_token: str, post_id: str) -> bool:
        self._api("POST", "/post/delete", access_token=access_token, json={"post_id": int(post_id), "deleted": True})
        return True
