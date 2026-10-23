"""Pydantic request/response shapes for the Agent API.

Kept small on purpose: agents will rely on the auto-generated OpenAPI
spec at ``/api/v1/docs``, so every field needs a sensible description.

We deliberately don't expose internal fields like ``workspace_id`` in
request bodies — workspace scope comes from the bearer token, never
from client-supplied JSON. This is the same confused-deputy defence
Postiz uses.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import TYPE_CHECKING, Any, Literal

from django.conf import settings
from ninja import Field, Schema
from pydantic import field_serializer

from providers import is_video_only, requires_media, thread_capability_for

if TYPE_CHECKING:
    from apps.analytics.derive import DerivedMetric
    from apps.composer.models import PlatformPost, Post


def _serialize_utc_z(value: dt.datetime | None) -> str | None:
    """Render a datetime as ISO 8601 with a trailing ``Z`` for UTC.

    Pydantic's default datetime serialization emits ``+00:00`` for UTC,
    while Django's JSON encoder emits ``Z``. Without explicit normalization
    REST and MCP responses can disagree for the same instant.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.UTC)
    return value.isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# /me
# ---------------------------------------------------------------------------


class AccountSummary(Schema):
    id: uuid.UUID
    platform: str
    account_name: str
    account_handle: str = ""
    connection_status: str
    char_limit: int = Field(
        2200,
        description="Maximum caption length the platform accepts. Reject locally before calling /posts.",
    )
    escaped_chars: str = Field(
        "",
        description=(
            "Characters the platform requires escaped, each costing two against ``char_limit``. "
            "LinkedIn escapes its reserved little-text set, so count a caption as "
            "``len(caption) + sum(caption.count(c) for c in escaped_chars)`` before rejecting it. "
            "Empty for platforms that publish the caption verbatim."
        ),
    )
    caption_counting: str = Field(
        "characters",
        description=(
            "How the platform counts a caption against ``char_limit``. ``characters``: one per "
            "character (plus ``escaped_chars``). ``weighted``: the twitter-text v3 weighted count "
            "X uses, where each URL counts as 23 and CJK characters and emoji as 2."
        ),
    )
    needs_title: bool = Field(
        False,
        description="True when the platform requires a title (YouTube, Pinterest). When false, the title field is ignored.",
    )
    supports_first_comment: bool = Field(
        True,
        description=(
            "True when the platform can auto-post a first comment after publish. "
            "False for TikTok, Pinterest, Bluesky, Google Business. "
            "LinkedIn Personal returns false when authorized via OIDC instead of the Community Management API. "
            "If false, ``first_comment`` is silently dropped at publish time."
        ),
    )
    requires_video: bool = Field(
        False,
        description=(
            "True when the platform publishes only video (TikTok, YouTube). Scheduling a post "
            "with no video in ``media_asset_ids`` is rejected with 422."
        ),
    )
    requires_media: bool = Field(
        False,
        description=(
            "True when the platform can't publish text alone (Instagram, Pinterest, and the "
            "``requires_video`` platforms). Scheduling a post with no ``media_asset_ids`` is "
            "rejected with 422."
        ),
    )
    thread_mode: Literal["reply_chain", "comments", "none"] = Field(
        "none",
        description=(
            "How the platform publishes a post's ``parts``: ``reply_chain`` replies each part to the one "
            "before it (Bluesky, Threads, Mastodon); ``comments`` posts later parts as comments on the "
            "first post (Facebook, Instagram, LinkedIn, YouTube); ``none`` can't take a thread, and a "
            "post with ``parts`` is rejected with 422 when scheduled."
        ),
    )
    thread_max_parts: int = Field(1, description="Most parts one thread may have, the first post included.")
    thread_part_max_length: int = Field(
        2200, description="Characters each later part may carry, counted like ``char_limit``."
    )
    thread_media_per_part: bool = Field(
        False, description="True when a later part can carry its own ``media_asset_ids``. Comments are text only."
    )
    post_settings: list[dict] = Field(
        default_factory=list,
        description=(
            "Per-post settings the platform needs beyond the caption (Reddit: ``subreddit``, ``flair_id``, ...). "
            "Each has a ``key``, ``label``, ``kind`` (text, select, checkbox), ``required`` and ``help``. Send them "
            "as ``post_settings`` when creating the post; a scheduled post missing a required one is rejected with 422."
        ),
    )

    @classmethod
    def from_social_account(cls, sa) -> AccountSummary:
        thread = thread_capability_for(sa.platform)
        return cls(
            id=sa.id,
            platform=sa.platform,
            account_name=sa.account_name,
            account_handle=getattr(sa, "account_handle", "") or "",
            connection_status=sa.connection_status,
            char_limit=sa.char_limit,
            escaped_chars=sa.escaped_chars,
            caption_counting=sa.caption_counting,
            needs_title=bool(sa.field_config.get("needs_title", False)),
            supports_first_comment=sa.supports_first_comment(),
            requires_video=is_video_only(sa.platform),
            requires_media=requires_media(sa.platform),
            thread_mode=thread.mode.value,
            thread_max_parts=thread.max_parts,
            thread_part_max_length=thread.part_max_length or sa.char_limit,
            thread_media_per_part=thread.media_per_part,
            post_settings=sa.post_settings,
        )


class StorageSummary(Schema):
    used_bytes: int
    limit_bytes: int
    remaining_bytes: int
    plan_slug: str = Field("", description="IntelligenceSubscription plan slug; empty if no active subscription.")


class MeResponse(Schema):
    """Echoes everything the key is scoped to so an agent can self-introspect."""

    api_key_id: uuid.UUID
    workspace_id: uuid.UUID
    workspace_name: str
    organization_id: uuid.UUID
    permissions: list[str]
    storage: StorageSummary
    allowlisted_accounts: list[AccountSummary]


# ---------------------------------------------------------------------------
# /accounts
# ---------------------------------------------------------------------------


class AccountsListResponse(Schema):
    accounts: list[AccountSummary]


# ---------------------------------------------------------------------------
# /posts — write
# ---------------------------------------------------------------------------


