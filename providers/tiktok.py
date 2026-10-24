"""TikTok Content Posting API provider."""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
from datetime import datetime
from typing import NoReturn
from urllib.parse import urlencode

from .base import SocialProvider
from .exceptions import APIError, OAuthError, PublishError
from .types import (
    AccountMetrics,
    AccountProfile,
    AuthType,
    MediaRules,
    MediaType,
    OAuthTokens,
    PeakWindow,
    PostMetrics,
    PostSetting,
    PostType,
    PublishContent,
    PublishResult,
    PublishState,
    PublishStatus,
    RateLimitConfig,
)

logger = logging.getLogger(__name__)

AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
API_BASE = "https://open.tiktokapis.com/v2"

DEFAULT_PRIVACY_LEVEL = "PUBLIC_TO_EVERYONE"
VALID_PRIVACY_LEVELS = frozenset(
    {
        "PUBLIC_TO_EVERYONE",
        "MUTUAL_FOLLOW_FRIENDS",
        "FOLLOWER_OF_CREATOR",
        "SELF_ONLY",
    }
)

# Error codes from /v2/post/publish/video/init/ that no amount of retrying
# can fix (app audit status, bad params, missing scopes). The engine fails
# these immediately instead of burning the backoff schedule.
PERMANENT_PUBLISH_ERROR_CODES = frozenset(
    {
        "unaudited_client_can_only_post_to_private_accounts",
        "privacy_level_option_mismatch",
        "invalid_param",
        "scope_not_authorized",
        "scope_permission_missed",
        "url_ownership_unverified",
        "app_version_check_failed",
        "spam_risk_too_many_pending_share",
    }
)

UNAUDITED_CLIENT_HINT = (
    "The TikTok app has not passed TikTok's content-posting audit yet. "
    "Until TikTok approves the audit (developer portal → Content Posting API "
    "→ apply for an audit), videos can only be published as private "
    "(SELF_ONLY). Set this post's TikTok privacy to 'Only you' to publish now."
)

# Optional post_info fields the composer may set via platform_extra.
# https://developers.tiktok.com/doc/content-posting-api-reference-direct-post
OPTIONAL_POST_INFO_FIELDS = (
    "disable_comment",
    "disable_duet",
    "disable_stitch",
    "brand_content_toggle",
    "brand_organic_toggle",
    "is_aigc",
)

# TikTok FILE_UPLOAD's per-chunk limit is 64 MB decimal (not 64 MiB).
# Anything larger requires multi-chunk upload, which we don't implement yet.
MAX_SINGLE_CHUNK_SIZE = 64_000_000

CONTENT_TYPE_BY_EXT = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
}

# /v2/post/publish/status/fetch/ "status" values, mapped to what they mean for
# us. Anything absent from this map is treated as still-in-flight, so a status
# TikTok adds later makes the engine wait rather than declare a false outcome.
PUBLISH_STATE_BY_TIKTOK_STATUS = {
    "PUBLISH_COMPLETE": PublishState.COMPLETE,
    "FAILED": PublishState.FAILED,
    # The direct-post request was downgraded to an upload: the video is sitting
    # in the creator's TikTok drafts and only they can publish it. Terminal for
    # us, and the one outcome where "failed" needs a very specific sentence.
    "SEND_TO_USER_INBOX": PublishState.INBOX,
}

INBOX_HINT = (
    "TikTok put this post in your TikTok inbox as a draft instead of posting it. "
    "Open the TikTok app to finish posting it."
)

# Photo posts: POST /v2/post/publish/content/init/ (media_type PHOTO). Title up to 90 and description up to 4,000
# UTF-16 runes, up to 35 images, PULL_FROM_URL only (the URL prefix must be verified in the TikTok developer portal),
# and post_mode DIRECT_POST (scope video.publish) or MEDIA_UPLOAD (scope video.upload, lands in the user's inbox;
# needs TikTok app 31.8+). auto_add_music, disable_comment and the brand toggles apply to DIRECT_POST only.
# https://developers.tiktok.com/doc/content-posting-api-reference-photo-post
PHOTO_TITLE_MAX = 90
PHOTO_DESCRIPTION_MAX = 4000
PHOTO_MAX_IMAGES = 35
PHOTO_POST_INFO_FIELDS = ("disable_comment", "auto_add_music", "brand_content_toggle", "brand_organic_toggle")
POST_MODES = ("DIRECT_POST", "MEDIA_UPLOAD")


