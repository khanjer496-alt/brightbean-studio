"""Shared data types for social platform providers."""

from __future__ import annotations

import enum
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from urllib.parse import urlparse

# Extension heuristic for spotting video URLs.
VIDEO_URL_SUFFIXES = (".mp4", ".mov")


def is_video_url(url: str) -> bool:
    """Heuristically detect a video URL by file extension.

    A last resort — prefer ``PublishContent.is_video``, which uses the media
    type the library sniffed from the file's magic bytes. Matches on the URL
    *path* only: media URLs are presigned (R2/S3 with ``AWS_QUERYSTRING_AUTH``),
    so a bare ``url.endswith(".mp4")`` sees the signature query string instead
    of the extension and never fires. Lowercased because exports off a phone or
    DSLR routinely arrive as ``.MP4``/``.MOV``.
    """
    return urlparse(url).path.lower().endswith(VIDEO_URL_SUFFIXES)


class PostType(enum.Enum):
    TEXT = "text"
    IMAGE = "image"
    VIDEO = "video"
    CAROUSEL = "carousel"
    STORY = "story"
    REEL = "reel"
    LINK = "link"
    ARTICLE = "article"
    POLL = "poll"
    PIN = "pin"
    SHORT = "short"


class MediaType(enum.Enum):
    JPEG = "jpeg"
    PNG = "png"
    GIF = "gif"
    MP4 = "mp4"
    MOV = "mov"
    WEBP = "webp"
    PDF = "pdf"


class AuthType(enum.Enum):
    OAUTH2 = "oauth2"
    SESSION = "session"
    INSTANCE_OAUTH = "instance_oauth"


@dataclass(frozen=True)
class OAuthTokens:
    access_token: str
    refresh_token: str | None = None
    expires_in: int | None = None
    token_type: str = "Bearer"
    scope: str | None = None
    raw_response: dict = field(default_factory=dict)


@dataclass(frozen=True)
class AccountProfile:
    platform_id: str
    name: str
    handle: str | None = None
    avatar_url: str | None = None
    follower_count: int = 0
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class PublishResult:
    platform_post_id: str
    url: str | None = None
    extra: dict = field(default_factory=dict)


class PublishState(enum.StrEnum):
    """Outcome of an asynchronous publish, as reported by the platform."""

    COMPLETE = "complete"
    PENDING = "pending"
    FAILED = "failed"
    # The platform accepted the upload but parked it as a draft the creator has
    # to finish by hand (TikTok's SEND_TO_USER_INBOX). Nothing we do will make
    # it go live, so it is terminal for us — but it is not a failure to hide.
    INBOX = "inbox"


@dataclass(frozen=True)
class PublishStatus:
    """Where an in-flight publish stands on the platform.

    Returned by :meth:`SocialProvider.check_publish_status` for providers whose
    publish API is asynchronous (``publish_is_async``). ``platform_post_id`` is
    the platform's real, final id — only set once ``state`` is COMPLETE.
    """

    state: PublishState
    platform_post_id: str = ""
    error: str = ""
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class CommentResult:
    platform_comment_id: str
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class PostMetrics:
    impressions: int = 0
    reach: int = 0
    engagements: int = 0
    likes: int = 0
    comments: int = 0
    shares: int = 0
    saves: int = 0
    clicks: int = 0
    video_views: int = 0
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class AccountMetrics:
    followers: int | None = 0
    followers_gained: int = 0
    impressions: int = 0
    reach: int = 0
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Demographics:
    age_ranges: dict = field(default_factory=dict)
    genders: dict = field(default_factory=dict)
    top_countries: dict = field(default_factory=dict)
    top_cities: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class InboxMessage:
    platform_message_id: str
    sender_id: str
    sender_name: str
    text: str
    timestamp: datetime
    message_type: str = "comment"
    is_read: bool = False
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ReplyResult:
    platform_message_id: str
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class RateLimitConfig:
    requests_per_hour: int = 200
    requests_per_day: int = 5000
    publish_per_day: int = 25
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class MediaRules:
    """What a platform accepts as the media of one post of a given ``PostType``.

    Declared per provider in ``SocialProvider.media_rules`` and checked by the
    composer and the API before a post is scheduled, against the metadata the
    media library stores for each attachment. Every limit is optional: ``None``
    (or ``0`` for a minimum) means the platform sets none worth checking.

    A broken limit blocks scheduling, because the platform would refuse the
    post. Name a limit in ``advisory`` to warn instead — for one the platform
    only recommends, or whose official value isn't firm enough to refuse a
    post over. The names are ``max_items``, ``media_types``, ``aspect_ratio``
    (both bounds), ``min_resolution``, ``max_resolution``, ``min_duration``,
    ``max_duration``, ``max_image_bytes`` and ``max_video_bytes``.
    ``recommended_aspect_ratio`` and ``published_items`` only ever warn.
    Ratios are width / height (9:16 is 0.5625).
    """

    # The platform's documentation the limits were taken from.
    source: str = ""
    # Attachments one post may carry.
    max_items: int | None = None
    # Attachments this provider actually sends; any beyond are dropped with a
    # warning. Set where the platform takes more than the provider implements.
    published_items: int | None = None
    # ``MediaAsset.media_type`` values the post type takes (image, video, gif).
    media_types: frozenset[str] | None = None
    min_aspect_ratio: float | None = None
    max_aspect_ratio: float | None = None
    recommended_aspect_ratio: float | None = None
    min_width: int = 0
    min_height: int = 0
    max_width: int | None = None
    max_height: int | None = None
    # Video length, in seconds.
    min_duration: float = 0
    max_duration: float | None = None
    max_image_bytes: int | None = None
    max_video_bytes: int | None = None
    advisory: frozenset[str] = frozenset()