PostAction = Literal["draft", "schedule"]


class PlatformOverride(Schema):
    """Per-account override of the post's title/caption/first_comment.

    Mirrors the three form fields the cookie-authenticated UI accepts
    (see [apps/composer/views.py:85-96]). Maps to the model's
    ``platform_specific_*`` columns on ``PlatformPost``.

    Each field is independent: omit a field (or send ``null``) to keep
    the post's default for that platform. Send a string (including ``""``
    to explicitly blank out the value just for this platform) to apply
    the override.
    """

    social_account_id: uuid.UUID = Field(
        ...,
        description="Must reference an account that is a child of this post.",
    )
    title: str | None = Field(None, max_length=255)
    caption: str | None = Field(None, max_length=10_000)
    first_comment: str | None = Field(None, max_length=10_000)


class ThreadPartOverride(Schema):
    """Per-account caption for one thread part — the part-level ``PlatformOverride.caption``."""

    social_account_id: uuid.UUID = Field(..., description="Must reference an account that is a child of this post.")
    caption: str | None = Field(None, max_length=10_000, description="``null`` keeps the part's own caption.")


class ThreadPart(Schema):
    """One later part of a thread. The post's ``caption`` and ``media_asset_ids`` are its first part."""

    caption: str = Field("", max_length=10_000)
    media_asset_ids: list[uuid.UUID] = Field(
        default_factory=list,
        description="Media for this part. Only on accounts with ``thread_media_per_part=true``.",
    )
    delay_seconds: int = Field(
        0, ge=0, le=86_400, description="Seconds to wait after the previous part is live before publishing this one."
    )
    is_finisher: bool = Field(
        False, description="A closing part (e.g. 'Follow for more'), always published last. At most one."
    )
    platform_overrides: list[ThreadPartOverride] = Field(default_factory=list)

    def to_spec(self):
        """The composer service's ``ThreadPartSpec`` for this part."""
        from apps.composer.services import ThreadPartSpec

        return ThreadPartSpec(
            caption=self.caption,
            media_asset_ids=list(self.media_asset_ids),
            delay_seconds=self.delay_seconds,
            is_finisher=self.is_finisher,
            caption_overrides={str(ov.social_account_id): ov.caption for ov in self.platform_overrides},
        )


_PARTS_DESCRIPTION = (
    "Optional later parts of a thread, in order; the post itself is the first part. Each account publishes "
    "them according to its ``thread_mode`` (see /accounts). Scheduling is rejected with 422 when an account "
    "can't publish the thread: ``thread_mode='none'``, more than ``thread_max_parts``, a part over "
    "``thread_part_max_length``, or media on a part where ``thread_media_per_part`` is false."
)


RecurrenceFrequency = Literal["never", "daily", "weekly", "monthly"]


class RecurrenceSpec(Schema):
    """Repeat a scheduled post — the same choices as the composer's Repeat control.

    The post is the series' first post; copies are generated up to 90 days
    ahead at the same local time in the workspace timezone, and carry the
    post's content, media and per-platform settings.
    """

    frequency: RecurrenceFrequency = Field(
        ...,
        description="``never`` stops an existing series and removes its upcoming unpublished copies.",
    )
    interval: int = Field(1, ge=1, le=99, description="Repeat every N days / weeks / months.")
    weekdays: list[int] = Field(
        default_factory=list,
        description=(
            "Weekly only: days to post on, 0=Monday … 6=Sunday, in the workspace timezone. "
            "Defaults to the first post's weekday."
        ),
    )
    end_date: dt.date | None = Field(
        None,
        description="Last date (workspace timezone) a copy may fall on. Can't be combined with ``count``.",
    )
    count: int | None = Field(
        None,
        ge=2,
        description="Total posts in the series, the first included. Can't be combined with ``end_date``.",
    )

    def rule_fields(self) -> dict:
        """Keyword arguments for ``apps.calendar.services.save_recurrence``."""
        return {
            "frequency": self.frequency,
            "interval": self.interval,
            "weekdays": self.weekdays,
            "end_date": self.end_date,
            "count": self.count,
        }


class RecurrenceResponse(Schema):
    frequency: str
    interval: int
    weekdays: list[int]
    end_date: dt.date | None
    count: int | None
    summary: str = Field(..., description='The rule in words, e.g. "Every Tuesday and Thursday until 31 Dec".')


class CreatePostRequest(Schema):
    """Create a draft or directly schedule a post against one account.

    ``social_account_id`` MUST be in the key's allowlist; the auth class
    raises 403 otherwise.
    """

    social_account_id: uuid.UUID = Field(
        ...,
        description="ID of the SocialAccount to target. Must be in the key's allowlist.",
    )
    caption: str = Field(..., max_length=10_000)
    title: str = Field(
        "",
        max_length=255,
        description="Required on platforms where ``needs_title=true`` (YouTube, Pinterest). Ignored otherwise.",
    )
    first_comment: str = Field(
        "",
        max_length=10_000,
        description=(
            "Auto-posted as a reply ~120s after the main post lands. "
            "Silently dropped at publish time when the target account's "
            "``supports_first_comment`` is false (TikTok, Pinterest, "
            "Bluesky, Google Business; LinkedIn Personal in OIDC mode)."
        ),
    )
    internal_notes: str = Field(
        "",
        max_length=10_000,
        description=(
            "Private team-only note. Never published to any platform; surfaced only "
            "to your team via the API and the composer."
        ),
    )
    media_asset_ids: list[uuid.UUID] = Field(
        default_factory=list,
        description="MediaAsset IDs already uploaded to the workspace's media library. Position-ordered.",
    )
    platform_overrides: list[PlatformOverride] = Field(
        default_factory=list,
        description=(
            "Optional per-platform overrides of title / caption / first_comment. "
            "Each entry's social_account_id must equal the post's social_account_id "
            "in the current single-account API. Sending a field as ``null`` (or "
            "omitting it) leaves the post's default in place for that platform."
        ),
    )
    parts: list[ThreadPart] = Field(default_factory=list, description=_PARTS_DESCRIPTION)
    post_settings: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Per-post settings the account's platform declares in ``/accounts`` ``post_settings`` "
            "(Reddit: ``subreddit``, ``flair_id``, ``nsfw``, ...). Unknown keys are rejected with 422."
        ),
    )
    action: PostAction = Field(
        "draft",
        description=(
            "``draft`` parks the post for later editing/scheduling; "
            "``schedule`` requires ``scheduled_at`` and queues for publishing."
        ),
    )
    scheduled_at: dt.datetime | None = Field(
        None,
        description="UTC timestamp. Required when ``action='schedule'``.",
    )
    proposed_publish_at: dt.datetime | None = Field(
        None,
        description=(
            "Optional draft-stage suggested publish time (UTC), independent of "
            "``scheduled_at``. Stored as-is — not validated against the future and "
            "never queued for publishing; cleared automatically if the post is scheduled."
        ),
    )
    recurrence: RecurrenceSpec | None = Field(
        None,
        description="Repeat the post. Only with ``action='schedule'``.",
    )
    idempotency_key: str | None = Field(
        None,
        max_length=128,
        description="Optional client-chosen retry key. Same key + same body → replay first response.",
    )