def _pkce_code_challenge(code_verifier: str) -> str:
    """Derive TikTok's PKCE ``code_challenge`` from a ``code_verifier``.

    TikTok deviates from RFC 7636: the challenge is the HEX-encoded SHA256 of
    the verifier (a 64-char hex string), NOT the base64url encoding that
    standard PKCE clients use. TikTok only supports the ``S256`` method.
    See https://developers.tiktok.com/doc/login-kit-desktop/
    """
    return hashlib.sha256(code_verifier.encode("ascii")).hexdigest()


# What TikTok's Content Posting API accepts. Creators can be held to a shorter
# maximum than 10 minutes; ``max_video_post_duration_sec`` from creator info is
# checked again at publish time.
# https://developers.tiktok.com/doc/content-posting-api-media-transfer-guide
TIKTOK_MEDIA_RULES = {
    # Photo mode takes up to 35 images pulled from a URL prefix verified with TikTok.
    PostType.IMAGE: MediaRules(
        source="https://developers.tiktok.com/doc/content-posting-api-reference-photo-post",
        max_items=PHOTO_MAX_IMAGES,
        media_types=frozenset({"image"}),
    ),
    PostType.VIDEO: MediaRules(
        source="https://developers.tiktok.com/doc/content-posting-api-media-transfer-guide",
        published_items=1,
        media_types=frozenset({"video"}),
        min_width=360,
        min_height=360,
        max_width=4096,
        max_height=4096,
        min_duration=3,
        max_duration=10 * 60,
        # TikTok takes up to 4 GB, but only as 5-64 MB chunks; we send the file as one chunk, so 64 MB is the most
        # we can publish. Checked here so it is caught when the post is scheduled, not when it fails.
        max_video_bytes=MAX_SINGLE_CHUNK_SIZE,
        advisory=frozenset({"min_duration"}),
    ),
}


