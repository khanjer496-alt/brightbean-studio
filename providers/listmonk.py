"""Listmonk provider implementation.

Listmonk is a self-hosted newsletter manager. A post becomes an email campaign sent to one of its lists, through
the instance's REST API (https://listmonk.app/docs/apis/campaigns/). Authentication is an API user's token sent as
``Authorization: token username:token``; there is no OAuth flow, so this provider uses ``AuthType.SESSION`` and
stores the token as the account's access token. The instance address is the account's ``instance_url`` and the API
username lives in ``platform_data``.
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

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

MAX_SUBJECT_LENGTH = 250

_IMAGE_RULES = MediaRules(
    source="https://listmonk.app/docs/apis/campaigns/",
    media_types=frozenset({"image"}),
)


class ListmonkProvider(BlogProvider):
    """Self-hosted Listmonk provider using an API user token (no OAuth)."""

    max_title_length = MAX_SUBJECT_LENGTH

    media_rules = {PostType.IMAGE: _IMAGE_RULES}

    writing_style = "A newsletter reader: a subject line that says what is inside, a warm direct opening and short scannable sections. No hashtags."

    connect_url_name = "connect_listmonk"
    supports_connection_link = False
    connect_intro = "Connect a self-hosted Listmonk instance (served over HTTPS) with an API user."
    connect_notice = (
        "Each post becomes a campaign sent to the list you pick in the composer, with the title as the subject. "
        "Create the API user under Admin → Users; it needs permission to read lists and manage campaigns. "
        "The body is a Listmonk template, so {{ .Subscriber.FirstName }} personalizes it."
    )
    connect_url_field = "site_url"
    account_url_credential = "site_url"
    connect_fields = (
        ConnectField(
            key="site_url",
            label="Listmonk address",
            kind="url",
            placeholder="https://listmonk.example.com",
            help="The address of your Listmonk instance (without /admin).",
        ),
        ConnectField(key="username", label="API username", placeholder="the API user's name"),
        ConnectField(
            key="token",
            label="API token",
            kind="password",
            placeholder="your API token",
            help="Shown once when you create the API user.",
        ),
    )

    post_settings = (
        PostSetting(key="list", label="Send to list", kind="select", required=True, options_from_provider=True),
        PostSetting(
            key="delivery",
            label="Delivery",
            kind="select",
            choices=(("now", "Send when the post publishes"), ("draft", "Save as a draft campaign")),
            help="A draft waits in Listmonk for you to review and start. Schedule the post to choose when it sends.",
        ),
        PostSetting(
            key="template",
            label="Template",
            kind="select",
            options_from_provider=True,
            help="Listmonk's default campaign template when empty.",
        ),
    )

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    @property
    def platform_name(self) -> str:
        return "Listmonk"

    @property
    def auth_type(self) -> AuthType:
        return AuthType.SESSION

    @property
    def max_caption_length(self) -> int:
        return 100000

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
        return RateLimitConfig(requests_per_hour=300, requests_per_day=3000, publish_per_day=20)

    @classmethod
    def credentials_for_account(cls, account) -> dict:
        return {"username": (account.platform_data or {}).get("username", "")}

    @classmethod
    def profile_url(cls, account) -> str:
        return ""  # an instance's admin address is private, and it has no public profile

    # ------------------------------------------------------------------
    # Requests
    # ------------------------------------------------------------------

    @property
    def site_url(self) -> str:
        url = self.credentials.get("site_url", "")
        if not url:
            raise PublishError(
                "This Listmonk account has no usable address. Reconnect it.",
                platform=self.platform_name,
                retryable=False,
            )
        return url

    def _api(self, method: str, path: str, access_token: str, **kwargs) -> dict:
        """One call to the instance's ``/api{path}``; the payload under ``data`` is returned."""
        username = self.credentials.get("username", "")
        resp = self._request(
            method,
            f"{self.site_url}/api{path}",
            headers={"Authorization": f"token {username}:{access_token}", "Accept": "application/json"},
            **kwargs,
        )
        try:
            return resp.json().get("data")
        except (ValueError, AttributeError) as exc:
            raise PublishError(
                "The address didn't answer like a Listmonk instance. Check it, and that it isn't redirected.",
                platform=self.platform_name,
                retryable=False,
            ) from exc

    def _lists(self, access_token: str) -> list[dict]:
        data = self._api("GET", "/lists", access_token, params={"minimal": "true", "per_page": "all"})
        return (data or {}).get("results") or []

    # ------------------------------------------------------------------
    # Profile
    # ------------------------------------------------------------------

    def get_profile(self, access_token: str) -> AccountProfile:
        """Validate the API user by reading the lists, and name the account after the instance."""
        self._lists(access_token)
        parts = urlsplit(self.site_url)
        username = self.credentials.get("username", "")
        return AccountProfile(
            platform_id=f"{parts.netloc}{parts.path}:{username}",
            name=parts.netloc,
            handle=username,
            follower_count=0,
            extra={"account_data": {"username": username}},
        )

    def connect_with_form(self, values: dict) -> tuple[AccountProfile, str]:
        self.credentials = {**self.credentials, "site_url": values["site_url"], "username": values["username"]}
        token = values["token"]
        try:
            return self.get_profile(token), token
        except APIError as exc:
            if exc.status_code in (401, 403):
                raise PublishError(
                    "Listmonk rejected the login. Check the API username and token, and that the user may read lists.",
                    platform=self.platform_name,
                    retryable=False,
                ) from exc
            raise

    # ------------------------------------------------------------------
    # Post settings
    # ------------------------------------------------------------------

    @classmethod
    def validate_post_settings(cls, settings: dict) -> str:
        error = super().validate_post_settings(settings)
        if error:
            return error
        for key, label in (("list", "List"), ("template", "Template")):
            value = str((settings or {}).get(key) or "")
            if value and not value.isdigit():
                return f"{label} has an unsupported value."
        return ""

    def get_setting_options(self, access_token: str, key: str, depends_value: str = "") -> list[dict]:
        """The instance's lists and campaign templates."""
        if key == "list":
            return [{"id": str(item["id"]), "name": item["name"]} for item in self._lists(access_token)]
        if key == "template":
            templates = self._api("GET", "/templates", access_token) or []
            return [
                {"id": str(item["id"]), "name": item["name"]}
                for item in templates
                if item.get("type", "campaign") == "campaign"
            ]
        raise NotImplementedError(f"Listmonk has no live options for {key}")

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    def publish_post(self, access_token: str, content: PublishContent) -> PublishResult:
        """Create a campaign for the chosen list and start it, or leave it as a draft."""
        subject = self._require_title(content)
        extra = content.extra
        error = self.validate_post_settings(extra)
        if error:
            raise PublishError(error, platform=self.platform_name, retryable=False)

        images = "".join(f"![]({url})\n\n" for url in self._image_urls(content))
        campaign: dict = {
            "name": subject,
            "subject": subject,
            "lists": [int(extra["list"])],
            "type": "regular",
            "content_type": "markdown",
            "body": f"{images}{self._body(content)}",
        }
        if extra.get("template"):
            campaign["template_id"] = int(extra["template"])

        created = self._api("POST", "/campaigns", access_token, json=campaign) or {}
        campaign_id = created.get("id")
        if not campaign_id:
            raise PublishError(
                "Listmonk accepted the request but returned no campaign id",
                platform=self.platform_name,
                raw_response=created,
            )
        if extra.get("delivery") != "draft":
            try:
                self._api("PUT", f"/campaigns/{campaign_id}/status", access_token, json={"status": "running"})
            except Exception as exc:
                # The campaign exists: a retry would create a second one, so this is final.
                raise PublishError(
                    "Listmonk created the campaign but couldn't start it. Open it in Listmonk to review and send it.",
                    platform=self.platform_name,
                    retryable=False,
                ) from exc
        return PublishResult(
            platform_post_id=str(campaign_id),
            url=f"{self.site_url}/admin/campaigns/{campaign_id}",
            extra=created,
        )