class UpdatePostRequest(Schema):
    caption: str | None = Field(None, max_length=10_000)
    title: str | None = Field(None, max_length=255)
    first_comment: str | None = Field(None, max_length=10_000)
    internal_notes: str | None = Field(
        None,
        max_length=10_000,
        description="Private team-only note. Omit (or send null) to leave it unchanged.",
    )
    media_asset_ids: list[uuid.UUID] | None = None
    parts: list[ThreadPart] | None = Field(
        None,
        description="Replace the thread's later parts (``[]`` makes it a single post). Omit to leave them unchanged. "
        + _PARTS_DESCRIPTION,
    )
    scheduled_at: dt.datetime | None = Field(
        None,
        description="If the post is currently scheduled, this re-times it. Ignored for drafts.",
    )
    proposed_publish_at: dt.datetime | None = Field(
        None,
        description="Set a draft's suggested publish time (UTC). Sending null is a no-op (cannot clear via PATCH).",
    )
    recurrence: RecurrenceSpec | None = Field(
        None,
        description=(
            "Start or change the repeat of a scheduled post, or stop it with ``frequency='never'``. "
            "Changing it replaces the upcoming unpublished copies. Omit (or send null) to leave it unchanged."
        ),
    )
    post_settings: dict[str, Any] | None = Field(
        None,
        description=(
            "Replace the per-post settings (as on create). Each account takes the keys its platform declares; "
            "a declared key left out is cleared. Omit (or send null) to leave them unchanged."
        ),
    )


class ScheduleRequest(Schema):
    scheduled_at: dt.datetime = Field(..., description="UTC timestamp at which the publisher should fire the post.")
    recurrence: RecurrenceSpec | None = Field(None, description="Repeat the post once it is scheduled.")


# ---------------------------------------------------------------------------
# /posts — read
# ---------------------------------------------------------------------------


class PlatformPostPartSummary(Schema):
    """One thread part's outcome on one account."""

    part_id: uuid.UUID
    status: str = Field(..., description="pending, posting, published, failed, or unconfirmed.")
    platform_part_id: str = ""
    url: str = ""
    publish_error: str = ""


def _saved_post_settings(pp: PlatformPost) -> dict[str, Any]:
    from providers import post_settings_for

    declared = {field.key for field in post_settings_for(pp.social_account.platform)}
    return {key: value for key, value in (pp.platform_extra or {}).items() if key in declared}


class PlatformPostSummary(Schema):
    id: uuid.UUID
    social_account_id: uuid.UUID
    platform: str
    status: str
    scheduled_at: dt.datetime | None
    published_at: dt.datetime | None
    platform_post_id: str = ""
    publish_error: str = ""
    thread_status: str = Field(
        "none",
        description=(
            "none, pending, complete, or partial. ``partial`` means the first post and some parts are live "
            "and the thread stopped; POST /posts/{id}/resume-thread picks it up from the stopped part."
        ),
    )
    thread_error: str = ""
    thread_parts: list[PlatformPostPartSummary] = Field(default_factory=list)
    post_settings: dict[str, Any] = Field(
        default_factory=dict, description="The per-post settings this account's platform declares, as saved."
    )

    @field_serializer("scheduled_at", "published_at")
    def _serialize_dt(self, value: dt.datetime | None) -> str | None:
        return _serialize_utc_z(value)

    @classmethod
    def from_platform_post(cls, pp: PlatformPost) -> PlatformPostSummary:
        # Reuse a prefetch when the caller made one, as ``PostResponse.from_post``
        # does for ``platform_posts``; otherwise join the part in one query.
        if "part_states" in getattr(pp, "_prefetched_objects_cache", {}):
            part_states = list(pp.part_states.all())
        else:
            part_states = list(pp.part_states.select_related("part"))
        return cls(
            id=pp.id,
            social_account_id=pp.social_account_id,
            platform=pp.social_account.platform,
            status=pp.status,
            scheduled_at=pp.scheduled_at,
            published_at=pp.published_at,
            platform_post_id=pp.platform_post_id or "",
            publish_error=pp.publish_error or "",
            thread_status=pp.thread_status,
            thread_error=pp.thread_error or "",
            post_settings=_saved_post_settings(pp),
            thread_parts=[
                PlatformPostPartSummary(
                    part_id=state.part_id,
                    status=state.status,
                    platform_part_id=state.platform_part_id,
                    url=state.url,
                    publish_error=state.publish_error,
                )
                for state in sorted(part_states, key=lambda state: state.part.position)
            ],
        )


class ThreadPartResponse(Schema):
    id: uuid.UUID
    position: int = Field(..., description="2 for the part right after the post, and so on.")
    caption: str
    media_asset_ids: list[uuid.UUID]
    delay_seconds: int
    is_finisher: bool