class TikTokProvider(SocialProvider):
    """TikTok Content Posting API provider using OAuth 2.0."""

    post_settings = (
        PostSetting(
            key="post_mode",
            label="Delivery",
            kind="select",
            placeholder="Publish now (direct post)",
            choices=(("MEDIA_UPLOAD", "Send to my TikTok inbox as a draft"),),
            help=(
                "A draft waits in the TikTok app for you to edit and post it: privacy, interaction and "
                "branded-content settings are chosen there. TikTok allows 5 pending drafts a day."
            ),
        ),
        PostSetting(
            key="auto_add_music",
            label="Add recommended music (photo posts)",
            kind="checkbox",
            help="Photo posts only: lets TikTok pick music for the slideshow. Needs direct posting.",
        ),
    )

    profile_url_template = "https://www.tiktok.com/@{handle}"

    media_rules = TIKTOK_MEDIA_RULES
    account_default_keys = ("privacy_level", "disable_comment", "disable_duet", "disable_stitch")
    account_default_choices = {
        "privacy_level": tuple(sorted(VALID_PRIVACY_LEVELS)),
        "disable_comment": (True, False),
        "disable_duet": (True, False),
        "disable_stitch": (True, False),
    }

    # Source: https://sproutsocial.com/insights/best-times-to-post-on-social-media/ (published 2026-03-31; checked 2026-10-24):
    # Mon 3-5pm; Tue 2-6pm; Wed 1-8pm; Thu 1-5pm; Fri 3-5pm. Hours are the audience's local time; industry averages, not your audience.
    best_time_source = "Sprout Social study, Mar 2026"
    best_time_windows = (
        PeakWindow(days=(1, 2, 3), hours=(14, 15, 16)),
        PeakWindow(days=(0, 4), hours=(15, 16)),
        PeakWindow(days=(2,), hours=(13, 17, 18, 19)),
    )

    writing_style = "Casual, energetic and trend-aware: a punchy hook and a few relevant hashtags. Emojis welcome."

    # TikTok requires PKCE on the authorization request (for desktop/native
    # apps and localhost redirect URIs); see ``_pkce_code_challenge``.
    uses_pkce = True

    # /post/publish/video/init/ only *accepts* the video; TikTok transcodes and
    # publishes it afterwards, and that step can still fail (format, duration,
    # spam risk) or divert the video to the creator's drafts. So a finished
    # upload is not a published post — the engine settles these from
    # :meth:`check_publish_status`. See ``PUBLISH_STATE_BY_TIKTOK_STATUS``.
    publish_is_async = True

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    # https://developers.tiktok.com/doc/content-posting-api-reference-direct-post: the title
    # is "2200 in UTF-16 runes".
    text_length_unit = "utf16"

    @property
    def platform_name(self) -> str:
        return "TikTok"

    @property
    def auth_type(self) -> AuthType:
        return AuthType.OAUTH2

    @property
    def max_caption_length(self) -> int:
        return 2200

    @property
    def supported_post_types(self) -> list[PostType]:
        return [PostType.VIDEO, PostType.IMAGE]

    @property
    def supported_media_types(self) -> list[MediaType]:
        return [MediaType.MP4, MediaType.MOV]

    # /v2/user/info/ returns lifetime cumulative counters with no date filter.
    # See ``get_account_metrics`` below — the sync layer uses this flag to
    # skip backfilling identical values into historical date rows.
    account_metrics_supports_date_range: bool = False

    @property
    def required_scopes(self) -> list[str]:
        scopes = [
            "user.info.basic",
            "video.publish",
            "video.upload",
        ]
        if self.include_analytics_scopes:
            scopes.extend(self.analytics_only_scopes)
        return scopes

    @property
    def analytics_only_scopes(self) -> list[str]:
        # ``video.list`` lets us read per-video stats. Keep TikTok analytics
        # video-only so sandbox apps don't need the profile/stat scopes.
        return ["video.list"]

    @property
    def rate_limits(self) -> RateLimitConfig:
        return RateLimitConfig(
            requests_per_hour=200,
            requests_per_day=5000,
            publish_per_day=5,
        )

    # ------------------------------------------------------------------
    # OAuth
    # ------------------------------------------------------------------

    def get_auth_url(self, redirect_uri: str, state: str, code_verifier: str | None = None) -> str:
        params = {
            "client_key": self.credentials["client_key"],
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": ",".join(self.required_scopes),
            "response_type": "code",
        }
        if code_verifier:
            params["code_challenge"] = _pkce_code_challenge(code_verifier)
            params["code_challenge_method"] = "S256"
        return f"{AUTH_URL}?{urlencode(params)}"

    def exchange_code(self, code: str, redirect_uri: str, code_verifier: str | None = None) -> OAuthTokens:
        data = {
            "client_key": self.credentials["client_key"],
            "client_secret": self.credentials["client_secret"],
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
        if code_verifier:
            data["code_verifier"] = code_verifier
        resp = self._request("POST", TOKEN_URL, data=data)
        body = resp.json()
        if "access_token" not in body:
            raise OAuthError(
                f"TikTok token exchange failed: {body}",
                platform=self.platform_name,
                raw_response=body,
            )
        return OAuthTokens(
            access_token=body["access_token"],
            refresh_token=body.get("refresh_token"),
            expires_in=body.get("expires_in"),
            refresh_expires_in=body.get("refresh_expires_in"),
            scope=body.get("scope"),
            raw_response=body,
        )

    def refresh_token(self, refresh_token: str) -> OAuthTokens:
        resp = self._request(
            "POST",
            TOKEN_URL,
            data={
                "client_key": self.credentials["client_key"],
                "client_secret": self.credentials["client_secret"],
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            ambiguous_on_failure=False,
        )
        body = resp.json()
        if "access_token" not in body:
            raise OAuthError(
                f"TikTok token refresh failed: {body}",
                platform=self.platform_name,
                raw_response=body,
            )
        return OAuthTokens(
            access_token=body["access_token"],
            refresh_token=body.get("refresh_token"),
            expires_in=body.get("expires_in"),
            refresh_expires_in=body.get("refresh_expires_in"),
            scope=body.get("scope"),
            raw_response=body,
        )

    # ------------------------------------------------------------------
    # Profile
    # ------------------------------------------------------------------

    def get_profile(self, access_token: str) -> AccountProfile:
        resp = self._request(
            "GET",
            f"{API_BASE}/user/info/",
            access_token=access_token,
            params={
                "fields": "open_id,union_id,avatar_url,display_name",
            },
        )
        body = resp.json()
        user = body.get("data", {}).get("user", {})
        return AccountProfile(
            platform_id=user.get("open_id", ""),
            name=user.get("display_name", ""),
            avatar_url=user.get("avatar_url"),
            follower_count=0,
            extra={"union_id": user.get("union_id")},
        )

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    def query_creator_info(self, access_token: str) -> dict:
        """Query the creator's current posting capabilities.

        Returns the ``data`` block of ``/v2/post/publish/creator_info/query/``:
        ``creator_nickname``, ``privacy_level_options``, ``comment_disabled``,
        ``duet_disabled``, ``stitch_disabled``, ``max_video_post_duration_sec``.

        TikTok's integration guidelines require fetching this fresh at posting
        time (the allowed privacy levels depend on the app's audit status and
        the creator's account settings), so the result is never cached.
        """
        resp = self._request(
            "POST",
            f"{API_BASE}/post/publish/creator_info/query/",
            access_token=access_token,
            json={},
            ambiguous_on_failure=False,
        )
        return resp.json().get("data", {}) or {}

    @classmethod
    def validate_post_settings(cls, settings: dict) -> str:
        settings = settings or {}
        error = super().validate_post_settings(settings)
        if error:
            return error
        if settings.get("auto_add_music") and settings.get("post_mode") == "MEDIA_UPLOAD":
            return "TikTok adds music only to directly posted photos; choose to publish now, or clear the music option."
        return ""

    def publish_post(self, access_token: str, content: PublishContent) -> PublishResult:
        error = self.validate_post_settings(content.extra)
        if error:
            raise PublishError(error, platform=self.platform_name, retryable=False)
        if content.post_type == PostType.IMAGE:
            return self._publish_photos(access_token, content)
        if content.post_type != PostType.VIDEO:
            raise PublishError(
                "TikTok only supports video and photo posts",
                platform=self.platform_name,
                retryable=False,
            )
        if content.extra.get("auto_add_music"):
            raise PublishError(
                "Adding music applies to photo posts only; clear it for a video.",
                platform=self.platform_name,
                retryable=False,
            )
        inbox = content.extra.get("post_mode") == "MEDIA_UPLOAD"

        privacy_level_is_explicit = "privacy_level" in content.extra
        privacy_level = content.extra.get("privacy_level", DEFAULT_PRIVACY_LEVEL)
        if privacy_level not in VALID_PRIVACY_LEVELS:
            raise PublishError(
                f"Invalid privacy_level '{privacy_level}'. Must be one of {sorted(VALID_PRIVACY_LEVELS)}",
                platform=self.platform_name,
                retryable=False,
            )

        if not inbox:
            privacy_level = self._check_creator_constraints(
                access_token,
                privacy_level,
                content,
                privacy_level_is_explicit=privacy_level_is_explicit,
            )

        # Prefer FILE_UPLOAD: PULL_FROM_URL requires the source domain to be
        # verified with TikTok, which presigned S3/R2 URLs can't satisfy.
        if content.media_files:
            return self._publish_file_upload(access_token, content, privacy_level, inbox=inbox)
        if content.media_urls:
            return self._publish_pull_from_url(access_token, content, privacy_level, inbox=inbox)
        raise PublishError(
            "No video source provided (media_files or media_urls required)",
            platform=self.platform_name,
            retryable=False,
        )

    def _check_creator_constraints(
        self,
        access_token: str,
        privacy_level: str,
        content: PublishContent,
        *,
        privacy_level_is_explicit: bool,
    ) -> str:
        """Validate privacy level and video duration against creator_info.

        A creator_info failure is logged and ignored — a transient outage of
        that endpoint must not block publishing; the video init call surfaces
        any real error and :meth:`_raise_classified_publish_error` handles it.
        """
        try:
            info = self.query_creator_info(access_token)
        except Exception:
            logger.warning("TikTok creator_info query failed; proceeding with publish", exc_info=True)
            return privacy_level

        options = info.get("privacy_level_options") or []
        if options and privacy_level not in options:
            if not privacy_level_is_explicit and options == ["SELF_ONLY"]:
                privacy_level = "SELF_ONLY"
            else:
                message = (
                    f"TikTok does not allow privacy level '{privacy_level}' for this "
                    f"account (allowed: {', '.join(options)})."
                )
                if options == ["SELF_ONLY"]:
                    message = f"{message} {UNAUDITED_CLIENT_HINT}"
                raise PublishError(
                    message,
                    platform=self.platform_name,
                    raw_response=info,
                    retryable=False,
                )

        # TikTok's UX guidelines require checking the video against the
        # creator's max post duration before uploading. Skip silently when
        # either value is unknown (0/None) so we never block on missing data.
        max_duration = info.get("max_video_post_duration_sec")
        duration = content.video_duration_sec
        if max_duration and duration and duration > max_duration:
            raise PublishError(
                f"Video is {round(duration)}s long but TikTok allows at most "
                f"{int(max_duration)}s for this account. Trim the video and try again.",
                platform=self.platform_name,
                raw_response=info,
                retryable=False,
            )
        return privacy_level

    def _init_video_publish(
        self,
        access_token: str,
        payload: dict,
        *,
        path: str = "/post/publish/video/init/",
        ambiguous_on_failure: bool = True,
    ) -> dict:
        """POST to ``path`` (the direct-post video init unless told otherwise) and return the parsed body.

        ``/post/publish/inbox/video/init/`` is the MEDIA_UPLOAD variant: it takes ``source_info`` only.
        ``ambiguous_on_failure=False`` is for a FILE_UPLOAD init, which only opens an upload session.

        Permanent TikTok error codes are re-raised as non-retryable
        PublishErrors via :meth:`_raise_classified_publish_error`.
        """
        try:
            resp = self._request(
                "POST",
                f"{API_BASE}{path}",
                access_token=access_token,
                json=payload,
                ambiguous_on_failure=ambiguous_on_failure,
            )
        except APIError as exc:
            self._raise_classified_publish_error(exc)
        return resp.json()

    def _build_post_info(self, content: PublishContent, privacy_level: str) -> dict:
        post_info: dict[str, str | bool | int] = {
            "title": self._require_fit(
                "The caption", content.title or content.text, self.max_caption_length, caption=True
            ),
            "privacy_level": privacy_level,
        }
        for field in OPTIONAL_POST_INFO_FIELDS:
            if field in content.extra:
                post_info[field] = bool(content.extra[field])
        # Cover frame timestamp; TikTok defaults to the first frame when absent.
        cover_ms = content.extra.get("video_cover_timestamp_ms")
        if cover_ms is not None:
            with contextlib.suppress(TypeError, ValueError):
                cover_ms = int(cover_ms)
                if cover_ms >= 0:
                    post_info["video_cover_timestamp_ms"] = cover_ms
        return post_info

    @staticmethod
    def _video_init_path(inbox: bool) -> str:
        return "/post/publish/inbox/video/init/" if inbox else "/post/publish/video/init/"

    def _publish_photos(self, access_token: str, content: PublishContent) -> PublishResult:
        """Publish a photo post (``media_type`` PHOTO), directly or into the user's inbox as a draft."""
        urls = [url for index, url in enumerate(content.media_urls) if not content.is_video(index)]
        if not urls or len(urls) != len(content.media_urls):
            raise PublishError("A TikTok photo post needs images only.", platform=self.platform_name, retryable=False)
        if len(urls) > PHOTO_MAX_IMAGES:
            raise PublishError(
                f"TikTok photo posts take at most {PHOTO_MAX_IMAGES} images.",
                platform=self.platform_name,
                retryable=False,
            )
        extra = content.extra
        inbox = extra.get("post_mode") == "MEDIA_UPLOAD"
        post_info: dict[str, str | bool | int] = {}
        # The caption goes in the description; a title is sent only when one was written, so it isn't repeated.
        title = (content.title or "").strip()
        if title:
            post_info["title"] = self._require_fit("The title", title, PHOTO_TITLE_MAX)
        if content.text:
            post_info["description"] = self._require_fit(
                "The caption", content.text, PHOTO_DESCRIPTION_MAX, caption=True
            )
        if not inbox:
            privacy_explicit = "privacy_level" in extra
            privacy = extra.get("privacy_level", DEFAULT_PRIVACY_LEVEL)
            if privacy not in VALID_PRIVACY_LEVELS:
                raise PublishError(
                    f"Invalid privacy_level '{privacy}'. Must be one of {sorted(VALID_PRIVACY_LEVELS)}",
                    platform=self.platform_name,
                    retryable=False,
                )
            post_info["privacy_level"] = self._check_creator_constraints(
                access_token, privacy, content, privacy_level_is_explicit=privacy_explicit
            )
            for field in PHOTO_POST_INFO_FIELDS:
                if field in extra:
                    post_info[field] = bool(extra[field])
        payload: dict = {
            "media_type": "PHOTO",
            "post_mode": "MEDIA_UPLOAD" if inbox else "DIRECT_POST",
            "post_info": post_info,
            "source_info": {"source": "PULL_FROM_URL", "photo_cover_index": 0, "photo_images": urls},
        }
        if extra.get("is_aigc") and not inbox:
            payload["is_aigc"] = True
        try:
            resp = self._request(
                "POST", f"{API_BASE}/post/publish/content/init/", access_token=access_token, json=payload
            )
        except APIError as exc:
            self._raise_classified_publish_error(exc)
        body = resp.json()
        return PublishResult(platform_post_id=body.get("data", {}).get("publish_id", ""), extra=body.get("data", {}))

    def _raise_classified_publish_error(self, exc: APIError) -> NoReturn:
        """Re-raise an APIError from video init, marking permanent codes non-retryable."""
        code = ((exc.raw_response or {}).get("error") or {}).get("code", "")
        if code not in PERMANENT_PUBLISH_ERROR_CODES:
            raise exc
        message = f"TikTok rejected the post ({code}): {exc}"
        if code == "url_ownership_unverified":
            message = (
                "TikTok rejected the post: photo posts are pulled from a URL prefix that must be verified in "
                "the TikTok developer portal (URL properties), and this post's images are served from one that isn't."
            )
        if code == "unaudited_client_can_only_post_to_private_accounts":
            message = f"TikTok rejected the post: {UNAUDITED_CLIENT_HINT}"
        raise PublishError(
            message,
            platform=self.platform_name,
            raw_response=exc.raw_response,
            retryable=False,
        ) from exc

    def _publish_pull_from_url(
        self,
        access_token: str,
        content: PublishContent,
        privacy_level: str,
        *,
        inbox: bool = False,
    ) -> PublishResult:
        """Publish using PULL_FROM_URL source."""
        payload = {
            "source_info": {
                "source": "PULL_FROM_URL",
                "video_url": content.media_urls[0],
            },
        }
        if not inbox:
            payload["post_info"] = self._build_post_info(content, privacy_level)
        body = self._init_video_publish(access_token, payload, path=self._video_init_path(inbox))
        publish_id = body.get("data", {}).get("publish_id", "")
        return PublishResult(
            platform_post_id=publish_id,
            extra=body.get("data", {}),
        )

    def _publish_file_upload(
        self,
        access_token: str,
        content: PublishContent,
        privacy_level: str,
        *,
        inbox: bool = False,
    ) -> PublishResult:
        """Publish using FILE_UPLOAD source (two-step)."""
        video_path = content.media_files[0]
        video_size = os.path.getsize(video_path)
        if video_size > MAX_SINGLE_CHUNK_SIZE:
            # Not retryable: the file will be exactly this big on every attempt,
            # and letting it burn the retry budget replaces this sentence with
            # the generic "we stopped retrying" copy — hiding the one thing the
            # user can actually act on.
            raise PublishError(
                f"This video is {video_size / 1_000_000:.0f} MB. TikTok accepts up to "
                f"{MAX_SINGLE_CHUNK_SIZE // 1_000_000} MB per upload — compress it or "
                "trim it, then try again.",
                platform=self.platform_name,
                retryable=False,
            )

        ext = os.path.splitext(video_path)[1].lower()
        content_type = CONTENT_TYPE_BY_EXT.get(ext, "video/mp4")

        # Step 1: initialize upload with size metadata TikTok requires
        payload = {
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": video_size,
                "chunk_size": video_size,
                "total_chunk_count": 1,
            },
        }
        if not inbox:
            payload["post_info"] = self._build_post_info(content, privacy_level)
        # Opening the session posts nothing; the PUT below is what hands TikTok the video to publish, so
        # that stays ambiguous when its response is lost.
        init_body = self._init_video_publish(
            access_token, payload, path=self._video_init_path(inbox), ambiguous_on_failure=False
        )
        upload_url = init_body.get("data", {}).get("upload_url")
        publish_id = init_body.get("data", {}).get("publish_id", "")

        if not upload_url:
            raise PublishError(
                "TikTok did not return an upload_url",
                platform=self.platform_name,
                raw_response=init_body,
            )

        # Step 2: stream the video binary to TikTok's presigned URL. The file
        # object is handed to httpx as-is so it is read in chunks off disk —
        # reading it into a bytes object first put the whole video in RSS, which
        # is what took the worker dyno over its memory quota.
        with open(video_path, "rb") as video:
            self._request(
                "PUT",
                upload_url,
                headers={
                    "Content-Type": content_type,
                    "Content-Length": str(video_size),
                    "Content-Range": f"bytes 0-{video_size - 1}/{video_size}",
                },
                data=video,
                timeout=120.0,
            )

        return PublishResult(
            platform_post_id=publish_id,
            extra=init_body.get("data", {}),
        )

    def check_publish_status(self, access_token: str, handle: str) -> PublishStatus:
        """Report what TikTok did with an accepted upload.

        ``handle`` is the ``publish_id`` from ``/post/publish/video/init/``.
        A transport failure is *not* swallowed here: the caller needs to tell
        "TikTok says it failed" apart from "we could not reach TikTok", and only
        the first of those is a reason to fail the post.
        """
        data = self._fetch_publish_status(access_token, handle)
        status = data.get("status") or ""
        state = PUBLISH_STATE_BY_TIKTOK_STATUS.get(status, PublishState.PENDING)

        video_id = ""
        if state is PublishState.COMPLETE:
            ids = data.get("publicaly_available_post_id") or []
            # Defensive: some TikTok response variants return a bare string.
            video_id = ids if isinstance(ids, str) else (ids[0] if ids else "")

        error = ""
        if state is PublishState.FAILED:
            reason = data.get("fail_reason") or "no reason given"
            error = f"TikTok could not process the video ({reason})."
        elif state is PublishState.INBOX:
            error = INBOX_HINT

        return PublishStatus(state=state, platform_post_id=str(video_id or ""), error=error, raw=data)

    # ------------------------------------------------------------------
    # Analytics
    # ------------------------------------------------------------------

    def _fetch_publish_status(self, access_token: str, publish_id: str) -> dict:
        """Fetch the publish status payload for an in-flight publish job.

        Returns the ``data`` block of ``/v2/post/publish/status/fetch/``.
        Once ``status == 'PUBLISH_COMPLETE'``, the payload contains
        ``publicaly_available_post_id`` — TikTok's official video ID list
        (the typo in the field name is part of TikTok's API contract).
        """
        resp = self._request(
            "POST",
            f"{API_BASE}/post/publish/status/fetch/",
            access_token=access_token,
            json={"publish_id": publish_id},
            ambiguous_on_failure=False,
        )
        return resp.json().get("data", {}) or {}

    def _resolve_video_id(self, access_token: str, post_id: str) -> str | None:
        """Resolve a stored ``platform_post_id`` to a TikTok video ID.

        :meth:`publish_post` stores the Content Posting API ``publish_id``
        (``v_pub_url~…``, ``v_pub_file~…``, or — if the direct-post audit
        downgrades the request — ``v_inbox_url~…`` / ``v_inbox_file~…``)
        as ``platform_post_id``. The analytics endpoint
        ``/v2/video/query/`` only accepts the final 19-digit numeric video
        ID, so anything that isn't already a bare numeric is treated as a
        publish handle and resolved via the publish status endpoint.

        Returns the original input if it's already a numeric video ID,
        the resolved video ID once the publish finalises, or ``None``
        when the publish is still in flight or terminally failed (caller
        treats ``None`` as "no data yet").
        """
        # Defensive: a stored NULL would AttributeError on the call below;
        # the outer except would misclassify via ``_is_insufficient_scope``
        # and silently break analytics for the post. Bail explicitly.
        if not post_id:
            return None

        # Positive identification: TikTok video IDs are 19-digit numerics.
        # Anything else (any ``v_…`` publish-handle prefix that exists today
        # or that TikTok adds tomorrow) goes through status resolution.
        if post_id.isdigit():
            return post_id

        status_data = self._fetch_publish_status(access_token, post_id)
        if status_data.get("status") != "PUBLISH_COMPLETE":
            # PROCESSING_UPLOAD / SEND_TO_USER_INBOX / FAILED / EXPIRED —
            # no video ID to query (yet, or ever). The sync layer will keep
            # polling until the post falls out of the 90-day cadence window.
            return None
        video_ids = status_data.get("publicaly_available_post_id") or []
        if isinstance(video_ids, str):
            # Defensive: some TikTok response variants return a bare string.
            return video_ids or None
        return video_ids[0] if video_ids else None

    def get_post_metrics(self, access_token: str, post_id: str) -> PostMetrics:
        """Per-video stats from the ``/v2/video/query/`` endpoint.

        Requires the ``video.list`` scope. TikTok returns lifetime totals
        (view/like/comment/share counts) — the sync layer snapshots them
        per day, so the chart series is built from successive captures.

        ``post_id`` may be either a TikTok video ID or a Content Posting
        API publish handle (what :meth:`publish_post` returns); publish
        handles are resolved to the underlying video ID via the publish
        status endpoint before the analytics call.

        Returns an empty :class:`PostMetrics` if TikTok has no record of
        the video (deleted, publish not yet complete, or not yet visible
        to the API).
        """
        video_id = self._resolve_video_id(access_token, post_id)
        if not video_id:
            # Publish still processing or failed — nothing to snapshot yet.
            return PostMetrics()

        resp = self._request(
            "POST",
            f"{API_BASE}/video/query/",
            access_token=access_token,
            params={"fields": "id,view_count,like_count,comment_count,share_count"},
            json={"filters": {"video_ids": [video_id]}},
            ambiguous_on_failure=False,
        )
        body = resp.json()
        videos = body.get("data", {}).get("videos", []) or []
        if not videos:
            return PostMetrics()

        video = videos[0]
        # ``engagements`` is intentionally omitted: the catalog's
        # ``"engagement"`` metric is a derived rate computed by
        # :func:`apps.analytics.derive.engagement_rate` from the raw parts
        # (likes/comments/shares + a denom), so populating the dataclass
        # field would be dead computation — the snapshot mapper has no
        # mapping for it.
        return PostMetrics(
            video_views=int(video.get("view_count", 0) or 0),
            likes=int(video.get("like_count", 0) or 0),
            comments=int(video.get("comment_count", 0) or 0),
            shares=int(video.get("share_count", 0) or 0),
        )

    def get_account_metrics(self, access_token: str, date_range: tuple[datetime, datetime]) -> AccountMetrics:
        """TikTok account-level analytics are intentionally disabled.

        The implemented TikTok analytics surface is video-only and relies on
        ``video.list``. Follower totals require ``user.info.stats``, which we
        do not request, so this returns an empty metrics object.
        """
        del access_token, date_range
        return AccountMetrics(followers=None)

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    def revoke_token(self, access_token: str) -> bool:
        try:
            self._request(
                "POST",
                f"{API_BASE}/oauth/revoke/",
                data={
                    "client_key": self.credentials["client_key"],
                    "token": access_token,
                },
            )
            return True
        except Exception:
            logger.exception("Failed to revoke TikTok token")
            return False
