"""Tumblr API v2 provider: Neue Post Format (NPF) posts with text, images and video.

Documentation checked 2026-10-24:

- OAuth 2.0 (authorize ``https://www.tumblr.com/oauth2/authorize``, token
  ``https://api.tumblr.com/v2/oauth2/token``, scopes ``basic write offline_access``;
  the refresh token only comes back when ``offline_access`` was requested):
  https://www.tumblr.com/docs/en/api/v2#oauth2-authorization
- ``POST /v2/blog/{blog}/posts`` (NPF create): ``content``, ``state``, ``tags``
  (comma-separated), multipart media parts named by the media object's ``identifier``;
  answers 201 with the post ``id`` as a string:
  https://www.tumblr.com/docs/en/api/v2#posts---createreblog-a-post-neue-post-format
- Content blocks: https://github.com/tumblr/docs/blob/master/npf-spec.md
- ``GET /v2/user/info`` lists the user's blogs; ``POST /v2/blog/{blog}/post/delete`` deletes.
- Limits: 250 new posts and 250 images a day per user, 20 videos a day; photos 10 MB,
  video 500 MB and 10 minutes.

One account is one blog: the connect flow lists the blogs the user owns (destination picker)
and each becomes its own account, all sharing the one grant.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
from contextlib import ExitStack
from urllib.parse import urlencode

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
)

logger = logging.getLogger(__name__)

AUTH_URL = "https://www.tumblr.com/oauth2/authorize"
API_BASE = "https://api.tumblr.com/v2"
TOKEN_URL = f"{API_BASE}/oauth2/token"
SCOPES = ["basic", "write", "offline_access"]

MAX_TEXT_LENGTH = 4096
MAX_TAGS = 30

_TUMBLR_DOCS = "https://www.tumblr.com/docs/en/api/v2"
TUMBLR_MEDIA_RULES = {
    PostType.IMAGE: MediaRules(source=_TUMBLR_DOCS, max_image_bytes=10 * 1024 * 1024),
    PostType.VIDEO: MediaRules(source=_TUMBLR_DOCS, max_video_bytes=500 * 1024 * 1024, max_duration=10 * 60),
}


def parse_tags(raw) -> list[str]:
    """Tags from a comma-separated string, without ``#``, blanks and repeats."""
    tags: list[str] = []
    for part in str(raw or "").replace("\n", ",").split(","):
        tag = part.strip().lstrip("#").strip()
        if tag and tag.lower() not in {t.lower() for t in tags}:
            tags.append(tag)
    return tags[:MAX_TAGS]