class PostResponse(Schema):
    id: uuid.UUID
    workspace_id: uuid.UUID
    title: str
    caption: str
    first_comment: str
    internal_notes: str
    scheduled_at: dt.datetime | None
    published_at: dt.datetime | None
    proposed_publish_at: dt.datetime | None
    status: str  # derived aggregate
    platform_posts: list[PlatformPostSummary]
    parts: list[ThreadPartResponse] = Field(
        default_factory=list, description="The thread's later parts, in order. Empty for a single post."
    )
    recurrence: RecurrenceResponse | None = Field(
        None, description="The repeat this post starts, if it is the first post of an active series."
    )
    recurrence_source_id: uuid.UUID | None = Field(
        None, description="For a copy generated by a series: the series' first post, where its repeat is changed."
    )
    created_at: dt.datetime
    updated_at: dt.datetime

    @field_serializer("scheduled_at", "published_at", "proposed_publish_at", "created_at", "updated_at")
    def _serialize_dt(self, value: dt.datetime | None) -> str | None:
        return _serialize_utc_z(value)

    @classmethod
    def from_post(cls, post: Post, *, include_internal_notes: bool = False) -> PostResponse:
        """Single source of truth for Post → API response shape.

        Used by both the REST router and MCP handlers so the two surfaces
        cannot drift. ``post.platform_posts`` should be prefetched with
        ``social_account`` to avoid an N+1.

        ``internal_notes`` is a team-only field. The composer hides it from
        ``client`` / ``viewer`` workspace roles, so the API/MCP mirror that by
        redacting it to ``""`` unless ``include_internal_notes`` is set. Call
        sites derive that flag from the caller's ``create_posts`` permission
        (see ``_can_view_internal_notes`` in the REST router and MCP handlers).
        It defaults to ``False`` so a new, unconverted call site fails closed —
        redacted — rather than leaking notes.
        """
        # ``.select_related()`` builds a *new* queryset, which discards any
        # prefetch cache and re-queries once per post — an N+1 on list views.
        # Use the cache when the caller prefetched, and fall back otherwise so
        # an un-prefetched single fetch still avoids a query per child.
        if "platform_posts" in getattr(post, "_prefetched_objects_cache", {}):
            children = post.platform_posts.all()
        else:
            children = post.platform_posts.select_related("social_account")
        platform_posts = [PlatformPostSummary.from_platform_post(pp) for pp in children]
        # Only the series' first post carries its rule; ``series_rule`` would
        # also answer for a copy, which can't change the series.
        rule = post.series_rule if post.recurrence_source_id is None else None
        recurrence = None
        if rule is not None and rule.is_active:
            recurrence = RecurrenceResponse(
                frequency=rule.frequency,
                interval=rule.interval,
                weekdays=list(rule.weekdays or []),
                end_date=rule.end_date,
                count=rule.count,
                summary=rule.summary,
            )
        return cls(
            id=post.id,
            workspace_id=post.workspace_id,
            title=post.title,
            caption=post.caption,
            first_comment=post.first_comment,
            internal_notes=post.internal_notes if include_internal_notes else "",
            scheduled_at=post.scheduled_at,
            published_at=post.published_at,
            proposed_publish_at=post.proposed_publish_at,
            status=post.status,
            platform_posts=platform_posts,
            parts=[
                ThreadPartResponse(
                    id=part.id,
                    position=part.position + 1,
                    caption=part.caption,
                    media_asset_ids=[pm.media_asset_id for pm in part.media_attachments.all()],
                    delay_seconds=part.delay_seconds,
                    is_finisher=part.is_finisher,
                )
                for part in post.thread_parts.all()
            ],
            recurrence=recurrence,
            recurrence_source_id=post.recurrence_source_id,
            created_at=post.created_at,
            updated_at=post.updated_at,
        )


# ---------------------------------------------------------------------------
# /media — write + read
# ---------------------------------------------------------------------------


class MediaAssetResponse(Schema):
    """Single MediaAsset as exposed to the API.

    ``url`` is whatever ``MediaAsset.file.url`` resolves to — a presigned
    URL with a configurable TTL on S3/R2, or a local ``/media/...`` path
    on local storage. Agents should treat it as short-lived and always
    reference the asset by ``id`` in subsequent calls.

    ``last_used_at`` is the max ``created_at`` across every Post that
    references this asset via ``PostMedia``. It is populated only on
    list/detail responses, never on the upload 201.
    """

    id: uuid.UUID
    # ``organization`` is a nullable FK on ``MediaAsset`` (see
    # [apps/media_library/models.py:93-99]). Pre-migration legacy rows can
    # exist with org=NULL, and Pydantic rejects them at response time if
    # this isn't optional. Staging hit this on 2026-06-01 with the agent
    # API media list endpoint.
    organization_id: uuid.UUID | None
    workspace_id: uuid.UUID | None
    filename: str
    media_type: str
    mime_type: str
    file_size: int
    file_size_display: str
    width: int
    height: int
    aspect_ratio: float
    duration: float
    title: str
    alt_text: str
    tags: list[str]
    folder_id: uuid.UUID | None
    is_starred: bool
    is_shared: bool
    processing_status: str
    url: str
    thumbnail_url: str | None
    last_used_at: dt.datetime | None = None
    created_at: dt.datetime
    updated_at: dt.datetime

    @field_serializer("last_used_at", "created_at", "updated_at")
    def _serialize_dt(self, value: dt.datetime | None) -> str | None:
        return _serialize_utc_z(value)

    @classmethod
    def from_asset(cls, asset, *, last_used_at: dt.datetime | None = None) -> MediaAssetResponse:
        return cls(
            id=asset.id,
            organization_id=asset.organization_id,
            workspace_id=asset.workspace_id,
            filename=asset.filename,
            media_type=asset.media_type,
            mime_type=asset.mime_type or "",
            file_size=int(asset.file_size or 0),
            file_size_display=asset.file_size_display,
            width=int(asset.width or 0),
            height=int(asset.height or 0),
            aspect_ratio=float(asset.aspect_ratio or 0),
            duration=float(asset.duration or 0),
            title=asset.title or "",
            alt_text=asset.alt_text or "",
            tags=list(asset.tags or []),
            folder_id=asset.folder_id,
            is_starred=bool(asset.is_starred),
            is_shared=bool(asset.is_shared),
            processing_status=asset.processing_status,
            url=_safe_file_url(asset.file),
            thumbnail_url=_safe_file_url(asset.thumbnail) or None,
            last_used_at=last_used_at,
            created_at=asset.created_at,
            updated_at=asset.updated_at,
        )