class ThreadMode(enum.StrEnum):
    """How a platform carries the parts of a thread after the first post."""

    # Each part replies to the one before it, so the parts read as one thread
    # on the author's profile (Bluesky, Threads, Mastodon).
    REPLY_CHAIN = "reply_chain"
    # The platform has no self-reply chain, so every later part is a comment on
    # the first post (Facebook, Instagram, LinkedIn, YouTube).
    COMMENTS = "comments"
    # The platform can't carry a thread at all; a post with parts is refused
    # before it is scheduled.
    NONE = "none"


@dataclass(frozen=True)
class ThreadCapability:
    """What a platform accepts as a thread, declared per provider.

    Read off the provider class (``SocialProvider.thread``) by the composer,
    the API and the publisher, so checking a thread never builds a provider or
    touches credentials. Parts are published by the engine one at a time, as
    their own scheduled tasks, so a delay between parts works the same on every
    platform that takes a thread at all.
    """

    mode: ThreadMode = ThreadMode.NONE
    # Most parts one thread may have, the first post included. A guardrail of
    # ours rather than a platform limit: a thread this long is almost always a
    # mistake, and each part spends one publish against the platform's quota.
    max_parts: int = 1
    # Characters one later part may carry. None means the same limit as the
    # post itself (the account's ``char_limit``); a comment often has its own.
    part_max_length: int | None = None
    # Whether a later part can carry its own media. A comment is text only.
    media_per_part: bool = False

    @property
    def supports_threads(self) -> bool:
        return self.mode is not ThreadMode.NONE


class UploadCache(Protocol):
    """Somewhere a provider can remember an upload between publish attempts.

    Best-effort: ``get`` may return None for anything, and providers must check
    a remembered upload is still usable before relying on it.
    """

    def get(self, key: str) -> str | None: ...

    def set(self, key: str, value: str, timeout: int) -> None: ...


@dataclass
class PublishContent:
    """Content payload passed to publish_post()."""

    text: str = ""
    media_urls: list[str] = field(default_factory=list)
    media_files: list[str] = field(default_factory=list)
    post_type: PostType = PostType.TEXT
    link_url: str | None = None
    title: str | None = None
    description: str | None = None
    first_comment: str | None = None
    extra: dict = field(default_factory=dict)
    # Duration of the primary video in seconds, when known. Lets providers
    # enforce platform limits (e.g. TikTok's max_video_post_duration_sec).
    video_duration_sec: float | None = None
    # Per-item media type ("image" / "video" / "gif" / ...), parallel to
    # ``media_urls``. Filled by the engine from MediaAsset.media_type.
    media_types: list[str] = field(default_factory=list)
    # The same for every attempt at one PlatformPost. A provider whose platform
    # honours request idempotency sends it, so a retry after a lost response
    # cannot publish twice.
    idempotency_key: str | None = None
    # Lets a provider reuse an upload the platform already accepted on an
    # earlier attempt instead of sending (and re-processing) the file again.
    upload_cache: UploadCache | None = None
    # True once the worker has been told to stop. Long waits should give up so
    # the attempt is rescheduled inside the shutdown grace period instead of
    # being killed mid-publish.
    should_stop: Callable[[], bool] | None = None

    def is_video(self, index: int = 0) -> bool:
        """Whether media item ``index`` is a video.

        Prefers ``media_types``, which the engine fills from the media
        library's magic-byte sniff. The URL extension is only a fallback for
        callers that don't supply it: a storage key's extension is copied from
        the *client-declared* filename and is cosmetic (see
        ``media_library.storage.generate_storage_key``), so it can disagree
        with the real content in either direction — an image uploaded as
        ``cat.mp4`` would otherwise be published through the video endpoint.
        """
        if index < len(self.media_types) and self.media_types[index]:
            return self.media_types[index] == "video"
        return index < len(self.media_urls) and is_video_url(self.media_urls[index])
