"""Threads API provider."""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from urllib.parse import urlencode

from .base import SocialProvider
from .exceptions import OAuthError, PublishError
from .types import (
    AccountProfile,
    AuthType,
    CommentResult,
    MediaRules,
    MediaType,
    OAuthTokens,
    PeakWindow,
    PlugAction,
    PlugCapability,
    PlugTarget,
    PlugTrigger,
    PostMetrics,
    PostType,
    PublishContent,
    PublishResult,
    RateLimitConfig,
    ThreadCapability,
    ThreadMode,
)

logger = logging.getLogger(__name__)

AUTH_URL = "https://www.threads.com/oauth/authorize"
TOKEN_URL = "https://graph.threads.net/oauth/access_token"
API_BASE = "https://graph.threads.net/v1.0"

# Threads ingests media (esp. video) asynchronously: the container must reach
# status FINISHED before it can be published, otherwise threads_publish fails
# with "The requested resource does not exist" (code 24 / media not found).
CONTAINER_POLL_INTERVAL = 3  # seconds
CONTAINER_POLL_TIMEOUT = 120  # seconds of video processing

# Meta issues long-lived Threads tokens with a 60-day TTL. Used as the fallback
# when a token response omits ``expires_in`` — see ``_tokens_from``.
LONG_LIVED_TOKEN_TTL = 60 * 24 * 60 * 60


# What Threads accepts: images up to 8 MB, videos up to 5 minutes and 1 GB,
# and carousels of 2 to 20 items (checked against the page below, 2026-10).
# https://developers.facebook.com/docs/threads/overview#media-specifications
_THREADS_DOCS = "https://developers.facebook.com/docs/threads/overview#media-specifications"
THREADS_MEDIA_RULES = {
    PostType.IMAGE: MediaRules(source=_THREADS_DOCS, max_image_bytes=8 * 1024 * 1024),
    PostType.VIDEO: MediaRules(source=_THREADS_DOCS, max_duration=5 * 60, max_video_bytes=1024 * 1024 * 1024),
    PostType.CAROUSEL: MediaRules(
        source=_THREADS_DOCS,
        max_items=20,
        max_image_bytes=8 * 1024 * 1024,
        max_duration=5 * 60,
        max_video_bytes=1024 * 1024 * 1024,
    ),
}