def _safe_file_url(file_field) -> str:
    """Return ``file_field.url`` if the field is set, else ``""``.

    ``FileField.url`` raises ``ValueError`` when the field is blank, which
    happens for ``thumbnail`` on every asset before async processing
    finishes.
    """
    if not file_field:
        return ""
    try:
        return file_field.url or ""
    except (ValueError, AttributeError):
        return ""


class MediaAssetListResponse(Schema):
    items: list[MediaAssetResponse]
    next_cursor: str | None = None
    limit: int


# ---------------------------------------------------------------------------
# /analytics — read
# ---------------------------------------------------------------------------


class DerivedMetricResponse(Schema):
    """A single derived metric over a rolling window.

    Mirrors ``apps.analytics.derive.DerivedMetric`` but with a stable wire
    shape: agents can rely on every metric carrying the same fields.
    The dataclass itself isn't a Pydantic model, so we route through
    :meth:`from_derived` rather than letting Ninja convert it implicitly.
    """

    key: str = Field(..., description="Metric identifier (e.g. ``views``, ``likes``, ``engagement``).")
    label: str = Field(..., description="Human-readable label for the metric (e.g. ``Views``).")
    kind: str = Field(..., description="Format hint: ``count`` | ``percent`` | ``minutes``.")
    value: float = Field(
        ...,
        description=(
            "Aggregate value over the current window. Sum for ``count`` metrics, "
            "YouTube uses the native whole-period report for every metric, including watch-time totals "
            "and averageViewPercentage. Other platforms may average daily percent/minute values."
        ),
    )
    delta: float = Field(
        ...,
        description=(
            f"Percent change vs. the previous equal-length window, calculated by {settings.BRAND_NAME}. "
            "``0.0`` when there is no prior baseline."
        ),
    )
    series: list[float | None] = Field(
        default_factory=list,
        description=(
            "Daily values for the current window, oldest first. ``null`` for a day the platform "
            "hasn't reported yet (for example YouTube Analytics' 2-3 day lag), which is not a zero."
        ),
    )
    calculated_by_brightbean: bool = Field(
        False,
        description=(
            f"``true`` when {settings.BRAND_NAME} calculated ``value`` rather than the platform reporting it: the "
            "engagement rate, daily averages, and estimates on supported platforms. "
            "Disclosure does not permit prohibited derived metrics. YouTube values are native reports."
        ),
    )
    averaged: bool = Field(
        False, description=f"``value`` is {settings.BRAND_NAME}'s average of the platform's daily figures."
    )
    estimated: bool = Field(
        False,
        description=(
            f"``value`` was estimated by {settings.BRAND_NAME} from other counts — summed per-post deltas when the "
            "platform's account-level analytics weren't available or were older, or differences between "
            "follower totals."
        ),
    )
    comparison_available: bool = Field(True, description="Whether delta has a complete, nonzero comparison baseline.")

    @classmethod
    def from_derived(
        cls, key: str, label: str, derived: DerivedMetric, *, calculated: bool = False
    ) -> DerivedMetricResponse:
        """``calculated`` marks a value BrightBean always computes (the
        engagement rate), on top of what ``derived`` records about itself."""
        return cls(
            key=key,
            label=label,
            kind=derived.kind,
            value=derived.value,
            delta=derived.delta,
            series=list(derived.series),
            calculated_by_brightbean=calculated or derived.averaged or derived.estimated,
            averaged=derived.averaged,
            estimated=derived.estimated,
            comparison_available=derived.comparison_available,
        )


class EngagementCardResponse(Schema):
    rate: DerivedMetricResponse = Field(
        ...,
        description=(
            f"Engagement rate = sum(parts) / denom * 100, calculated by {settings.BRAND_NAME}. The denom is the "
            "first of ``views, reach, impressions, plays`` with data, falling back to the follower count."
        ),
    )
    parts: list[DerivedMetricResponse] = Field(
        default_factory=list,
        description="The component metrics that feed the rate's numerator (likes, comments, shares, …).",
    )
    formula: str | None = Field(
        None,
        description=(
            "How the rate was calculated, e.g. ``(Likes + Comments + Shares) ÷ Views``. "
            "``null`` when there was nothing to divide by yet."
        ),
    )


class ReportingPeriodResponse(Schema):
    start: dt.date
    end: dt.date
    timezone: str
    latest_reported_day: dt.date | None = None


class AccountAnalyticsResponse(Schema):
    """Channel-level analytics summary over a rolling window of days."""

    account_id: uuid.UUID
    platform: str
    account_name: str
    connection_status: str = Field(
        ...,
        description=(
            "``connected``, ``token_expiring``, ``disconnected``, or ``error``. "
            "Disconnected accounts still return historical analytics, but new data may not be flowing."
        ),
    )
    days: int = Field(..., description="Window size (in days) used for derivation. One of 7, 30, or 90.")
    reporting_period: ReportingPeriodResponse | None = None
    analytics_available: bool = Field(
        ...,
        description=(
            "``false`` for platforms whose APIs don't expose aggregate analytics "
            "(currently LinkedIn Personal, Bluesky, Mastodon). Metric arrays are empty when false."
        ),
    )
    unavailable_reason: str | None = Field(
        None,
        description="Human-readable reason the analytics surface is unavailable, when ``analytics_available`` is false.",
    )
    hero_metrics: list[DerivedMetricResponse] = Field(
        default_factory=list,
        description="Top-line per-channel KPIs. YouTube includes separate likes, comments and shares here.",
    )
    engagement: EngagementCardResponse | None = Field(
        None,
        description="Engagement-rate card, or ``null`` when the platform lacks a reach-like denominator.",
    )
    follower_growth: DerivedMetricResponse | None = Field(
        None,
        description="New followers / subscribers gained over the window, when the platform reports it.",
    )
    captured_at: dt.datetime | None = Field(
        None,
        description="Most recent ``captured_at`` across the account's snapshots. ``null`` if no rows yet.",
    )
    next_sync_eta: dt.datetime | None = Field(
        None,
        description=(
            "Approximate next time the background sync will refresh this account. "
            "``null`` for platforms without analytics. Use it to pick a polling delay."
        ),
    )

    @field_serializer("captured_at", "next_sync_eta")
    def _serialize_dt(self, value: dt.datetime | None) -> str | None:
        return _serialize_utc_z(value)


