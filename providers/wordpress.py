"""WordPress (self-hosted) provider implementation.

Talks to the site's own REST API (``/wp-json/wp/v2``) with a user's Application Password over HTTP Basic auth
(https://developer.wordpress.org/rest-api/using-the-rest-api/authentication/); there is no OAuth flow, so this
provider uses ``AuthType.SESSION`` and stores the application password as the account's access token. The site
address is the account's ``instance_url`` and the username lives in ``platform_data``. WordPress.com sites need
a separate OAuth integration and aren't covered.
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import os
from urllib.parse import urlsplit

import markdown

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

MAX_TAGS = 10
MAX_TITLE_LENGTH = 255

_FEATURED_IMAGE_RULES = MediaRules(
    source="https://developer.wordpress.org/rest-api/reference/media/",
    media_types=frozenset({"image"}),
    # Only the first image is uploaded, as the featured image.
    published_items=1,
)


def markdown_to_html(text: str) -> str:
    """The post body as the HTML WordPress stores."""
    return markdown.markdown(text or "", extensions=["fenced_code", "tables", "sane_lists"])


class WordPressProvider(BlogProvider):
    """Self-hosted WordPress provider using an Application Password (no OAuth)."""

    max_tags = MAX_TAGS
    max_title_length = MAX_TITLE_LENGTH

    # The featured image is uploaded from the local file.
    needs_local_media = True

    media_rules = {PostType.IMAGE: _FEATURED_IMAGE_RULES}

    writing_style = "A blog reader: a descriptive headline, a clear opening and well-structured sections with subheadings. Tags rather than hashtags."

    connect_url_name = "connect_wordpress"
    supports_connection_link = False
    connect_intro = (
        "Connect a self-hosted WordPress site (5.6 or newer, served over HTTPS) with an Application Password."
    )
    connect_notice = (
        "Posts publish through your site's REST API as the user you enter. Create the password under "
        "Users → Profile → Application Passwords; you can revoke it there at any time. "
        "WordPress.com sites aren't supported yet."
    )
    connect_url_field = "site_url"
    account_url_credential = "site_url"
    connect_fields = (
        ConnectField(
            key="site_url",
            label="Site address",
            kind="url",
            placeholder="https://example.com",
            help="The address of your WordPress site, as visitors see it (without /wp-admin).",
        ),
        ConnectField(key="username", label="Username", placeholder="your WordPress username"),
        ConnectField(
            key="app_password",
            label="Application Password",
            kind="password",
            placeholder="xxxx xxxx xxxx xxxx xxxx xxxx",
            help="Users → Profile → Application Passwords. Spaces are fine.",
        ),
    )

    post_settings = (
        PostSetting(
            key="status",
            label="Status",
            kind="select",
            choices=(("publish", "Publish"), ("draft", "Draft"), ("pending", "Pending review"), ("private", "Private")),
            help="Publish by default. A draft waits in WordPress for you to finish it.",
        ),
        PostSetting(key="category", label="Category", kind="select", options_from_provider=True),
    )

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    @property
    def platform_name(self) -> str:
        return "WordPress"

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
        # Set by the site's own host; stay modest so a small shared host isn't hammered.
        return RateLimitConfig(requests_per_hour=200, requests_per_day=2000, publish_per_day=50)

    @classmethod
    def credentials_for_account(cls, account) -> dict:
        return {"username": (account.platform_data or {}).get("username", "")}

    @classmethod
    def profile_url(cls, account) -> str:
        return account.instance_url if account.instance_url.startswith("https://") else ""

    # ------------------------------------------------------------------
    # Requests
    # ------------------------------------------------------------------

    @property
    def site_url(self) -> str:
        url = self.credentials.get("site_url", "")
        if not url:
            raise PublishError(
                "This WordPress account has no usable site address. Reconnect it.",
                platform=self.platform_name,
                retryable=False,
            )
        return url

    def _auth_headers(self, access_token: str, username: str | None = None) -> dict:
        user = username if username is not None else self.credentials.get("username", "")
        basic = base64.b64encode(f"{user}:{access_token}".encode()).decode()
        return {"Authorization": f"Basic {basic}", "Accept": "application/json"}

    def _wp(self, method: str, path: str, access_token: str, *, username: str | None = None, **kwargs):
        """One call to ``/wp-json{path}``, answered as JSON (the site's HTML error pages are refused)."""
        headers = {**self._auth_headers(access_token, username), **(kwargs.pop("headers", None) or {})}
        resp = self._request(method, f"{self.site_url}/wp-json{path}", headers=headers, **kwargs)
        try:
            return resp.json()
        except ValueError as exc:
            raise PublishError(
                "The site didn't answer with WordPress REST data. Check the address, and that the REST API "
                "isn't blocked or redirected.",
                platform=self.platform_name,
                retryable=False,
            ) from exc

    # ------------------------------------------------------------------
    # Profile
    # ------------------------------------------------------------------

    def get_profile(self, access_token: str) -> AccountProfile:
        """Validate the application password and return the user it signs in as."""
        user = self._wp("GET", "/wp/v2/users/me", access_token, params={"context": "edit"})
        site_name = self._site_name()
        host = urlsplit(self.site_url).netloc
        avatars = user.get("avatar_urls") or {}
        return AccountProfile(
            platform_id=f"{host}:{user['id']}",
            name=site_name or host,
            handle=user.get("slug") or self.credentials.get("username", ""),
            avatar_url=avatars.get("96") or None,
            follower_count=0,
            extra={
                "account_data": {"username": self.credentials.get("username", "")},
                "can_publish": bool((user.get("capabilities") or {}).get("publish_posts", True)),
            },
        )

    def _site_name(self) -> str:
        """The site's title from the REST index (public), or "" when it can't be read."""
        try:
            return str(
                self._request("GET", f"{self.site_url}/wp-json", params={"_fields": "name"}).json().get("name") or ""
            )
        except Exception:
            return ""

    def connect_with_form(self, values: dict) -> tuple[AccountProfile, str]:
        self.credentials = {**self.credentials, "site_url": values["site_url"], "username": values["username"]}
        password = values["app_password"]
        try:
            profile = self.get_profile(password)
        except APIError as exc:
            if exc.status_code in (401, 403):
                raise PublishError(
                    "WordPress rejected the login. Check the username and Application Password. A security "
                    "plugin or server setting that blocks the REST API or strips the Authorization header "
                    "can cause this too.",
                    platform=self.platform_name,
                    retryable=False,
                ) from exc
            if exc.status_code == 404:
                raise PublishError(
                    "That address doesn't have a WordPress REST API. Check the site address.",
                    platform=self.platform_name,
                    retryable=False,
                ) from exc
            raise
        if not profile.extra.get("can_publish"):
            raise PublishError(
                "That WordPress user can't publish posts. Use an Author, Editor or Administrator.",
                platform=self.platform_name,
                retryable=False,
            )
        return profile, password

    # ------------------------------------------------------------------
    # Post settings
    # ------------------------------------------------------------------

    def get_setting_options(self, access_token: str, key: str, depends_value: str = "") -> list[dict]:
        """The site's categories."""
        if key != "category":
            raise NotImplementedError(f"WordPress has no live options for {key}")
        categories = self._wp(
            "GET",
            "/wp/v2/categories",
            access_token,
            params={"per_page": 100, "hide_empty": "false", "_fields": "id,name"},
        )
        return [{"id": str(item["id"]), "name": item["name"]} for item in categories]

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    def publish_post(self, access_token: str, content: PublishContent) -> PublishResult:
        """Create a post with POST /wp/v2/posts, uploading the first image as its featured image."""
        title = self._require_title(content)
        extra = content.extra
        error = self.validate_post_settings(extra)
        if error:
            raise PublishError(error, platform=self.platform_name, retryable=False)

        post: dict = {
            "title": title,
            "content": markdown_to_html(self._body(content)),
            "status": extra.get("status") or "publish",
        }
        if str(extra.get("category") or "").isdigit():
            post["categories"] = [int(extra["category"])]
        tag_ids = self._tag_ids(access_token, self._tags(content))
        if tag_ids:
            post["tags"] = tag_ids
        if content.media_files:
            post["featured_media"] = self._upload_image(access_token, content.media_files[0])

        data = self._wp("POST", "/wp/v2/posts", access_token, json=post)
        if not data.get("id"):
            raise PublishError(
                "WordPress accepted the request but returned no post id",
                platform=self.platform_name,
                raw_response=data,
            )
        return PublishResult(platform_post_id=str(data["id"]), url=data.get("link"), extra=data)

    def _upload_image(self, access_token: str, path: str) -> int:
        """Upload ``path`` to the media library and return its id."""
        filename = os.path.basename(path)
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        with open(path, "rb") as handle:
            data = self._wp(
                "POST",
                "/wp/v2/media",
                access_token,
                headers={"Content-Disposition": f'attachment; filename="{filename}"', "Content-Type": mime},
                data=handle,
                # A media-library item is not a post; a lost response only costs a duplicate upload.
                ambiguous_on_failure=False,
            )
        if not data.get("id"):
            raise PublishError(
                "WordPress accepted the image but returned no media id",
                platform=self.platform_name,
                raw_response=data,
            )
        return int(data["id"])

    def _tag_ids(self, access_token: str, names: list[str]) -> list[int]:
        """The ids of the site's tags named ``names``, creating the missing ones.

        A user who may not create tags still publishes: the tags the site doesn't have are dropped.
        """
        ids: list[int] = []
        for name in names:
            try:
                found = self._wp(
                    "GET", "/wp/v2/tags", access_token, params={"search": name, "per_page": 100, "_fields": "id,name"}
                )
                match = next((t for t in found if t["name"].lower() == name.lower()), None)
                if match is None:
                    match = self._wp(
                        "POST", "/wp/v2/tags", access_token, json={"name": name}, ambiguous_on_failure=False
                    )
                ids.append(int(match["id"]))
            except (APIError, PublishError, KeyError, ValueError):
                logger.warning("Could not resolve WordPress tag %r", name)
        return ids