class ThreadsProvider(SocialProvider):
    """Threads API provider using OAuth 2.0."""

    profile_url_template = "https://www.threads.net/@{handle}"

    media_rules = THREADS_MEDIA_RULES
    # Each part is a reply to the one before it (``reply_to_id``).
    thread = ThreadCapability(mode=ThreadMode.REPLY_CHAIN, max_parts=25, media_per_part=True)

    # Source: https://buffer.com/resources/best-time-to-post-social-media/ (published 2026-03-25; checked 2026-10-24):
    # weekdays 7am-12pm, Wed best, then Thu and Tue; Sprout has no Threads data. Hours are the audience's local time; industry averages, not your audience.
    best_time_source = "Buffer study, Mar 2026"
    best_time_windows = (
        PeakWindow(days=(1, 2, 3), hours=(8, 9, 10)),
        PeakWindow(days=(0, 4), hours=(9, 10, 11)),
    )

    # A reply is a text container with reply_to_id, published like any post; insights report views, likes and
    # reposts. POST /{threads-id}/repost boosts a post for the connected account (threads_content_publish).
    # Reposting one's own post is not offered: the API page does not say it is allowed, and the app does not.
    # https://developers.facebook.com/docs/threads/posts/reposts
    plugs = PlugCapability(
        triggers=frozenset({PlugTrigger.LIKES, PlugTrigger.REPOSTS, PlugTrigger.VIEWS}),
        own_actions=frozenset({PlugAction.REPLY}),
        cross_actions=frozenset({PlugAction.REPOST}),
        message_max_length=500,
        source="https://developers.facebook.com/docs/threads/posts/reposts",
        # Meta's inauthentic-behaviour policy targets networks of accounts boosting each other, so a repost by another
        # account is only ever made once a person approves it (every cross-account action is held).
        policy_source="https://transparency.meta.com/policies/community-standards/inauthentic-behavior/",
    )

    writing_style = "Casual and conversational, like talking to followers. At most one hashtag; emojis welcome."

    # Publishes from hosted URLs only — the platform fetches the media
    # itself, so ``PublishContent.media_files`` is never read and the engine
    # can skip downloading the asset to local disk entirely.
    needs_local_media = False

    def __init__(self, credentials: dict | None = None):
        creds = dict(credentials or {})
        # Normalize: accept app_id/app_secret as aliases for client_id/client_secret
        if "app_id" in creds and "client_id" not in creds:
            creds["client_id"] = creds.pop("app_id")
        if "app_secret" in creds and "client_secret" not in creds:
            creds["client_secret"] = creds.pop("app_secret")
        super().__init__(creds)

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    @property
    def platform_name(self) -> str:
        return "Threads"

    @property
    def auth_type(self) -> AuthType:
        return AuthType.OAUTH2

    @property
    def max_caption_length(self) -> int:
        return 500

    @property
    def supported_post_types(self) -> list[PostType]:
        return [PostType.TEXT, PostType.IMAGE, PostType.VIDEO, PostType.CAROUSEL]

    @property
    def supported_media_types(self) -> list[MediaType]:
        return [MediaType.JPEG, MediaType.PNG, MediaType.MP4, MediaType.MOV]

    @property
    def required_scopes(self) -> list[str]:
        return [
            "threads_basic",
            "threads_content_publish",
            "threads_manage_insights",
            "threads_manage_replies",
        ]

    @property
    def rate_limits(self) -> RateLimitConfig:
        return RateLimitConfig(
            requests_per_hour=200,
            requests_per_day=5000,
            publish_per_day=250,
        )

    # ------------------------------------------------------------------
    # OAuth
    # ------------------------------------------------------------------

    def get_auth_url(self, redirect_uri: str, state: str, code_verifier: str | None = None) -> str:
        params = {
            "client_id": self.credentials["client_id"],
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": ",".join(self.required_scopes),
            "response_type": "code",
        }
        return f"{AUTH_URL}?{urlencode(params)}"

    def exchange_code(self, code: str, redirect_uri: str, code_verifier: str | None = None) -> OAuthTokens:
        resp = self._request(
            "POST",
            TOKEN_URL,
            data={
                "client_id": self.credentials["client_id"],
                "client_secret": self.credentials["client_secret"],
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            },
        )
        body = resp.json()
        short_lived_token = body.get("access_token")
        if not short_lived_token:
            raise OAuthError(
                f"Threads token exchange failed: {body}",
                platform=self.platform_name,
                raw_response=body,
            )

        # Exchange short-lived token for long-lived token
        long_lived = self._exchange_for_long_lived_token(short_lived_token)
        return long_lived

    def _exchange_for_long_lived_token(self, short_lived_token: str) -> OAuthTokens:
        """Exchange a short-lived token for a long-lived one."""
        resp = self._request(
            "GET",
            f"{API_BASE}/access_token",
            params={
                "grant_type": "th_exchange_token",
                "client_secret": self.credentials["client_secret"],
                "access_token": short_lived_token,
            },
        )
        body = resp.json()
        if "access_token" not in body:
            raise OAuthError(
                f"Threads long-lived token exchange failed: {body}",
                platform=self.platform_name,
                raw_response=body,
            )
        return self._tokens_from(body)

    @staticmethod
    def _tokens_from(body: dict) -> OAuthTokens:
        """Build OAuthTokens from a long-lived-token response.

        Threads issues no separate refresh token: the long-lived access token is
        replayed as its own refresh credential (see ``refresh_token``). Returning
        it in ``refresh_token`` is what lets the refresh machinery — which skips
        accounts whose ``oauth_refresh_token`` is empty — ever run, and carries
        the rotated token forward so an account keeps refreshing rather than
        replaying the stale original.

        ``expires_in`` falls back to the documented 60-day TTL: consumers only
        advance ``token_expires_at`` when it is truthy, so a response without it
        would leave a stale past expiry in place and re-trigger a refresh on
        every health check.
        """
        return OAuthTokens(
            access_token=body["access_token"],
            refresh_token=body["access_token"],
            expires_in=body.get("expires_in") or LONG_LIVED_TOKEN_TTL,
            token_type=body.get("token_type", "Bearer"),
            raw_response=body,
        )

    def refresh_token(self, refresh_token: str) -> OAuthTokens:
        """Refresh a long-lived Threads token.

        Note: Threads uses the access token itself for refresh (no separate
        refresh_token). The ``refresh_token`` argument here is the current
        long-lived access token.
        """
        resp = self._request(
            "GET",
            f"{API_BASE}/refresh_access_token",
            params={
                "grant_type": "th_refresh_token",
                "access_token": refresh_token,
            },
        )
        body = resp.json()
        if "access_token" not in body:
            raise OAuthError(
                f"Threads token refresh failed: {body}",
                platform=self.platform_name,
                raw_response=body,
            )
        return self._tokens_from(body)

    # ------------------------------------------------------------------
    # Profile
    # ------------------------------------------------------------------

    def get_profile(self, access_token: str) -> AccountProfile:
        resp = self._request(
            "GET",
            f"{API_BASE}/me",
            access_token=access_token,
            params={
                "fields": "id,username,name,threads_profile_picture_url,threads_biography",
            },
        )
        body = resp.json()
        return AccountProfile(
            platform_id=body.get("id", ""),
            name=body.get("name", ""),
            handle=body.get("username"),
            avatar_url=body.get("threads_profile_picture_url"),
            extra={"biography": body.get("threads_biography")},
        )

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    def publish_post(self, access_token: str, content: PublishContent) -> PublishResult:
        user_id = content.extra.get("user_id")
        if not user_id:
            # Fetch user_id from profile if not provided
            profile = self.get_profile(access_token)
            user_id = profile.platform_id

        if not user_id:
            raise PublishError(
                "Could not determine Threads user_id",
                platform=self.platform_name,
            )

        if content.post_type == PostType.CAROUSEL:
            return self._publish_carousel(access_token, user_id, content)

        return self._publish_single(access_token, user_id, content)

    def _publish_single(
        self,
        access_token: str,
        user_id: str,
        content: PublishContent,
    ) -> PublishResult:
        """Create and publish a single-item thread (TEXT, IMAGE, or VIDEO)."""
        # Step 1: Create container
        container_payload: dict = {
            "text": (content.text or "")[: self.max_caption_length],
        }

        if content.post_type == PostType.IMAGE and content.media_urls:
            container_payload["media_type"] = "IMAGE"
            container_payload["image_url"] = content.media_urls[0]
        elif content.post_type == PostType.VIDEO and content.media_urls:
            container_payload["media_type"] = "VIDEO"
            container_payload["video_url"] = content.media_urls[0]
        else:
            container_payload["media_type"] = "TEXT"

        # Add reply_to_id if this is a reply
        reply_to = content.extra.get("reply_to_id")
        if reply_to:
            container_payload["reply_to_id"] = reply_to

        create_resp = self._request(
            "POST",
            f"{API_BASE}/{user_id}/threads",
            access_token=access_token,
            data=container_payload,
        )
        create_body = create_resp.json()
        creation_id = create_body.get("id")

        if not creation_id:
            raise PublishError(
                # The body goes in raw_response, not the message: publish_error
                # is rendered to users, and a ProviderError message is the one
                # thing the sanitizer passes through verbatim.
                "Threads container creation failed",
                platform=self.platform_name,
                raw_response=create_body,
            )

        # Step 1b: wait for media containers to finish processing before
        # publishing. Text-only threads publish instantly, but image/video
        # containers are ingested asynchronously; publishing too early fails
        # with "media not found".
        if container_payload["media_type"] != "TEXT":
            self._wait_for_container(access_token, creation_id)

        # Step 2: Publish the container
        publish_resp = self._request(
            "POST",
            f"{API_BASE}/{user_id}/threads_publish",
            access_token=access_token,
            data={"creation_id": creation_id},
        )
        publish_body = publish_resp.json()
        thread_id = publish_body.get("id", "")
        return PublishResult(
            platform_post_id=thread_id,
            extra=publish_body,
        )

    def _wait_for_container(self, access_token: str, creation_id: str) -> None:
        """Poll a Threads media container until it is FINISHED.

        Threads exposes the ingest state via the container's ``status`` field
        (values: IN_PROGRESS, FINISHED, ERROR, EXPIRED, PUBLISHED). Video
        containers stay IN_PROGRESS while Meta transcodes the upload; calling
        threads_publish before FINISHED returns "media not found".
        """

        def finished() -> bool:
            resp = self._request(
                "GET",
                f"{API_BASE}/{creation_id}",
                access_token=access_token,
                params={"fields": "status,error_message"},
            )
            data = resp.json()
            status = data.get("status", "")
            if status in ("ERROR", "EXPIRED"):
                raise PublishError(
                    f"Threads container failed: {data.get('error_message', status)}",
                    platform=self.platform_name,
                    raw_response=data,
                )
            return status in ("FINISHED", "PUBLISHED")

        self._poll_until(
            finished,
            deadline=time.monotonic() + CONTAINER_POLL_TIMEOUT,
            interval=CONTAINER_POLL_INTERVAL,
            on_timeout=lambda: PublishError("Threads container processing timed out", platform=self.platform_name),
        )

    def _publish_carousel(
        self,
        access_token: str,
        user_id: str,
        content: PublishContent,
    ) -> PublishResult:
        """Create and publish a carousel thread."""
        # Step 1: Create individual item containers
        children_ids: list[str] = []

        for index, url in enumerate(content.media_urls):
            if content.is_video(index):
                media_type = "VIDEO"
                key = "video_url"
            else:
                media_type = "IMAGE"
                key = "image_url"

            item_resp = self._request(
                "POST",
                f"{API_BASE}/{user_id}/threads",
                access_token=access_token,
                data={
                    "media_type": media_type,
                    key: url,
                    "is_carousel_item": "true",
                },
            )
            item_body = item_resp.json()
            item_id = item_body.get("id")
            if not item_id:
                raise PublishError(
                    "Threads carousel item creation failed",
                    platform=self.platform_name,
                    raw_response=item_body,
                )
            # Video children ingest asynchronously; wait before they are
            # referenced by the carousel container.
            if media_type == "VIDEO":
                self._wait_for_container(access_token, item_id)
            children_ids.append(item_id)

        # Step 2: Create carousel container
        carousel_resp = self._request(
            "POST",
            f"{API_BASE}/{user_id}/threads",
            access_token=access_token,
            data={
                "media_type": "CAROUSEL",
                "children": ",".join(children_ids),
                "text": (content.text or "")[: self.max_caption_length],
                # A carousel can be a reply too, which is how a thread part
                # carrying several images joins its thread.
                **({"reply_to_id": content.extra["reply_to_id"]} if content.extra.get("reply_to_id") else {}),
            },
        )
        carousel_body = carousel_resp.json()
        creation_id = carousel_body.get("id")
        if not creation_id:
            raise PublishError(
                "Threads carousel container creation failed",
                platform=self.platform_name,
                raw_response=carousel_body,
            )

        self._wait_for_container(access_token, creation_id)

        # Step 3: Publish
        publish_resp = self._request(
            "POST",
            f"{API_BASE}/{user_id}/threads_publish",
            access_token=access_token,
            data={"creation_id": creation_id},
        )
        publish_body = publish_resp.json()
        thread_id = publish_body.get("id", "")
        return PublishResult(
            platform_post_id=thread_id,
            extra=publish_body,
        )

    def publish_thread_part(
        self,
        access_token: str,
        content: PublishContent,
        *,
        root: PublishResult,
        parent: PublishResult,
    ) -> PublishResult:
        """Publish a thread part as a reply to the part before it.

        Threads takes ``reply_to_id`` on any container, media or not, so a
        part goes through ``publish_post`` and keeps its own media.
        """
        return self.publish_post(
            access_token,
            replace(content, extra={**content.extra, "reply_to_id": parent.platform_post_id}),
        )

    # ------------------------------------------------------------------
    # Comments (replies)
    # ------------------------------------------------------------------

    def publish_comment(self, access_token: str, post_id: str, text: str) -> CommentResult:
        """Reply to a thread (uses the container flow with reply_to_id)."""
        user_id_resp = self._request(
            "GET",
            f"{API_BASE}/me",
            access_token=access_token,
            params={"fields": "id"},
        )
        user_id = user_id_resp.json().get("id", "")

        # Create reply container
        create_resp = self._request(
            "POST",
            f"{API_BASE}/{user_id}/threads",
            access_token=access_token,
            data={
                "media_type": "TEXT",
                "text": text[: self.max_caption_length],
                "reply_to_id": post_id,
            },
        )
        creation_id = create_resp.json().get("id")
        if not creation_id:
            raise PublishError(
                "Threads reply container creation failed",
                platform=self.platform_name,
                raw_response=create_resp.json(),
            )

        # Publish
        publish_resp = self._request(
            "POST",
            f"{API_BASE}/{user_id}/threads_publish",
            access_token=access_token,
            data={"creation_id": creation_id},
        )
        reply_id = publish_resp.json().get("id", "")
        return CommentResult(
            platform_comment_id=reply_id,
            extra=publish_resp.json(),
        )

    def repost_post(self, access_token: str, target: PlugTarget) -> PublishResult:
        """Repost a Threads post for the connected account (``POST /{threads-id}/repost``)."""
        data = self._request("POST", f"{API_BASE}/{target.platform_post_id}/repost", access_token=access_token).json()
        return PublishResult(platform_post_id=str(data.get("id", "")), extra=data)

    # ------------------------------------------------------------------
    # Analytics
    # ------------------------------------------------------------------

    def get_post_metrics(self, access_token: str, post_id: str) -> PostMetrics:
        resp = self._request(
            "GET",
            f"{API_BASE}/{post_id}/insights",
            access_token=access_token,
            params={
                "metric": "views,likes,replies,reposts,quotes",
            },
        )
        body = resp.json()
        data = body.get("data", [])

        metrics: dict[str, int] = {}
        for item in data:
            name = item.get("name", "")
            values = item.get("values", [])
            if values:
                metrics[name] = values[0].get("value", 0)

        return PostMetrics(
            impressions=metrics.get("views", 0),
            likes=metrics.get("likes", 0),
            comments=metrics.get("replies", 0),
            shares=metrics.get("reposts", 0),
            engagements=(
                metrics.get("likes", 0)
                + metrics.get("replies", 0)
                + metrics.get("reposts", 0)
                + metrics.get("quotes", 0)
            ),
            extra={
                "quotes": metrics.get("quotes", 0),
                "raw": data,
            },
        )