class PostMetricTileResponse(Schema):
    """One metric tile in the per-post analytics drawer."""

    key: str
    label: str
    kind: str
    value: float = Field(..., description="Most recent snapshot value for this metric on this post.")
    series: list[float] = Field(
        default_factory=list,
        description="Daily sparkline since publish — one entry per day with a recorded snapshot.",
    )
    is_primary: bool = Field(
        False,
        description="True for the platform's default headline metric (e.g. ``views`` for YouTube).",
    )


class PlatformPostAnalyticsResponse(Schema):
    """Analytics for a single per-platform child of a Post."""

    platform_post_id: uuid.UUID
    social_account_id: uuid.UUID
    platform: str
    status: str
    published_at: dt.datetime | None
    analytics_available: bool = Field(
        ...,
        description=(
            "``false`` for platforms without analytics. ``true`` for everything else — including "
            "drafts and scheduled posts, which simply return empty metric tiles until publish."
        ),
    )
    unavailable_reason: str | None = None
    metric_tiles: list[PostMetricTileResponse] = Field(
        default_factory=list,
        description="One tile per metric this platform reports. Empty until the first snapshot lands.",
    )
    captured_at: dt.datetime | None = Field(
        None,
        description="Most recent ``captured_at`` across this platform-post's snapshots.",
    )
    next_sync_eta: dt.datetime | None = Field(
        None,
        description=(
            "Approximate next sync time based on the post's age: <24h→+1h, 1-7d→+6h, 7-30d→+1d, "
            "30-90d→+7d, >90d→null (syncs have stopped)."
        ),
    )

    @field_serializer("published_at", "captured_at", "next_sync_eta")
    def _serialize_dt(self, value: dt.datetime | None) -> str | None:
        return _serialize_utc_z(value)


class PostAnalyticsResponse(Schema):
    """Analytics for a Post, with per-platform breakdown.

    Mirrors ``PostResponse``'s parent-Post-with-platform-children shape so
    agents that already track ``post_id`` from ``schedule_post`` /
    ``create_draft`` can poll analytics without learning new IDs.
    """

    post_id: uuid.UUID
    workspace_id: uuid.UUID
    title: str
    caption: str
    platform_posts: list[PlatformPostAnalyticsResponse]


class OverviewMetricResponse(Schema):
    """One headline figure summed across the workspace's accounts."""

    key: str = Field(
        ...,
        description=(
            "``followers`` | ``new_followers`` | ``exposure`` (views or impressions) | ``engagements`` | "
            "``engagement_rate`` | ``posts``."
        ),
    )
    label: str
    kind: str = Field(..., description="Format hint: ``count`` | ``percent``.")
    value: float | None = Field(
        None, description="``null`` when no account reports this metric. Never ``0`` for missing data."
    )
    previous: float | None = Field(None, description="The same figure for the previous equal-length period.")
    delta: float | None = Field(
        None,
        description=(
            f"Percent change vs. the previous period, calculated by {settings.BRAND_NAME} over the accounts "
            "that reported in both periods. ``null`` when there is nothing to compare."
        ),
    )
    accounts_reporting: int
    accounts_total: int


class OverviewAccountResponse(Schema):
    account_id: uuid.UUID
    platform: str
    account_name: str
    analytics_available: bool
    unavailable_reason: str | None = None
    metrics: dict[str, float | None] = Field(
        ..., description="Same keys as the headline metrics; ``null`` means not available for this account."
    )


class OverviewNetworkResponse(Schema):
    platform: str
    name: str
    accounts: int
    metrics: dict[str, float | None]


class OverviewDayResponse(Schema):
    date: dt.date
    exposure: float | None = None
    engagements: float | None = None
    new_followers: float | None = None


class OverviewPostResponse(Schema):
    post_id: uuid.UUID
    platform_post_id: uuid.UUID
    social_account_id: uuid.UUID
    platform: str
    caption: str
    published_at: dt.datetime
    format: str = Field(..., description="``Text`` | ``Image`` | ``Video`` | ``Carousel``.")
    exposure: float | None = None
    engagements: float | None = None
    engagement_rate: float | None = None

    @field_serializer("published_at")
    def _serialize_dt(self, value: dt.datetime) -> str | None:
        return _serialize_utc_z(value)


class WorkspaceOverviewResponse(Schema):
    """Workspace-wide analytics over a date range, with the previous period for comparison."""

    workspace_id: uuid.UUID
    start: dt.date
    end: dt.date
    timezone: str = Field(..., description="Workspace timezone the range's days are counted in.")
    compare: bool
    previous_start: dt.date | None = None
    previous_end: dt.date | None = None
    metrics: list[OverviewMetricResponse]
    networks: list[OverviewNetworkResponse]
    accounts: list[OverviewAccountResponse]
    daily: list[OverviewDayResponse]
    top_posts: list[OverviewPostResponse]


# ---------------------------------------------------------------------------
# /inbox — read + reply drafting
# ---------------------------------------------------------------------------


