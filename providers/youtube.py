"""YouTube Data API v3 provider."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode, urlsplit

import httpx

from .base import SocialProvider
from .exceptions import (
    APIError,
    OAuthError,
    ProviderError,
    PublicProfileNotFoundError,
    PublishError,
    QuotaExceededError,
    TokenExpiredError,
)
from .google_errors import (
    AUTH_REASONS,
    QUOTA_REASONS,
    THROTTLE_REASONS,
    google_error_reasons,
    next_google_quota_reset,
)
from .types import (
    AccountMetrics,
    AccountProfile,
    AuthType,
    CommentResult,
    InboxMessage,
    MediaRules,
    MediaType,
    OAuthTokens,
    PostMetrics,
    PostType,
    PublicProfileMetrics,
    PublishContent,
    PublishResult,
    RateLimitConfig,
    ReplyResult,
    ThreadCapability,
    ThreadMode,
)

logger = logging.getLogger(__name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API_BASE = "https://www.googleapis.com/youtube/v3"
UPLOAD_BASE = "https://www.googleapis.com/upload/youtube/v3"
ANALYTICS_BASE = "https://youtubeanalytics.googleapis.com/v2"

# ``videos.insert``: both the resumable-session POST and the PUT that carries
# the bytes, whose Location URL keeps this path and adds an ``upload_id``.
# Matched on the path rather than the whole upload host, because
# ``thumbnails.set`` lives under ``/upload/`` too but is charged to the regular
# pool.
_VIDEOS_INSERT_PATH = urlsplit(f"{UPLOAD_BASE}/videos").path

# Resumable upload (``videos.insert``). Chunks must be a multiple of 256 KB; 8 MB
# keeps a worker's memory flat and loses little on a dropped connection. A
# session URI outlives the retry ladder comfortably, so it is remembered for a day.
UPLOAD_CHUNK_SIZE = 32 * 256 * 1024
UPLOAD_SESSION_TTL = 24 * 3600
UPLOAD_RESUME_ATTEMPTS = 4


class _UploadSessionExpiredError(Exception):
    """YouTube answered 404 for the resumable session: it expired, so a new one is needed."""


# YouTube Analytics caps a ``filters=video==<id>,<id>,...`` list at 500 IDs
# per request. Larger inputs to :meth:`YouTubeProvider.get_post_analytics`
# are split into multiple requests transparently.
_ANALYTICS_VIDEO_FILTER_CHUNK = 500

# ``videos.list`` accepts up to 50 ids in one ``id=a,b,c`` request and charges
# the same 1 quota unit as a single-id call, so batching is a straight 50x
# reduction in quota spend for the per-post metrics sweep.
_DATA_API_VIDEO_ID_CHUNK = 50

# How long to stand down after a per-second throttle (as opposed to a spent
# daily quota). Long enough for a burst to clear, short enough that a momentary
# spike doesn't cost the rest of the day's syncing.
_THROTTLE_COOLDOWN = timedelta(minutes=5)

# Bounds on a routine comment poll. Both exist because ``commentThreads.list``
# charges a quota unit per page against a budget the whole deployment shares,
# and an unbounded walk of a channel's history repeated all day is what spends
# it. See :meth:`YouTubeProvider.get_messages`.
#
# The lookback is the width of the early exit, not a guess at clock skew: a
# comment thread sorts by its *top-level* comment, so a thread answered a minute
# ago can sit behind threads published hours later. Two hours buys several pages
# of slack on a busy channel; anything a reply older than that needs is the
# weekly deep sweep's job, because no finite lookback catches a reply to a
# year-old video.
_COMMENT_LOOKBACK = timedelta(hours=2)

# The backstop for a channel busy enough that even its recent traffic runs past
# the lookback. Five pages is 500 threads — far more than a 30-minute window
# produces on any real channel, so hitting it means something unusual.
_MAX_COMMENT_PAGES = 5

# The deep sweep's ceiling. Larger, because walking further back is its whole
# job — but not absent: without a cap a channel with 50,000 threads spends 500
# units in one synchronous call inside a 5-minute cycle, which is the exact
# failure the bounds above exist to prevent. 50 pages reaches 5,000 threads,
# deep enough that anything past it is history no comment poll should be
# reconstructing.
_MAX_DEEP_COMMENT_PAGES = 50


def _quota_scope_for(url: str) -> str:
    """Which of YouTube's separately metered budgets a request to ``url`` spends.

    Read off the request because the response cannot say: Google refuses a
    spent budget with the same ``quotaExceeded`` / ``youtube.quota`` whichever
    one it is. The request can — only the Analytics host spends the Analytics
    quota, and only ``videos.insert`` spends the upload bucket.

    Each is kept apart because a block is keyed on this name, and a refusal
    filed under the wrong one stops calls that still have budget: the
    Analytics sync because the Data API ran dry, or the inbox, analytics and
    health checks for the rest of the day because 100 uploads went out.

    ``search.list`` has its own bucket too, but nothing here calls it.
    """
    if url.startswith(ANALYTICS_BASE):
        return "analytics"
    if urlsplit(url).path.rstrip("/") == _VIDEOS_INSERT_PATH:
        return "upload"
    return "data"


class YouTubeMessageBatch(list[InboxMessage]):
    """Messages plus the cursor left by a page cap, if the walk is incomplete."""

    def __init__(self, messages: list[InboxMessage], next_page_token: str | None = None):
        super().__init__(messages)
        self.next_page_token = next_page_token


# YouTube takes uploads of up to 12 hours, but channels that haven't verified a
# phone number stop at 15 minutes — which this can't see, so it only warns.
# https://support.google.com/youtube/answer/71673
# Shorts run up to 3 minutes and are square or vertical; anything else uploads
# as a regular video rather than failing.
# https://support.google.com/youtube/answer/15424877
YOUTUBE_MEDIA_RULES = {
    PostType.VIDEO: MediaRules(
        source="https://support.google.com/youtube/answer/71673",
        published_items=1,
        media_types=frozenset({"video"}),
        max_duration=15 * 60,
        advisory=frozenset({"max_duration"}),
    ),
    PostType.SHORT: MediaRules(
        source="https://support.google.com/youtube/answer/15424877",
        published_items=1,
        media_types=frozenset({"video"}),
        max_aspect_ratio=1.0,
        max_duration=3 * 60,
        advisory=frozenset({"aspect_ratio", "max_duration"}),
    ),
}


_CHANNEL_ID = re.compile(r"UC[A-Za-z0-9_-]{22}")


def _int_or_none(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class YouTubeProvider(SocialProvider):
    """YouTube Data API v3 provider using Google OAuth 2.0."""

    profile_url_template = "https://www.youtube.com/@{handle}"

    media_rules = YOUTUBE_MEDIA_RULES
    account_default_keys = ("privacy_status", "self_declared_made_for_kids")
    account_default_choices = {
        "privacy_status": ("public", "unlisted", "private"),
        "self_declared_made_for_kids": (True, False),
    }

    supports_public_profile_metrics = True
    competitor_handle_hint = "@channelhandle"
    # A channel id, or a handle: 3-30 characters, letters in any script (Arabic handles are common).
    competitor_handle_pattern = r"UC[A-Za-z0-9_-]{22}|[\w.·-]{3,30}"

    @classmethod
    def _competitor_handle_from_url(cls, host, segments):
        # youtube.com/channel/<id>[/videos] or youtube.com/@<handle>[/videos]
        if "channel" in segments[:-1]:
            return segments[segments.index("channel") + 1]
        return super()._competitor_handle_from_url(host, segments)

    @classmethod
    def _canonical_competitor_handle(cls, value):
        # Channel ids are case-sensitive; handles are not.
        return value if _CHANNEL_ID.fullmatch(value) else value.lower()

    # Top-level comments on the video carry up to 10,000 characters.
    thread = ThreadCapability(mode=ThreadMode.COMMENTS, max_parts=10, part_max_length=10000)

    @classmethod
    def permalink_for(cls, platform_post_id: str, account) -> str:
        video_id = (platform_post_id or "").strip()
        return f"https://www.youtube.com/watch?v={video_id}" if re.fullmatch(r"[A-Za-z0-9_-]{6,20}", video_id) else ""

    # Unverified: Sprout Social's 2026 study has no YouTube times, Hootsuite's page is gone and Buffer's
    # (Mar 2026) contradicts itself on long-form hours, so the generic DEFAULT_BEST_TIME_WINDOWS apply
    # (checked 2026-10-24).

    writing_style = "A video description: what viewers will get in the first lines, then detail, with a call to subscribe. A few hashtags at the end."

    # Captions come from the official captions endpoints, for videos on the
    # connected account's own channel only.
    owned_video_transcripts = True

    _VIDEO_HOSTS = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"})
    _VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

    @classmethod
    def video_id_from_url(cls, url: str) -> str:
        """The 11-character video id in a youtube.com / youtu.be watch, shorts, live or embed link."""
        try:
            parts = urlsplit((url or "").strip())
        except ValueError:
            return ""
        host = (parts.hostname or "").lower()
        segments = [p for p in parts.path.split("/") if p]
        candidate = ""
        if host == "youtu.be" and segments:
            candidate = segments[0]
        elif host in cls._VIDEO_HOSTS:
            if parts.path.rstrip("/") == "/watch":
                candidate = dict(pair.split("=", 1) for pair in parts.query.split("&") if "=" in pair).get("v", "")
            elif len(segments) >= 2 and segments[0] in {"shorts", "live", "embed", "v"}:
                candidate = segments[1]
        return candidate if cls._VIDEO_ID_RE.match(candidate) else ""

    def fetch_owned_video_transcript(self, access_token: str, *, video_id: str, channel_id: str) -> str | None:
        """The video's caption track as plain text, or None when it isn't ours or has no usable captions.

        Three official calls: ``videos.list`` (1 quota unit) to confirm the video
        is on ``channel_id``, ``captions.list`` (50) to pick a track, and
        ``captions.download`` (200, ``tfmt=srt``) to read it. ``captions.download``
        needs permission to edit the video, so the API refuses it for anyone
        else's video; that refusal (403/404) is an ordinary "no transcript".
        Sources: https://developers.google.com/youtube/v3/docs/captions/list and
        https://developers.google.com/youtube/v3/docs/captions/download
        """
        video = self._request(
            "GET", f"{API_BASE}/videos", access_token=access_token, params={"part": "snippet", "id": video_id}
        ).json()
        items = video.get("items") or []
        if not items or (items[0].get("snippet") or {}).get("channelId") != channel_id:
            return None
        listing = self._request(
            "GET", f"{API_BASE}/captions", access_token=access_token, params={"part": "snippet", "videoId": video_id}
        ).json()
        tracks = [t for t in listing.get("items") or [] if not (t.get("snippet") or {}).get("isDraft")]
        if not tracks:
            return None
        # A human-made track beats automatic speech recognition.
        tracks.sort(key=lambda t: (t.get("snippet") or {}).get("trackKind") == "asr")
        try:
            response = self._request(
                "GET", f"{API_BASE}/captions/{tracks[0]['id']}", access_token=access_token, params={"tfmt": "srt"}
            )
        except APIError as exc:
            if exc.status_code in (403, 404):
                return None
            raise
        return response.text

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    @property
    def platform_name(self) -> str:
        return "YouTube"

    @property
    def auth_type(self) -> AuthType:
        return AuthType.OAUTH2

    @property
    def max_caption_length(self) -> int:
        return 5000

    @property
    def supported_post_types(self) -> list[PostType]:
        return [PostType.VIDEO, PostType.SHORT]

    @property
    def supported_media_types(self) -> list[MediaType]:
        return [MediaType.MP4, MediaType.MOV]

    @property
    def required_scopes(self) -> list[str]:
        scopes = [
            "https://www.googleapis.com/auth/youtube.upload",
            "https://www.googleapis.com/auth/youtube.readonly",
            "https://www.googleapis.com/auth/youtube.force-ssl",
        ]
        if self.include_analytics_scopes:
            scopes.extend(self.analytics_only_scopes)
        return scopes

    @property
    def analytics_only_scopes(self) -> list[str]:
        # Required for the YouTube Analytics API (watch time, retention,
        # demographics). Only requested when analytics is enabled so the
        # Google OAuth consent screen doesn't list it when not needed.
        return ["https://www.googleapis.com/auth/yt-analytics.readonly"]

    @property
    def rate_limits(self) -> RateLimitConfig:
        # Since 2026-06-01 the Data API meters three daily budgets, each per
        # Google Cloud project (so shared by every channel on this deployment)
        # and each reset at midnight US/Pacific: ``videos.insert`` and
        # ``search.list`` get 100 calls apiece at 1 unit a call, and every other
        # method shares the regular 10,000 units. An upload therefore no longer
        # spends the pool the inbox, analytics and health check live on — its
        # custom thumbnail (``thumbnails.set``, 50 units) and first comment
        # (``commentThreads.insert``, 50) still do.
        return RateLimitConfig(
            requests_per_hour=600,
            requests_per_day=10000,
            publish_per_day=100,
            extra={"quota_units_per_day": 10000, "videos_insert_per_day": 100, "search_list_per_day": 100},
        )

    # ------------------------------------------------------------------
    # OAuth
    # ------------------------------------------------------------------

    def get_auth_url(self, redirect_uri: str, state: str, code_verifier: str | None = None) -> str:
        params = {
            "client_id": self.credentials["client_id"],
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": " ".join(self.required_scopes),
            "response_type": "code",
            "access_type": "offline",
            "prompt": "consent",
        }
        return f"{AUTH_URL}?{urlencode(params)}"

    def exchange_code(self, code: str, redirect_uri: str, code_verifier: str | None = None) -> OAuthTokens:
        resp = self._request(
            "POST",
            TOKEN_URL,
            data={
                "code": code,
                "client_id": self.credentials["client_id"],
                "client_secret": self.credentials["client_secret"],
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        body = resp.json()
        if "access_token" not in body:
            raise OAuthError(
                f"YouTube token exchange failed: {body}",
                platform=self.platform_name,
                raw_response=body,
            )
        return OAuthTokens(
            access_token=body["access_token"],
            refresh_token=body.get("refresh_token"),
            expires_in=body.get("expires_in"),
            scope=body.get("scope"),
            raw_response=body,
        )

    def refresh_token(self, refresh_token: str) -> OAuthTokens:
        resp = self._request(
            "POST",
            TOKEN_URL,
            data={
                "refresh_token": refresh_token,
                "client_id": self.credentials["client_id"],
                "client_secret": self.credentials["client_secret"],
                "grant_type": "refresh_token",
            },
        )
        body = resp.json()
        if "access_token" not in body:
            raise OAuthError(
                f"YouTube token refresh failed: {body}",
                platform=self.platform_name,
                raw_response=body,
            )
        return OAuthTokens(
            access_token=body["access_token"],
            refresh_token=body.get("refresh_token", refresh_token),
            expires_in=body.get("expires_in"),
            scope=body.get("scope"),
            raw_response=body,
        )

    # ------------------------------------------------------------------
    # Profile
    # ------------------------------------------------------------------

    def get_profile(self, access_token: str, *, channel_id: str | None = None) -> AccountProfile:
        resp = self._request(
            "GET",
            f"{API_BASE}/channels",
            access_token=access_token,
            params={"part": "snippet,statistics", "mine": "true"},
        )
        body = resp.json()
        items = body.get("items", [])
        if not items:
            return AccountProfile(platform_id="", name="Unknown")

        if channel_id:
            while not any(item.get("id") == channel_id for item in items) and body.get("nextPageToken"):
                resp = self._request(
                    "GET",
                    f"{API_BASE}/channels",
                    access_token=access_token,
                    params={"part": "snippet,statistics", "mine": "true", "pageToken": body["nextPageToken"]},
                )
                body = resp.json()
                items = body.get("items", [])
            channel = next((item for item in items if item.get("id") == channel_id), None)
            if channel is None:
                return AccountProfile(platform_id="", name="Unknown")
        else:
            channel = items[0]
        snippet = channel.get("snippet", {})
        stats = channel.get("statistics", {})
        thumbnails = snippet.get("thumbnails", {})
        avatar = thumbnails.get("default", {}).get("url") or thumbnails.get("medium", {}).get("url")
        return AccountProfile(
            platform_id=channel["id"],
            name=snippet.get("title", ""),
            handle=snippet.get("customUrl"),
            avatar_url=avatar,
            follower_count=int(stats.get("subscriberCount", 0)),
            extra={
                "view_count": int(stats.get("viewCount", 0)),
                "video_count": int(stats.get("videoCount", 0)),
            },
        )

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    def publish_post(self, access_token: str, content: PublishContent) -> PublishResult:
        if content.post_type not in (PostType.VIDEO, PostType.SHORT):
            raise PublishError(
                "YouTube only supports VIDEO and SHORT post types",
                platform=self.platform_name,
                retryable=False,
            )

        title = content.title or content.text or ""
        description = content.description or content.text or ""

        # Shorts: add #Shorts tag if not already present
        if content.post_type == PostType.SHORT and "#Shorts" not in title:
            title = f"{title} #Shorts".strip()

        privacy_status = content.extra.get("privacy_status", "public")
        made_for_kids = content.extra.get("self_declared_made_for_kids", False)
        category_id = content.extra.get("category_id", "22")  # 22 = People & Blogs
        tags = content.extra.get("tags", [])

        metadata = {
            "snippet": {
                "title": title[:100],
                "description": description[: self.max_caption_length],
                "tags": tags,
                "categoryId": category_id,
            },
            "status": {
                "privacyStatus": privacy_status,
                "selfDeclaredMadeForKids": made_for_kids,
            },
        }

        if not content.media_files:
            raise PublishError(
                "No video file provided (media_files required)",
                platform=self.platform_name,
                retryable=False,
            )

        video_path = content.media_files[0]
        video_id = self._upload_video(access_token, video_path, metadata, content)
        if not video_id:
            # The upload finished but YouTube named no video. It may exist, so
            # this is not safe to retry.
            raise APIError(
                "YouTube accepted the upload but did not return a video id",
                platform=self.platform_name,
                ambiguous_write=True,
            )

        # Step 3 (optional): upload custom thumbnail
        thumbnail_path = content.extra.get("thumbnail_file")
        if video_id and thumbnail_path:
            try:
                with open(thumbnail_path, "rb") as tf:
                    thumb_data = tf.read()
                # Guess content type from extension
                ext = thumbnail_path.lower().rsplit(".", 1)[-1]
                thumb_ct = "image/png" if ext == "png" else "image/jpeg"
                self._request(
                    "POST",
                    f"{UPLOAD_BASE}/thumbnails/set",
                    access_token=access_token,
                    params={"videoId": video_id, "uploadType": "media"},
                    headers={
                        "Content-Type": thumb_ct,
                        "Content-Length": str(len(thumb_data)),
                    },
                    data=thumb_data,
                    timeout=60.0,
                )
            except Exception:
                logger.exception("Custom thumbnail upload failed for video %s", video_id)

        return PublishResult(
            platform_post_id=video_id,
            url=f"https://www.youtube.com/watch?v={video_id}" if video_id else None,
            # The ID above is sufficient. Persisting the resource here
            # also copies it into composer settings, clones and history.
            extra={},
        )

    # ------------------------------------------------------------------
    # Resumable upload
    # ------------------------------------------------------------------
    # https://developers.google.com/youtube/v3/guides/using_resumable_upload_protocol

    def _upload_video(self, access_token: str, video_path: str, metadata: dict, content: PublishContent) -> str:
        """Upload ``video_path`` through a resumable session and return the new video id.

        The session URI is remembered in ``content.upload_cache``, so a retry
        after a dropped connection asks YouTube how much it already holds and
        sends only the rest, instead of starting the whole file again. A
        transport error or 5xx on a chunk is recovered from inside this loop
        (query the received range, resume), so none of these steps is an
        ambiguous write: YouTube answers the status query with the finished
        video if the last chunk did land.
        """
        size = os.path.getsize(video_path)
        key = self._upload_session_key(access_token, content.idempotency_key, video_path, size)
        cache = content.upload_cache
        session_uri = cache.get(key) if cache is not None and key else None

        failures = 0
        while True:
            try:
                if session_uri is None:
                    session_uri = self._start_upload_session(access_token, metadata, size)
                    if cache is not None and key:
                        cache.set(key, session_uri, UPLOAD_SESSION_TTL)
                done, offset, body = self._upload_status(session_uri, size)
                if done:
                    return body.get("id", "")
                return self._send_chunks(session_uri, video_path, size, offset)
            except _UploadSessionExpiredError:
                # 404: Google no longer knows this session. Start over.
                session_uri = None
                if cache is not None and key:
                    cache.set(key, "", 1)
            except (APIError, PublishError) as exc:
                transient = isinstance(exc, APIError) and (exc.status_code is None or exc.status_code >= 500)
                failures += 1
                if not transient or failures >= UPLOAD_RESUME_ATTEMPTS:
                    if isinstance(exc, APIError):
                        # The session is saved, so the engine's retry resumes it.
                        exc.ambiguous_write = False
                    raise
                if content.should_stop is not None and content.should_stop():
                    # Shutting down: leave the saved session for the rescheduled attempt.
                    if isinstance(exc, APIError):
                        exc.ambiguous_write = False
                    raise
                time.sleep(min(2**failures, 30))

    @staticmethod
    def _upload_session_key(access_token: str, publish_key: str | None, video_path: str, size: int) -> str | None:
        """Where a session is remembered: scoped to one publish, the account (token hashed) and the file."""
        if not publish_key:
            return None
        account = hashlib.sha256(access_token.encode()).hexdigest()[:32]
        publish = hashlib.sha256(publish_key.encode()).hexdigest()[:32]
        with open(video_path, "rb") as f:
            digest = hashlib.file_digest(f, "sha256").hexdigest()
        return f"youtube-session:{account}:{publish}:{size}:{digest}"

    def _start_upload_session(self, access_token: str, metadata: dict, size: int) -> str:
        resp = self._request(
            "POST",
            f"{UPLOAD_BASE}/videos",
            access_token=access_token,
            params={"uploadType": "resumable", "part": "snippet,status"},
            headers={"X-Upload-Content-Length": str(size), "X-Upload-Content-Type": "video/*"},
            json=metadata,
        )
        uri = resp.headers.get("Location")
        if not uri:
            raise PublishError("YouTube did not return a resumable upload URI", platform=self.platform_name)
        return uri

    def _upload_status(self, session_uri: str, size: int) -> tuple[bool, int, dict]:
        """Ask YouTube what the session holds: ``(finished, next_offset, video_body)``."""
        try:
            resp = self._request(
                "PUT",
                session_uri,
                headers={"Content-Range": f"bytes */{size}", "Content-Length": "0"},
                data=b"",
            )
        except APIError as exc:
            if exc.status_code == 404:
                raise _UploadSessionExpiredError from exc
            raise
        if resp.status_code in (200, 201):
            return True, size, resp.json()
        return False, self._received_offset(resp), {}

    @staticmethod
    def _received_offset(resp) -> int:
        """The first byte YouTube still needs, from a 308's ``Range: bytes=0-N`` header (absent: nothing yet)."""
        match = re.fullmatch(r"bytes=0-(\d+)", (resp.headers.get("Range") or "").strip())
        return int(match.group(1)) + 1 if match else 0

    def _send_chunks(self, session_uri: str, video_path: str, size: int, offset: int) -> str:
        """PUT the file from ``offset`` in 256 KB-multiple chunks; return the video id once complete."""
        with open(video_path, "rb") as video:
            while offset < size:
                video.seek(offset)
                chunk = video.read(UPLOAD_CHUNK_SIZE)
                last = offset + len(chunk) - 1
                try:
                    resp = self._request(
                        "PUT",
                        session_uri,
                        headers={
                            "Content-Length": str(len(chunk)),
                            "Content-Range": f"bytes {offset}-{last}/{size}",
                        },
                        data=chunk,
                        timeout=300.0,
                    )
                except APIError as exc:
                    if exc.status_code == 404:
                        raise _UploadSessionExpiredError from exc
                    raise
                if resp.status_code in (200, 201):
                    return resp.json().get("id", "")
                offset = self._received_offset(resp)
        # Everything sent but no completion response: ask for the finished video.
        done, _, body = self._upload_status(session_uri, size)
        return body.get("id", "") if done else ""

    # ------------------------------------------------------------------
    # Comments
    # ------------------------------------------------------------------

    def publish_comment(self, access_token: str, post_id: str, text: str) -> CommentResult:
        resp = self._request(
            "POST",
            f"{API_BASE}/commentThreads",
            access_token=access_token,
            params={"part": "snippet"},
            json={
                "snippet": {
                    "videoId": post_id,
                    "topLevelComment": {
                        "snippet": {
                            "textOriginal": text,
                        }
                    },
                }
            },
        )
        body = resp.json()
        comment_id = body.get("id", "")
        return CommentResult(
            platform_comment_id=comment_id,
            extra=body,
        )

    # ------------------------------------------------------------------
    # Inbox
    # ------------------------------------------------------------------

    def get_messages(
        self,
        access_token: str,
        since: datetime | None = None,
        *,
        deep: bool = False,
        page_token: str | None = None,
    ) -> YouTubeMessageBatch:
        """Comment threads on this channel, newest first.

        ``commentThreads.list`` costs a quota unit per page, and the Data API
        grants 10,000 units a day to the whole OAuth client — every account
        connected to this deployment, not each one. So walking a channel's
        entire comment history on a poll that repeats all day is the one thing
        this must not do: ten channels deep-paging every five minutes spend the
        day's budget before noon, and then nearly everything YouTube stops —
        the inbox, analytics, reconnecting the accounts, and the thumbnail and
        first comment on each new video — until the quota rolls over at
        midnight US/Pacific. Only the upload itself carries on, because
        ``videos.insert`` draws on a bucket of its own.

        A normal poll therefore stops early. ``order=time`` returns threads
        newest first, so once a page ends older than ``since`` there is nothing
        newer behind it. ``_COMMENT_LOOKBACK`` widens that edge, because a
        thread sorts by its *top-level* comment: one answered today can sit
        pages deep behind its original publish date. ``_MAX_COMMENT_PAGES`` is
        the backstop for a channel whose recent traffic alone runs long.

        ``deep=True`` drops the early exit for the weekly sweep and raises the
        per-call page cap. A capped batch returns ``next_page_token`` so the
        caller can resume later without spending an unbounded number of units
        in one cycle. It still honours ``since`` when deciding what to emit.

        The two ``since`` filters below are deliberately separate. A thread
        whose top-level comment predates ``since`` can still carry a reply
        posted seconds ago, so skipping the whole thread — as this did — lost
        the reply with it.
        """
        # Counted before the call, not after, so a request that raises still
        # shows up in the tally — and counted with ``+=`` so an auth retry adds
        # to what the first attempt bought instead of replacing it. A refusal on
        # quota is not charged upstream, so this is a ceiling on the real spend,
        # which is the safe direction to be wrong in. See
        # SocialProvider.last_call_quota_units.
        self.last_call_quota_units += 1
        # Resolve channel ID
        ch_resp = self._request(
            "GET",
            f"{API_BASE}/channels",
            access_token=access_token,
            params={"part": "id", "mine": "true"},
        )
        ch_items = ch_resp.json().get("items", [])
        if not ch_items:
            return YouTubeMessageBatch([])
        channel_id = ch_items[0]["id"]

        # How far back a page may reach before paging stops. ``None`` means "no
        # floor" — a first sync (no ``since``) and the deep sweep both want the
        # page cap, or nothing at all, to be what ends the walk.
        cutoff = None if (since is None or deep) else since - _COMMENT_LOOKBACK
        max_pages = _MAX_DEEP_COMMENT_PAGES if deep else _MAX_COMMENT_PAGES

        messages: list[InboxMessage] = []
        pages = 0
        continuation: str | None = None

        while True:
            params: dict = {
                "part": "snippet,replies",
                "allThreadsRelatedToChannelId": channel_id,
                "maxResults": 100,
                "order": "time",
            }
            if page_token:
                params["pageToken"] = page_token

            self.last_call_quota_units += 1
            resp = self._request(
                "GET",
                f"{API_BASE}/commentThreads",
                access_token=access_token,
                params=params,
            )
            body = resp.json()
            pages += 1

            # The oldest thread on this page, for the early exit below. Read
            # from the response rather than tracked through the loop so a
            # thread skipped for emission still counts toward how far back the
            # page reached.
            oldest_on_page: datetime | None = None

            for thread in body.get("items", []):
                top_snippet = thread["snippet"]["topLevelComment"]["snippet"]
                published = datetime.fromisoformat(top_snippet["publishedAt"].replace("Z", "+00:00"))
                if oldest_on_page is None or published < oldest_on_page:
                    oldest_on_page = published

                top_comment_id = thread["snippet"]["topLevelComment"]["id"]
                video_id = top_snippet["videoId"]

                if not since or published >= since:
                    messages.append(
                        InboxMessage(
                            platform_message_id=top_comment_id,
                            sender_id=top_snippet.get("authorChannelId", {}).get("value", ""),
                            sender_name=top_snippet.get("authorDisplayName", ""),
                            text=top_snippet.get("textDisplay", ""),
                            timestamp=published,
                            message_type="comment",
                            extra={
                                "video_id": video_id,
                                "comment_id": top_comment_id,
                                "sender_avatar_url": top_snippet.get("authorProfileImageUrl", ""),
                            },
                        )
                    )

                # YouTube embeds only a subset for threads with many replies.
                # A full history walk must fetch the rest or its new replies to
                # old threads can remain invisible despite reaching the thread.
                replies = thread.get("replies", {}).get("comments", [])
                if (deep or since is None) and thread["snippet"].get("totalReplyCount", 0) > len(replies):
                    replies = self._all_comment_replies(access_token, top_comment_id)
                for reply in replies:
                    r_snippet = reply["snippet"]
                    r_published = datetime.fromisoformat(r_snippet["publishedAt"].replace("Z", "+00:00"))
                    if since and r_published < since:
                        continue
                    messages.append(
                        InboxMessage(
                            platform_message_id=reply["id"],
                            sender_id=r_snippet.get("authorChannelId", {}).get("value", ""),
                            sender_name=r_snippet.get("authorDisplayName", ""),
                            text=r_snippet.get("textDisplay", ""),
                            timestamp=r_published,
                            message_type="comment",
                            extra={
                                "video_id": video_id,
                                "comment_id": reply["id"],
                                "parent_id": top_comment_id,
                                "sender_avatar_url": r_snippet.get("authorProfileImageUrl", ""),
                            },
                        )
                    )

            page_token = body.get("nextPageToken")
            if not page_token:
                break

            # Newest-first ordering means a page that already reaches past the
            # cutoff has nothing newer behind it.
            if cutoff is not None and oldest_on_page is not None and oldest_on_page < cutoff:
                break

            if pages >= max_pages:
                # Not an error — a busy channel legitimately runs long. Worth
                # saying out loud because a channel that caps every poll is one
                # whose older comments only ever reach the inbox via the weekly
                # deep sweep, and one that caps the *sweep* has history no
                # comment poll will ever reach.
                logger.warning(
                    "YouTube comment poll hit the %d-page cap for channel %s (deep=%s)",
                    max_pages,
                    channel_id,
                    deep,
                )
                continuation = page_token
                break

        return YouTubeMessageBatch(messages, continuation)

    def _all_comment_replies(self, access_token: str, parent_id: str) -> list[dict]:
        """Fetch every reply when ``commentThreads.list`` embeds only a subset.

        This is used for initial imports and deep sweeps, not the frequent
        routine poll. The latter stays bounded by its thread-page allowance.
        """
        replies: list[dict] = []
        page_token: str | None = None
        while True:
            params = {"part": "snippet", "parentId": parent_id, "maxResults": 100}
            if page_token:
                params["pageToken"] = page_token
            self.last_call_quota_units += 1
            body = self._request("GET", f"{API_BASE}/comments", access_token=access_token, params=params).json()
            replies.extend(body.get("items", []))
            page_token = body.get("nextPageToken")
            if not page_token:
                return replies

    def reply_to_comment(self, access_token: str, comment_id: str, text: str, extra: dict | None = None) -> ReplyResult:
        """Reply to a comment. YouTube answers on the comments endpoint, so this is the same call."""
        return self.reply_to_message(access_token, comment_id, text, extra)

    def reply_to_message(
        self,
        access_token: str,
        message_id: str,
        text: str,
        extra: dict | None = None,
        *,
        human_agent: bool = False,
    ) -> ReplyResult:
        resp = self._request(
            "POST",
            f"{API_BASE}/comments",
            access_token=access_token,
            params={"part": "snippet"},
            json={
                "snippet": {
                    "parentId": message_id,
                    "textOriginal": text,
                }
            },
        )
        body = resp.json()
        return ReplyResult(platform_message_id=body.get("id", ""), extra=body)

    # ------------------------------------------------------------------
    # Error classification
    # ------------------------------------------------------------------

    def _error_for_response(self, response: httpx.Response, *, now: datetime | None = None) -> ProviderError:
        """Tell Google's 403s apart.

        Google answers a spent daily quota with **403**, not 429, so the base
        class's status-code check never recognised it and every quota failure
        arrived as a generic :class:`APIError` — indistinguishable from a
        permission refusal. That is how an exhausted quota came to tell healthy
        accounts to reconnect, and how the analytics sync kept hammering an API
        that had already said "not until tomorrow".

        A genuine permission 403 (``reason: "forbidden"`` /
        ``"insufficientPermissions"``) deliberately falls through to ``super()``
        and stays an ``APIError``, because that is what
        ``apps.analytics.tasks._is_insufficient_scope`` reads to flag the
        account for reconnect.

        ``now`` exists so both deadline branches below share one clock read.
        Two live reads can straddle Pacific midnight and land a day apart, and
        a caller that needs a pinned moment (a test, say) can supply it instead
        of patching this module's imports.
        """
        body = self._safe_json(response)
        reasons = google_error_reasons(body)
        now = now or datetime.now(UTC)
        scope = _quota_scope_for(str(response.url))

        if reasons & QUOTA_REASONS:
            return QuotaExceededError(
                f"{self.platform_name} daily quota exhausted ({scope} quota)",
                resets_at=next_google_quota_reset(now),
                quota_scope=scope,
                status_code=response.status_code,
                platform=self.platform_name,
                raw_response=body,
            )

        if reasons & THROTTLE_REASONS:
            return QuotaExceededError(
                f"{self.platform_name} request rate throttled ({scope} quota)",
                resets_at=now + _THROTTLE_COOLDOWN,
                quota_scope=scope,
                status_code=response.status_code,
                platform=self.platform_name,
                raw_response=body,
            )

        if response.status_code == 401 or (reasons & AUTH_REASONS):
            return TokenExpiredError(
                f"{self.platform_name} rejected the access token",
                status_code=response.status_code,
                platform=self.platform_name,
                raw_response=body,
            )

        return super()._error_for_response(response)

    # ------------------------------------------------------------------
    # Analytics
    # ------------------------------------------------------------------

    post_metrics_batch_size = _DATA_API_VIDEO_ID_CHUNK

    def get_post_metrics(self, access_token: str, post_id: str) -> PostMetrics:
        """Per-video counts from the YouTube Data API ``videos.list?part=statistics``.

        Only the counts the Data API exposes: views, likes, comments. Watch
        time, average view percentage, and shares are intentionally absent
        — those live on the Analytics API and are batched per channel by
        :meth:`get_post_analytics`.

        A video the API doesn't return — deleted, private, or never ours —
        yields an empty :class:`PostMetrics`, which is the contract callers of
        the single-post path have always relied on. The batch path reports the
        same situation by *omitting* the id instead; see
        :meth:`get_post_metrics_batch`.
        """
        return self.get_post_metrics_batch(access_token, [post_id]).get(post_id, PostMetrics())

    def get_post_metrics_batch(self, access_token: str, post_ids: list[str]) -> dict[str, PostMetrics]:
        """Per-video counts for many videos, 50 ids per request.

        ``videos.list`` charges 1 quota unit whether it is asked for one id or
        fifty, so asking one at a time spent 50x the quota it needed to. That
        overspend is what put the daily budget within reach of a single bad
        sync loop.

        Videos the API omits (deleted, made private, not on this channel) are
        absent from the returned dict — never present with zeros, which would
        overwrite a real history with a flat line.
        """
        if not post_ids:
            return {}

        result: dict[str, PostMetrics] = {}
        for offset in range(0, len(post_ids), _DATA_API_VIDEO_ID_CHUNK):
            chunk = post_ids[offset : offset + _DATA_API_VIDEO_ID_CHUNK]
            resp = self._request(
                "GET",
                f"{API_BASE}/videos",
                access_token=access_token,
                params={"part": "statistics", "id": ",".join(chunk)},
            )
            for item in resp.json().get("items", []) or []:
                video_id = item.get("id")
                if not video_id:
                    continue
                result[video_id] = self._post_metrics_from_statistics(item.get("statistics", {}) or {})
        return result

    @staticmethod
    def _post_metrics_from_statistics(stats: dict) -> PostMetrics:
        """Build :class:`PostMetrics` from one ``videos.list`` ``statistics`` block."""
        views = int(stats.get("viewCount", 0))
        likes = int(stats.get("likeCount", 0))
        comments = int(stats.get("commentCount", 0))
        return PostMetrics(
            video_views=views,
            likes=likes,
            comments=comments,
            engagements=likes + comments,
            extra={
                "favorite_count": int(stats.get("favoriteCount", 0)),
                "reported_metrics": {
                    key: float(stats[api_key])
                    for api_key, key in (("viewCount", "views"), ("likeCount", "likes"), ("commentCount", "comments"))
                    if api_key in stats
                },
            },
        )

    def get_comments_by_id(self, access_token, comment_ids):
        """Refresh retained comments, including replies and edited text."""
        result = {}
        for offset in range(0, len(comment_ids), 50):
            ids = comment_ids[offset : offset + 50]
            try:
                response = self._request(
                    "GET",
                    f"{API_BASE}/comments",
                    access_token=access_token,
                    params={"part": "snippet", "id": ",".join(ids), "textFormat": "plainText"},
                )
            except APIError as exc:
                if exc.status_code != 404:
                    raise
                # A single missing ID can fail the whole batch. Isolate it;
                # never delete every other comment based on that one 404.
                if len(ids) > 1:
                    split = len(ids) // 2
                    result.update(self.get_comments_by_id(access_token, ids[:split]))
                    result.update(self.get_comments_by_id(access_token, ids[split:]))
                continue
            result.update({item["id"]: item.get("snippet", {}) for item in response.json().get("items", [])})
        return result

    _CHANNEL_METRICS = {
        "views": "views",
        "likes": "likes",
        "comments": "comments",
        "shares": "shares",
        "estimatedMinutesWatched": "watch_time",
        "averageViewPercentage": "avg_view_pct",
        "subscribersGained": "subscribers",
    }

    def _channel_report(self, access_token, channel_id, start, end, *, daily=False):
        """Parse columns by name; only returned numeric cells are observations."""
        import math
        from datetime import date

        params = {
            "ids": f"channel=={channel_id}",
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "metrics": ",".join(self._CHANNEL_METRICS),
        }
        if daily:
            params.update(dimensions="day", sort="day", maxResults=200)
        result = []
        while True:
            body = self._request("GET", f"{ANALYTICS_BASE}/reports", access_token=access_token, params=params).json()
            columns = [h.get("name") for h in body.get("columnHeaders", [])]
            rows = body.get("rows", [])
            for cells in rows:
                record = dict(zip(columns, cells, strict=False))
                metrics = {}
                for api_key, key in self._CHANNEL_METRICS.items():
                    value = record.get(api_key)
                    if value is not None:
                        try:
                            number = float(value)
                        except (TypeError, ValueError):
                            continue
                        if math.isfinite(number):
                            metrics[key] = number
                if daily:
                    if record.get("day"):
                        day = date.fromisoformat(record["day"])
                        if start <= day <= end:
                            result.append({"date": day, "metrics": metrics})
                else:
                    return metrics
            if not daily or len(rows) < 200:
                return result if daily else {}
            params["startIndex"] = params.get("startIndex", 1) + len(rows)

    def get_channel_daily_metrics(self, access_token, channel_id, start, end):
        return self._channel_report(access_token, channel_id, start, end, daily=True)

    def get_channel_period_metrics(self, access_token, channel_id, start, end):
        return self._channel_report(access_token, channel_id, start, end)

    def public_profile_metrics(self, handle: str, using_account, access_token: str = "") -> PublicProfileMetrics:
        """A channel's public statistics from ``channels.list`` with the connected account's token.

        The newest uploads (``playlistItems.list`` on the channel's uploads
        playlist, then ``videos.list``) give the engagement average and posting
        rate. Three quota units in all, added to ``last_call_quota_units``.
        A channel that hides its subscriber count reports no followers.
        """
        lookup = {"id": handle} if _CHANNEL_ID.fullmatch(handle) else {"forHandle": f"@{handle}"}
        self.last_call_quota_units += 1
        items = (
            self._request(
                "GET",
                f"{API_BASE}/channels",
                access_token=access_token,
                params={"part": "snippet,statistics,contentDetails", **lookup},
            )
            .json()
            .get("items", [])
        )
        if not items:
            raise PublicProfileNotFoundError(f"No YouTube channel found for {handle}.", platform="YouTube")
        channel = items[0]
        stats = channel.get("statistics") or {}
        followers = None if stats.get("hiddenSubscriberCount") else _int_or_none(stats.get("subscriberCount"))
        engagement, stamps = [], []
        uploads = ((channel.get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads")
        if uploads:
            self.last_call_quota_units += 1
            playlist = self._request(
                "GET",
                f"{API_BASE}/playlistItems",
                access_token=access_token,
                params={"part": "contentDetails", "playlistId": uploads, "maxResults": 25},
            ).json()
            video_ids = [i["contentDetails"]["videoId"] for i in playlist.get("items", []) if i.get("contentDetails")]
            if video_ids:
                self.last_call_quota_units += 1
                videos = self._request(
                    "GET",
                    f"{API_BASE}/videos",
                    access_token=access_token,
                    params={"part": "snippet,statistics", "id": ",".join(video_ids)},
                ).json()
                for video in videos.get("items", []):
                    v = video.get("statistics") or {}
                    engagement.append(int(v.get("likeCount") or 0) + int(v.get("commentCount") or 0))
                    try:
                        stamps.append(datetime.fromisoformat(video["snippet"]["publishedAt"].replace("Z", "+00:00")))
                    except (KeyError, ValueError):
                        continue
        snippet = channel.get("snippet") or {}
        custom = snippet.get("customUrl") or ""
        return PublicProfileMetrics(
            handle=custom.lstrip("@") or handle,
            display_name=snippet.get("title") or "",
            followers=followers,
            posts_count=_int_or_none(stats.get("videoCount")),
            avg_engagement=round(sum(engagement) / len(engagement), 2) if engagement else None,
            posts_per_week=self._posts_per_week(stamps),
            sampled_posts=len(engagement),
            profile_url=f"https://www.youtube.com/{custom if custom.startswith('@') else 'channel/' + channel['id']}",
        )

    def get_account_metrics(self, access_token: str, date_range: tuple[datetime, datetime]) -> AccountMetrics:
        """Channel-level metrics from the YouTube Analytics API.

        Fetches the metrics the YouTube Data API can't provide per-video:
        watch time, average view %, subscribers gained, and shares.
        Views/likes/comments come from per-post snapshots so the main page
        stays consistent with the per-post drawer; shares is the exception
        because ``videos.list?part=statistics`` exposes no shareCount field.

        Per-video equivalents of the channel-level watch-time / avg-view-% /
        shares triple come from :meth:`get_post_analytics`, which calls the
        same ``/reports`` endpoint with ``dimensions=video`` and the video
        IDs as a filter.

        Callers must pass a single-day ``date_range`` — Analytics returns one
        aggregated row across [startDate, endDate], and the sync layer writes
        it as a single day's snapshot. Larger ranges would silently collapse
        into one cell.

        Requires the ``yt-analytics.readonly`` scope. The YouTube Analytics
        API typically lags 1–2 days behind real-time.
        """
        start_date = date_range[0].date().isoformat()
        end_date = date_range[1].date().isoformat()
        resp = self._request(
            "GET",
            f"{ANALYTICS_BASE}/reports",
            access_token=access_token,
            params={
                "ids": "channel==MINE",
                "startDate": start_date,
                "endDate": end_date,
                "metrics": "estimatedMinutesWatched,averageViewPercentage,subscribersGained,shares",
            },
        )
        body = resp.json()
        headers = body.get("columnHeaders", [])
        rows = body.get("rows", [])
        if not rows or not rows[0]:
            return AccountMetrics()

        index = {col.get("name", ""): i for i, col in enumerate(headers)}
        row = rows[0]

        def _value_or_none(name: str) -> float | None:
            """Return float value if Analytics returned the column (even 0), else None.

            Distinguishes "metric not in the response" (None) from "metric
            returned 0" (0.0). Falsy-zero guards downstream incorrectly drop
            real 0-value days otherwise.
            """
            i = index.get(name)
            if i is None or i >= len(row) or row[i] is None:
                return None
            try:
                return float(row[i])
            except (TypeError, ValueError):
                return None

        extra: dict = {}
        for api_key, catalog_key in (
            ("estimatedMinutesWatched", "watch_time"),
            ("averageViewPercentage", "avg_view_pct"),
            ("subscribersGained", "subscribers"),
            ("shares", "shares"),
        ):
            v = _value_or_none(api_key)
            if v is not None:
                extra[catalog_key] = v
        return AccountMetrics(extra=extra)

    def get_post_analytics(
        self,
        access_token: str,
        post_ids: list[str],
        date_range: tuple[datetime, datetime],
        *,
        deadline: datetime | None = None,
    ) -> dict[str, PostMetrics]:
        """Per-video metrics from the YouTube Analytics API, batched.

        Returns ``{video_id: PostMetrics}`` populated with ``extra``:
        ``watch_time`` (estimatedMinutesWatched), ``avg_view_pct``
        (averageViewPercentage), and ``shares``. These are the per-video
        metrics the Data API v3 ``statistics`` part can't provide — the
        catalog keys match the ones :func:`apps.analytics.tasks._post_metrics_to_dict`
        reads from ``PostMetrics.extra`` via ``_GENERIC_POST_EXTRA_KEYS``.

        One ``/reports`` request covers every video in ``post_ids`` via
        ``dimensions=video`` and ``filters=video==<id>,<id>,...``. The
        endpoint caps the filter at 500 IDs per request, so longer inputs
        are split into multiple calls and merged.

        Videos that have no activity in the window are omitted from the
        Analytics response and will be absent from the returned dict —
        callers should treat that as "no data" rather than "all zeros".

        Requires the ``yt-analytics.readonly`` scope (same as
        :meth:`get_account_metrics`). The Analytics API typically lags
        1–2 days behind real-time.

        ``deadline`` stops between 500-video filter chunks and returns the
        partial result collected so far, allowing the hourly worker to resume
        the remaining videos on its next pass.
        """
        if not post_ids:
            return {}

        start_date = date_range[0].date().isoformat()
        end_date = date_range[1].date().isoformat()
        result: dict[str, PostMetrics] = {}

        for offset in range(0, len(post_ids), _ANALYTICS_VIDEO_FILTER_CHUNK):
            if deadline is not None and datetime.now(UTC) >= deadline:
                logger.warning("YouTube Analytics post metrics stopped at the task deadline")
                break
            chunk = post_ids[offset : offset + _ANALYTICS_VIDEO_FILTER_CHUNK]
            resp = self._request(
                "GET",
                f"{ANALYTICS_BASE}/reports",
                access_token=access_token,
                params={
                    "ids": "channel==MINE",
                    "startDate": start_date,
                    "endDate": end_date,
                    "metrics": "estimatedMinutesWatched,averageViewPercentage,shares",
                    "dimensions": "video",
                    "filters": f"video=={','.join(chunk)}",
                },
            )
            body = resp.json()
            headers = body.get("columnHeaders", []) or []
            rows = body.get("rows", []) or []
            index = {col.get("name", ""): i for i, col in enumerate(headers)}
            video_idx = index.get("video")
            if video_idx is None:
                continue

            # API column → indexes used below. Pre-computed once per chunk so
            # the inner row loop is a flat lookup. Same semantics as the
            # closure in ``get_account_metrics``: missing column → key
            # omitted; present column with a 0 → 0 is preserved (not dropped
            # as falsy).
            watch_idx = index.get("estimatedMinutesWatched")
            avp_idx = index.get("averageViewPercentage")
            shares_idx = index.get("shares")

            for row in rows:
                if video_idx >= len(row):
                    continue
                video_id = row[video_idx]
                if not video_id:
                    continue

                extra: dict = {}
                for catalog_key, i in (("watch_time", watch_idx), ("avg_view_pct", avp_idx)):
                    if i is None or i >= len(row) or row[i] is None:
                        continue
                    try:
                        extra[catalog_key] = float(row[i])
                    except (TypeError, ValueError):
                        continue

                # ``shares`` has a dedicated ``PostMetrics`` field; the catalog
                # mapper (``_post_metrics_to_dict``) reads it from there, not
                # from ``extra``. Cast to int to match the field type.
                shares_value: int | None = None
                if shares_idx is not None and shares_idx < len(row) and row[shares_idx] is not None:
                    try:
                        shares_value = int(float(row[shares_idx]))
                    except (TypeError, ValueError):
                        shares_value = None

                if extra or shares_value is not None:
                    if shares_value is not None:
                        extra["reported_metrics"] = {"shares": shares_value}
                    result[video_id] = PostMetrics(shares=shares_value or 0, extra=extra)
        return result

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    def revoke_token(self, access_token: str) -> bool:
        try:
            self._request(
                "POST",
                REVOKE_URL,
                data={"token": access_token},
                timeout=5,
            )
            return True
        except Exception:
            logger.warning("YouTube revocation deferred; retry via encrypted outbox")
            return False