class TumblrProvider(SocialProvider):
    """Tumblr provider: one account per blog, OAuth 2.0 with a refreshable grant."""

    profile_url_template = "https://{handle}.tumblr.com"

    media_rules = TUMBLR_MEDIA_RULES
    has_destination_picker = True
    no_destinations_hint = "Tumblr returned no blogs for this login."
    revokes_grant_on_disconnect = False

    writing_style = (
        "Personal and a little playful, in the blog's own voice. A short title or opening line, then the post. "
        "Tags carry the topics instead of inline hashtags."
    )

    post_settings = (
        PostSetting(
            key="tags",
            label="Tags",
            kind="text",
            max_length=500,
            placeholder="photography, travel",
            help="Separated by commas, without #. They are how people find a post on Tumblr.",
        ),
        PostSetting(
            key="state",
            label="Publish as",
            kind="select",
            choices=(("published", "Published"), ("draft", "Draft"), ("private", "Private")),
            help="Draft and private posts stay out of the public blog until you publish them in Tumblr.",
        ),
    )
    account_default_keys = ("tags", "state")
    account_default_choices = {"state": ("published", "draft", "private")}

    @classmethod
    def credentials_for_account(cls, account) -> dict:
        # The blog is the account: its short name is the blog identifier every call needs.
        return {"blog": account.account_handle}

    @classmethod
    def permalink_for(cls, platform_post_id: str, account) -> str:
        post_id = (platform_post_id or "").strip()
        handle = (account.account_handle or "").strip()
        if not (post_id.isdigit() and handle.replace("-", "").isalnum()):
            return ""
        return f"https://{handle}.tumblr.com/post/{post_id}"

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    @property
    def platform_name(self) -> str:
        return "Tumblr"

    @property
    def auth_type(self) -> AuthType:
        return AuthType.OAUTH2

    @property
    def max_caption_length(self) -> int:
        return MAX_TEXT_LENGTH

    @property
    def supported_post_types(self) -> list[PostType]:
        return [PostType.TEXT, PostType.IMAGE, PostType.VIDEO, PostType.LINK]

    @property
    def supported_media_types(self) -> list[MediaType]:
        return [MediaType.JPEG, MediaType.PNG, MediaType.GIF, MediaType.WEBP, MediaType.MP4, MediaType.MOV]

    @property
    def required_scopes(self) -> list[str]:
        return list(SCOPES)

    @property
    def rate_limits(self) -> RateLimitConfig:
        # Per consumer key: 1,000 an hour and 5,000 a day; per user: 250 new posts a day.
        return RateLimitConfig(requests_per_hour=1000, requests_per_day=5000, publish_per_day=250)

    # ------------------------------------------------------------------
    # OAuth
    # ------------------------------------------------------------------

    def get_auth_url(self, redirect_uri: str, state: str, code_verifier: str | None = None) -> str:
        params = {
            "client_id": self.credentials["client_id"],
            "response_type": "code",
            "scope": " ".join(self.required_scopes),
            "state": state,
            "redirect_uri": redirect_uri,
        }
        return f"{AUTH_URL}?{urlencode(params)}"

    def _token_request(self, form: dict, what: str) -> OAuthTokens:
        resp = self._request(
            "POST",
            TOKEN_URL,
            data={
                **form,
                "client_id": self.credentials["client_id"],
                "client_secret": self.credentials["client_secret"],
            },
        )
        body = resp.json()
        if "access_token" not in body:
            # The body goes in raw_response: it can echo request details.
            raise OAuthError(f"Tumblr {what} failed", platform=self.platform_name, raw_response=body)
        return OAuthTokens(
            access_token=body["access_token"],
            refresh_token=body.get("refresh_token"),
            expires_in=body.get("expires_in"),
            scope=body.get("scope"),
            raw_response=body,
        )

    def exchange_code(self, code: str, redirect_uri: str, code_verifier: str | None = None) -> OAuthTokens:
        return self._token_request(
            {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri}, "token exchange"
        )

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

    # ------------------------------------------------------------------
    # Profile and blogs
    # ------------------------------------------------------------------

    def _blogs(self, access_token: str) -> list[dict]:
        data = self._request("GET", f"{API_BASE}/user/info", access_token=access_token).json()
        return ((data.get("response") or {}).get("user") or {}).get("blogs") or []

    @staticmethod
    def _blog_profile(blog: dict) -> AccountProfile:
        name = blog.get("name", "")
        return AccountProfile(
            platform_id=str(blog.get("uuid") or name),
            name=blog.get("title") or name,
            handle=name,
            avatar_url=f"{API_BASE}/blog/{name}.tumblr.com/avatar/128" if name else None,
            follower_count=int(blog.get("followers") or 0),
            extra=blog,
        )

    def get_profile(self, access_token: str) -> AccountProfile:
        """The primary blog of the login (the account itself is picked blog by blog)."""
        blogs = self._blogs(access_token)
        if not blogs:
            raise OAuthError("Tumblr returned no blogs for this login", platform=self.platform_name)
        primary = next((blog for blog in blogs if blog.get("primary")), blogs[0])
        return self._blog_profile(primary)

    def list_destinations(self, tokens: OAuthTokens) -> list[dict]:
        """Every blog the login owns; each carries the grant's tokens so it can refresh on its own."""
        rows = []
        for blog in self._blogs(tokens.access_token):
            profile = self._blog_profile(blog)
            rows.append(
                {
                    "id": profile.platform_id,
                    "name": profile.name,
                    "handle": profile.handle,
                    "picture": profile.avatar_url or "",
                    "followers_count": profile.follower_count,
                    "access_token": tokens.access_token,
                    "refresh_token": tokens.refresh_token,
                    "expires_in": tokens.expires_in,
                }
            )
        return rows

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    def _blog(self) -> str:
        blog = (self.credentials.get("blog") or "").strip()
        if not blog:
            raise PublishError(
                "This Tumblr account has no blog attached. Reconnect it.", platform=self.platform_name, retryable=False
            )
        return blog

    @staticmethod
    def _text_blocks(text: str) -> list[dict]:
        return [{"type": "text", "text": para.strip()} for para in (text or "").split("\n\n") if para.strip()]

    def _build_post(self, content: PublishContent, uploads: list[tuple[str, str, str]]) -> dict:
        """The NPF body. ``uploads`` is ``[(identifier, kind, mime type)]`` for the files sent alongside it."""
        blocks: list[dict] = []
        if content.title:
            blocks.append({"type": "text", "subtype": "heading1", "text": content.title.strip()})
        blocks.extend(self._text_blocks(content.text or ""))
        if content.link_url:
            blocks.append({"type": "link", "url": content.link_url})
        for index, (identifier, kind, mime) in enumerate(uploads):
            if kind == "video":
                blocks.append({"type": "video", "media": {"type": mime, "identifier": identifier}})
            else:
                block: dict = {"type": "image", "media": [{"type": mime, "identifier": identifier}]}
                alt = (content.extra.get("alt_text") or "").strip() if index == 0 else ""
                if alt:
                    block["alt_text"] = alt[:4096]
                blocks.append(block)
        post: dict = {"content": blocks, "state": content.extra.get("state") or "published"}
        tags = parse_tags(content.extra.get("tags"))
        if tags:
            post["tags"] = ",".join(tags)
        return post

    def publish_post(self, access_token: str, content: PublishContent) -> PublishResult:
        blog = self._blog()
        if len(content.text or "") > MAX_TEXT_LENGTH:
            raise PublishError(
                f"Tumblr text is limited to {MAX_TEXT_LENGTH} characters here.",
                platform=self.platform_name,
                retryable=False,
            )
        if not (content.text or content.title or content.link_url or content.media_files):
            raise PublishError(
                "Tumblr needs some text, a link or media to post.", platform=self.platform_name, retryable=False
            )

        uploads = [
            (
                f"media-{index}",
                "video" if content.is_video(index) else "image",
                mimetypes.guess_type(path)[0] or "application/octet-stream",
            )
            for index, path in enumerate(content.media_files)
        ]
        post = self._build_post(content, uploads)

        url = f"{API_BASE}/blog/{blog}.tumblr.com/posts"
        if uploads:
            with ExitStack() as stack:
                files: dict = {"json": (None, json.dumps(post), "application/json")}
                for (identifier, _kind, mime), path in zip(uploads, content.media_files, strict=True):
                    files[identifier] = (os.path.basename(path), stack.enter_context(open(path, "rb")), mime)
                resp = self._request("POST", url, access_token=access_token, files=files, timeout=300)
        else:
            resp = self._request("POST", url, access_token=access_token, json=post)

        data = resp.json()
        post_id = str(((data.get("response") or {}).get("id")) or "")
        if not post_id:
            raise APIError(
                "Tumblr accepted the post but returned no id",
                platform=self.platform_name,
                raw_response=data,
                ambiguous_write=True,
            )
        return PublishResult(platform_post_id=post_id, url=f"https://{blog}.tumblr.com/post/{post_id}", extra=data)

    def delete_post(self, access_token: str, post_id: str) -> bool:
        blog = self._blog()
        self._request(
            "POST", f"{API_BASE}/blog/{blog}.tumblr.com/post/delete", access_token=access_token, data={"id": post_id}
        )
        return True