class InboxReplyResponse(Schema):
    """One outbound reply to an inbox message.

    A reply is created as ``draft``, then a send step delivers it to the
    platform and moves it to ``sent`` (or ``failed`` with a human-readable
    ``send_error`` if the platform refused it). ``sent_at`` /
    ``platform_reply_id`` are populated only once ``status == "sent"``.
    """

    id: uuid.UUID
    inbox_message_id: uuid.UUID
    status: str
    body: str
    author_email: str = ""
    platform_reply_id: str = ""
    send_error: str = ""
    created_at: dt.datetime
    updated_at: dt.datetime
    sent_at: dt.datetime | None = None

    @field_serializer("created_at", "updated_at", "sent_at")
    def _serialize_dt(self, value: dt.datetime | None) -> str | None:
        return _serialize_utc_z(value)

    @classmethod
    def from_reply(cls, reply) -> InboxReplyResponse:
        author = getattr(reply, "author", None)
        return cls(
            id=reply.id,
            inbox_message_id=reply.inbox_message_id,
            status=reply.status,
            body=reply.body,
            author_email=(getattr(author, "email", "") or ""),
            platform_reply_id=reply.platform_reply_id or "",
            send_error=reply.send_error or "",
            created_at=reply.created_at,
            updated_at=reply.updated_at,
            sent_at=reply.sent_at,
        )


class InboxAttachmentResponse(Schema):
    type: str
    url: str = ""


class InboxReplyEligibilityResponse(Schema):
    can_send: bool
    reason: str = ""
    window: str
    standard_reply_until: dt.datetime | None = None
    human_reply_until: dt.datetime | None = None
    human_agent: bool = False

    @field_serializer("standard_reply_until", "human_reply_until")
    def _serialize_deadline(self, value):
        return _serialize_utc_z(value)


class InboxMessageResponse(Schema):
    """An inbound comment / mention / DM / review in the unified inbox."""

    id: uuid.UUID
    workspace_id: uuid.UUID
    social_account_id: uuid.UUID
    platform: str
    message_type: str
    status: str
    sentiment: str
    sender_name: str
    sender_handle: str = ""
    body: str
    related_post_id: uuid.UUID | None = None
    received_at: dt.datetime
    created_at: dt.datetime
    replies: list[InboxReplyResponse] = Field(default_factory=list)
    attachments: list[InboxAttachmentResponse] = Field(default_factory=list)
    reply_eligibility: InboxReplyEligibilityResponse = Field(
        description="Platform and reply-window eligibility for REST/MCP. Sending also requires reply_from_inbox permission."
    )

    @field_serializer("received_at", "created_at")
    def _serialize_dt(self, value: dt.datetime | None) -> str | None:
        return _serialize_utc_z(value)

    @classmethod
    def from_message(cls, message, *, include_replies: bool = False) -> InboxMessageResponse:
        from apps.inbox.reply_policy import reply_eligibility

        replies: list[InboxReplyResponse] = []
        if include_replies:
            if "replies" in getattr(message, "_prefetched_objects_cache", {}):
                rows = message.replies.all()
            else:
                rows = message.replies.select_related("author")
            replies = [InboxReplyResponse.from_reply(r) for r in rows]
        return cls(
            id=message.id,
            workspace_id=message.workspace_id,
            social_account_id=message.social_account_id,
            platform=message.social_account.platform,
            message_type=message.message_type,
            status=message.status,
            sentiment=message.sentiment,
            sender_name=message.sender_name,
            sender_handle=message.sender_handle or "",
            body=message.body or "",
            related_post_id=message.related_post_id,
            received_at=message.received_at,
            created_at=message.created_at,
            replies=replies,
            attachments=message.attachments,
            reply_eligibility=reply_eligibility(message),
        )


class InboxMessagesListResponse(Schema):
    messages: list[InboxMessageResponse]
    limit: int
    next_cursor: str | None = None


class CreateReplyRequest(Schema):
    body: str = Field(..., min_length=1, max_length=10_000, description="The reply text.")
    idempotency_key: str | None = Field(
        None,
        max_length=128,
        description="Optional client-chosen retry key. Same key + same body → replay first response.",
    )
    send: bool = Field(
        False,
        description=(
            "When true, immediately deliver the reply to the platform instead of "
            "leaving it as a draft. Requires the ``reply_from_inbox`` permission."
        ),
    )


class UpdateReplyRequest(Schema):
    body: str = Field(..., min_length=1, max_length=10_000, description="Replacement reply text.")


# ---------------------------------------------------------------------------
# AI writing assistant (apps.ai_writer)
# ---------------------------------------------------------------------------


class GenerateCaptionRequest(Schema):
    prompt: str = Field(
        ...,
        min_length=1,
        max_length=2_000,
        description="What the post should say, e.g. 'Announce our spring sale: 20% off this weekend'.",
    )
    social_account_ids: list[uuid.UUID] = Field(
        default_factory=list,
        max_length=25,
        description=(
            "Accounts the caption is for (must be in this key's allowlist). The caption is kept within "
            "the smallest of their character limits. Omit for a caption with no length target."
        ),
    )


class GenerateCaptionResponse(Schema):
    caption: str


class AdaptCaptionRequest(Schema):
    caption: str = Field(..., min_length=1, max_length=10_000, description="The caption to adapt.")
    social_account_ids: list[uuid.UUID] = Field(
        ...,
        min_length=1,
        max_length=25,
        description="Accounts to adapt it for (must be in this key's allowlist).",
    )


class AdaptedCaption(Schema):
    social_account_id: uuid.UUID
    platform: str
    caption: str = Field(
        ..., description="Ready to send as this account's ``platform_overrides`` caption when creating the post."
    )
    length: int = Field(..., description="Length as the platform counts it (after any escaping).")
    char_limit: int


class AdaptCaptionResponse(Schema):
    captions: list[AdaptedCaption]


# ---------------------------------------------------------------------------
# /hashtag-sets and /signatures — read
# ---------------------------------------------------------------------------


class HashtagSetSummary(Schema):
    id: uuid.UUID
    name: str
    hashtags: list[str] = Field(..., description="Each with its leading ``#``, ready to append to a caption.")


class HashtagSetsListResponse(Schema):
    hashtag_sets: list[HashtagSetSummary]


class SignatureSummary(Schema):
    id: uuid.UUID
    name: str
    body: str = Field(..., description="Text to append to a caption, after a blank line.")
    is_default: bool = Field(..., description="Whether the composer starts every new post in the workspace with it.")


class SignaturesListResponse(Schema):
    signatures: list[SignatureSummary]


# ---------------------------------------------------------------------------
# Error envelope (used by the exception handler in api.py)
# ---------------------------------------------------------------------------


class ErrorResponse(Schema):
    error: str
    detail: str | None = None
    tier: str | None = None
    limit: int | None = None
    remaining: int | None = None
    retry_after: int | None = None
    reset_at: dt.datetime | None = None


# ---------------------------------------------------------------------------
# /webhooks
# ---------------------------------------------------------------------------


class WebhookEndpointResponse(Schema):
    id: uuid.UUID
    url: str
    description: str = ""
    is_enabled: bool
    event_types: list[str]
    secret_hint: str = Field(..., description="Last characters of the signing secret, to tell secrets apart.")
    consecutive_failures: int = Field(0, description="Failed attempts in a row; reset by any success.")
    disabled_at: dt.datetime | None = Field(
        None, description="Set when the endpoint was turned off after repeated failures."
    )
    disabled_reason: str = ""
    created_at: dt.datetime
    updated_at: dt.datetime

    @field_serializer("disabled_at", "created_at", "updated_at")
    def _serialize_dt(self, value: dt.datetime | None) -> str | None:
        return _serialize_utc_z(value)

    @classmethod
    def from_endpoint(cls, endpoint) -> WebhookEndpointResponse:
        return cls(
            id=endpoint.id,
            url=endpoint.url,
            description=endpoint.description,
            is_enabled=endpoint.is_enabled,
            event_types=list(endpoint.event_types),
            secret_hint=endpoint.secret_hint,
            consecutive_failures=endpoint.consecutive_failures,
            disabled_at=endpoint.disabled_at,
            disabled_reason=endpoint.disabled_reason,
            created_at=endpoint.created_at,
            updated_at=endpoint.updated_at,
        )


class WebhookEndpointWithSecretResponse(WebhookEndpointResponse):
    secret: str = Field(..., description="The signing secret. Returned this once only; rotate it to get a new one.")

    @classmethod
    def from_endpoint_and_secret(cls, endpoint, secret: str) -> WebhookEndpointWithSecretResponse:
        return cls(**WebhookEndpointResponse.from_endpoint(endpoint).model_dump(), secret=secret)


class WebhookEndpointsListResponse(Schema):
    endpoints: list[WebhookEndpointResponse]


class CreateWebhookEndpointRequest(Schema):
    url: str = Field(..., max_length=2000, description="Public http(s) URL that accepts POST requests.")
    event_types: list[str] = Field(..., min_length=1, description="Event types to subscribe to, e.g. `post.published`.")
    description: str = Field("", max_length=255)
    is_enabled: bool = True


class UpdateWebhookEndpointRequest(Schema):
    url: str | None = Field(None, max_length=2000)
    event_types: list[str] | None = Field(None, min_length=1)
    description: str | None = Field(None, max_length=255)
    is_enabled: bool | None = Field(None, description="Setting this to true also clears an automatic shut-off.")


class WebhookDeliveryResponse(Schema):
    id: uuid.UUID = Field(..., description="Sent as the `PostDelegate-Delivery` header; stable across retries.")
    event_id: uuid.UUID
    event_type: str
    status: str = Field(..., description="`pending`, `delivered` or `failed`.")
    attempts: int
    last_status_code: int | None = None
    last_duration_ms: int | None = None
    last_error: str = ""
    response_body: str = Field("", description="First 2,000 characters of the receiver's response.")
    next_attempt_at: dt.datetime | None = None
    last_attempt_at: dt.datetime | None = None
    delivered_at: dt.datetime | None = None
    created_at: dt.datetime
    payload: dict = Field(..., description="The exact JSON body that is sent.")

    @field_serializer("next_attempt_at", "last_attempt_at", "delivered_at", "created_at")
    def _serialize_dt(self, value: dt.datetime | None) -> str | None:
        return _serialize_utc_z(value)

    @classmethod
    def from_delivery(cls, delivery) -> WebhookDeliveryResponse:
        return cls(
            id=delivery.id,
            event_id=delivery.event_id,
            event_type=delivery.event_type,
            status=delivery.status,
            attempts=delivery.attempts,
            last_status_code=delivery.last_status_code,
            last_duration_ms=delivery.last_duration_ms,
            last_error=delivery.last_error,
            response_body=delivery.response_body,
            next_attempt_at=delivery.next_attempt_at,
            last_attempt_at=delivery.last_attempt_at,
            delivered_at=delivery.delivered_at,
            created_at=delivery.created_at,
            payload=delivery.payload,
        )


class WebhookDeliveriesListResponse(Schema):
    deliveries: list[WebhookDeliveryResponse]
    limit: int
    next_cursor: str | None = None


# ---------------------------------------------------------------------------
# /reports — scheduled client reports
# ---------------------------------------------------------------------------


class ReportRunResponse(Schema):
    """One build of a report: its status, period and (when ready) headline figures."""

    id: uuid.UUID
    report_id: uuid.UUID
    status: str = Field(..., description="``pending`` | ``running`` | ``succeeded`` | ``failed``.")
    trigger: str = Field(..., description="``scheduled`` or ``manual``.")
    period_start: dt.date
    period_end: dt.date
    created_at: dt.datetime
    finished_at: dt.datetime | None = None
    error: str = Field("", description="Why the run failed, when it did.")
    emailed_to: int = Field(0, description="How many recipients the PDF was emailed to.")
    summary: str | None = Field(None, description="The report's 'what changed and what to do next' text, once ready.")
    metrics: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Headline figures once ready: ``key``, ``label``, ``value`` (display), ``delta`` (percent) per metric.",
    )


class ReportResponse(Schema):
    id: uuid.UUID
    name: str
    range_type: str
    sections: list[str]
    recipients: int = Field(..., description="Number of email recipients.")
    frequency: str = Field(..., description="``none`` | ``weekly`` | ``monthly``.")
    is_active: bool
    visible_to_clients: bool
    white_label: bool
    latest_run: ReportRunResponse | None = None
