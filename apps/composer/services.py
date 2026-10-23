"""Composer service helpers.

Where reusable "build me a post" logic lives so it can be called from the
HTMX-driven composer view *and* from the Agent API without duplicating
state-machine semantics.

The Phase 1 helper ``sync_post_scheduled_at`` keeps ``Post.scheduled_at``
in sync with the earliest per-platform ``PlatformPost.scheduled_at``;
the Phase 2 additions wrap ``Post`` + ``PlatformPost`` creation and
status transitions so audit + idempotency live in one place.
"""

from __future__ import annotations

import copy
import dataclasses
import datetime as dt
import uuid
from collections.abc import Iterable
from typing import Any


def sync_post_scheduled_at(post):
    """Keep ``Post.scheduled_at`` in sync with the earliest child time.

    Three cases:

    * Some children have a ``scheduled_at`` → set parent to the min.
    * No children have a ``scheduled_at`` AND parent has one cached →
      clear the parent so listings, calendars, and Coalesce fallbacks
      don't keep showing the post as scheduled. This branch covers the
      "agent cancels the last scheduled child" flow — without it,
      ``Post.scheduled_at`` lingered at the old value and the post
      kept appearing as scheduled in the UI.
    * No children, parent already null → no-op.

    A post that gains a committed schedule also drops any draft-stage
    ``proposed_publish_at``: this is the chokepoint the scheduling paths flow
    through (``create_post``, ``transition_platform_post`` for the REST
    ``/schedule`` route and the MCP ``schedule_draft`` tool, and the PATCH
    re-time which calls this after saving), so the suggestion and a real
    schedule never coexist. The ``scheduled_at`` aggregate logic is unchanged —
    the publisher's ``Coalesce(scheduled_at, post__scheduled_at)`` fallback
    depends on it — so only the proposal clear is added here. The one path that
    commits a child WITHOUT a ``scheduled_at`` (the composer's per-account chip
    endpoint) clears the proposal itself rather than routing through here, to
    avoid disturbing that aggregate.
    """
    times = list(post.platform_posts.exclude(scheduled_at__isnull=True).values_list("scheduled_at", flat=True))
    if not times:
        if post.scheduled_at is not None:
            post.scheduled_at = None
            post.save(update_fields=["scheduled_at", "updated_at"])
        return
    earliest = min(times)
    update_fields = []
    if post.scheduled_at != earliest:
        post.scheduled_at = earliest
        update_fields.append("scheduled_at")
    if post.proposed_publish_at is not None:
        post.proposed_publish_at = None
        update_fields.append("proposed_publish_at")
    if update_fields:
        post.save(update_fields=[*update_fields, "updated_at"])


# ---------------------------------------------------------------------------
# Phase 2 — Agent-API-facing post creation
# ---------------------------------------------------------------------------


_INITIAL_STATUSES = {"draft", "scheduled"}


def create_post(
    *,
    workspace,
    social_account,
    caption: str,
    media_asset_ids: Iterable[Any] | None = None,
    title: str = "",
    first_comment: str = "",
    internal_notes: str = "",
    scheduled_at: dt.datetime | None = None,
    proposed_publish_at: dt.datetime | None = None,
    author=None,
    status: str = "draft",
    platform_overrides: dict[Any, dict[str, str | None]] | None = None,
    parts: list[ThreadPartSpec] | None = None,
):
    """Create a ``Post`` + one ``PlatformPost`` for ``social_account``.

    The canonical service-layer entry point for the Agent API. The
    composer's HTMX-driven ``save_post`` view will be refactored to call
    this in a follow-up; for now both code paths coexist and only the
    API uses it.

    Contract:

    * ``status`` must be one of ``draft`` or ``scheduled``. Other
      states (``approved``, ``publishing``, …) are produced only via
      explicit transitions through ``transition_platform_post``.
    * If ``status='scheduled'``, ``scheduled_at`` is required and must
      be in the future. The publisher polls every ~15 s; a past
      ``scheduled_at`` is accepted and will fire on the very next tick,
      but the caller almost certainly meant "now-ish" so we leave the
      decision to them.
    * ``proposed_publish_at`` is an optional draft-stage suggestion. It is
      stored as-is regardless of ``status`` and carries no future-time
      requirement — it never reaches the publisher.
    * ``social_account.workspace_id`` must equal ``workspace.id``. The
      caller (auth class / view) already enforced scope, but we
      double-check here so anyone reaching the service directly can't
      bypass it.
    * Media assets are attached in order by their position in
      ``media_asset_ids``. They must already live in the workspace's
      media library.
    * ``parts`` are the thread's later parts (``ThreadPartSpec``); the
      caption and media above are its first. Their media must live in the
      workspace's library too, and a scheduled thread must be one the
      platform can publish (``thread_blocker``).

    Returns the persisted ``Post`` with one ``PlatformPost`` child.
    """
    from django.db import transaction

    from apps.composer.models import PlatformPost, Post, PostMedia
    from apps.media_library.models import MediaAsset

    if status not in _INITIAL_STATUSES:
        raise ValueError(
            f"create_post can only initialise status in {sorted(_INITIAL_STATUSES)}; "
            f"got {status!r}. Use transition_platform_post for other states."
        )
    if status == "scheduled" and scheduled_at is None:
        raise ValueError("status='scheduled' requires scheduled_at.")
    if social_account.workspace_id != workspace.id:
        raise ValueError(f"SocialAccount {social_account.id} is not in workspace {workspace.id}.")

    # Refuse to queue work against a connection the platform itself has
    # rejected. ``failed`` rows still count against the platform quota
    # (see ``QUOTA_CONSUMING_STATUSES`` in apps/api/limits.py), so each
    # silently-scheduled post against a disconnected/errored account
    # would burn one slot without ever publishing. Forcing an explicit
    # error here makes the agent reconnect first.
    if getattr(social_account, "needs_reconnect", False):
        raise ValueError(
            f"SocialAccount {social_account.id} is in connection_status "
            f"{social_account.connection_status!r}; reconnect it before scheduling."
        )

    if status == "scheduled":
        _require_approval_gate_passes(workspace)

    media_ids = list(media_asset_ids or [])
    # Pull and validate all referenced assets in a single query — fail
    # closed if any ID is missing or belongs to a different workspace.
    #
    # Normalize every id to a ``UUID`` for keying/lookup so all caller shapes
    # match identically: UUID objects (REST, coerced by Pydantic) and UUID
    # *strings* in any form (MCP, off the JSON-RPC wire — including uppercase or
    # braced). A raw string never compares equal to a UUID key, so without this
    # MCP-supplied assets are wrongly rejected as "not found in workspace".
    asset_map: dict[uuid.UUID, MediaAsset] = {}
    resolved: list[tuple[Any, uuid.UUID | None]] = []
    if media_ids:

        def _to_uuid(value):
            try:
                return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
            except (ValueError, TypeError, AttributeError):
                return None

        resolved = [(mid, _to_uuid(mid)) for mid in media_ids]
        found = list(MediaAsset.objects.filter(id__in=[u for _, u in resolved if u is not None], workspace=workspace))
        asset_map = {a.id: a for a in found}
        missing = [mid for mid, u in resolved if u is None or u not in asset_map]
        if missing:
            raise ValueError(f"Media asset(s) not found in workspace {workspace.id}: {missing}")

    if status == "scheduled":
        # A new post has no platform_extra, so a Pinterest pin has no board
        # yet and is refused here: it can be created as a draft, given a
        # board in the composer, and scheduled from there.
        reason = publish_blocker(
            social_account,
            media_types=usable_media_types(asset_map.values()),
        ) or media_rules_blocker(social_account, [asset_map[u] for _mid, u in resolved if u is not None])
        if reason:
            raise ValueError(reason)

    part_specs = list(parts or [])
    part_assets = resolve_thread_part_assets(workspace, part_specs)
    if status == "scheduled":
        reason = thread_blocker(social_account, spec_thread_parts(social_account, part_specs, part_assets))
        if reason:
            raise ValueError(reason)

    override = (platform_overrides or {}).get(social_account.id) or {}

    with transaction.atomic():
        post = Post.objects.create(
            workspace=workspace,
            author=author,
            title=title,
            caption=caption,
            first_comment=first_comment,
            internal_notes=internal_notes,
            scheduled_at=scheduled_at if status == "scheduled" else None,
            # A proposal is plain draft metadata — keep it independent of
            # ``status`` (unlike ``scheduled_at``, which only exists once the
            # post is committed to the publisher).
            proposed_publish_at=proposed_publish_at,
        )
        # Each override field is independent: ``None`` (or omitted)
        # means "no platform-specific override; fall back to the post
        # value via ``PlatformPost.effective_*``". Any string —
        # including ``""`` — is treated as an explicit override.
        PlatformPost.objects.create(
            post=post,
            social_account=social_account,
            status=status,
            scheduled_at=scheduled_at if status == "scheduled" else None,
            platform_specific_title=override.get("title"),
            platform_specific_caption=override.get("caption"),
            platform_specific_first_comment=override.get("first_comment"),
        )
        for position, (_mid, u) in enumerate(resolved):
            # ``u`` is validated non-None and present in ``asset_map`` above
            # (the ``missing`` check raises otherwise); narrow for mypy.
            assert u is not None
            PostMedia.objects.create(
                post=post,
                media_asset=asset_map[u],
                position=position,
            )
        if part_specs:
            set_post_parts(post, part_specs, asset_map=part_assets)

    # Keep the aggregate column consistent — the publisher polls
    # PlatformPost directly so this is just for listings/UI, but the
    # composer view code relies on it.
    sync_post_scheduled_at(post)
    return post


def transition_platform_post(
    platform_post,
    target_status: str,
    *,
    scheduled_at: dt.datetime | None = None,
):
    """Apply a state-machine transition and persist.

    Wraps ``PlatformPost.transition_to`` (which only mutates the model
    in memory) with the persistence + side-effects the API needs:

    * Save the new status.
    * On ``scheduled``, set ``scheduled_at`` if provided, and sync the
      aggregate ``Post.scheduled_at`` column.
    * On ``draft``, clear ``scheduled_at`` so the row stops being a
      target for the publisher's polling query.

    Raises ``ValueError`` (from ``transition_to``) if the transition
    isn't in ``VALID_TRANSITIONS``.
    """
    from django.db import transaction

    from apps.composer.models import PlatformPost

    # Approval workflow gate — Codex review (PR #53) flagged that the
    # ``POST /posts/{id}/schedule`` route reached this function with
    # ``target_status="scheduled"`` and bypassed the approval check
    # ``create_post`` enforces. The gate belongs here so EVERY path that
    # promotes a draft to scheduled (REST `/schedule` route, any future
    # MCP transition tool, the composer's HTMX `transition_platform_post`
    # view) is covered.
    if target_status == "scheduled":
        _require_approval_gate_passes(platform_post.post.workspace, platform_post)
        require_publishable(platform_post)

    with transaction.atomic():
        platform_post.transition_to(target_status)
        # Always include ``updated_at`` — Django docs: when ``update_fields``
        # is explicit, ``auto_now`` fields are NOT touched unless listed.
        # Without this every API/MCP-driven transition would leave
        # ``PlatformPost.updated_at`` frozen at creation time, breaking
        # any "modified since X" sync and ORDER BY updated_at view.
        # TRANSITION_FIELDS covers everything transition_to writes (the retry
        # budget and the publish handle, not just status), so the REST/MCP
        # scheduling path persists the same reset the composer does.
        update_fields = {*PlatformPost.TRANSITION_FIELDS, "updated_at"}
        if target_status == "scheduled":
            if scheduled_at is not None:
                platform_post.scheduled_at = scheduled_at
                update_fields.add("scheduled_at")
            # A committed child schedule supersedes any draft-stage proposed
            # publish time even when no concrete child timestamp is supplied.
            # The per-account composer chip relies on the parent fallback for
            # due-time selection, so clear only the proposal here; do not call
            # sync_post_scheduled_at until after this row is persisted.
            post = platform_post.post
            if post.proposed_publish_at is not None:
                post.proposed_publish_at = None
                post.save(update_fields=["proposed_publish_at", "updated_at"])
        elif target_status == "draft":
            if platform_post.scheduled_at is not None:
                platform_post.scheduled_at = None
                update_fields.add("scheduled_at")
            # A drafted child is no longer queued — drop its QueueEntry so the
            # slot frees as a gap. Covers cancel / unschedule paths that would
            # otherwise orphan the row with a stale assigned_slot_datetime.
            from apps.calendar.models import QueueEntry

            QueueEntry.objects.filter(
                post=platform_post.post,
                queue__social_account=platform_post.social_account,
            ).delete()
        elif target_status == "published":
            update_fields.add("published_at")  # set by transition_to
        platform_post.save(update_fields=list(update_fields))

    sync_post_scheduled_at(platform_post.post)
    return platform_post


def clone_post(post, *, author=None):
    """Create a fresh DRAFT duplicate of *post* (Clone / Repost).

    Copies caption, title, first comment, internal notes, tags, category, every
    per-platform override (``platform_specific_*`` + ``platform_extra``), all
    media attachments, and the thread's later parts with their overrides. The
    copy starts as a ``draft`` with no schedule, so a published (or any) post
    can be re-edited and re-queued without touching the original. ``author``
    defaults to the source author.
    """
    from django.db import transaction

    from apps.composer.models import PlatformPost, Post, PostMedia
    from apps.social_accounts.youtube_compliance import authored_youtube_settings

    with transaction.atomic():
        new_post = Post.objects.create(
            workspace=post.workspace,
            author=author or post.author,
            title=(f"Copy of {post.title}" if post.title else ""),
            caption=post.caption,
            first_comment=post.first_comment,
            internal_notes=post.internal_notes,
            tags=post.tags,
            category=post.category,
        )
        children = list(post.platform_posts.all())
        if children:
            PlatformPost.objects.bulk_create(
                [
                    PlatformPost(
                        post=new_post,
                        social_account=pp.social_account,
                        status="draft",
                        platform_specific_title=pp.platform_specific_title,
                        platform_specific_caption=pp.platform_specific_caption,
                        platform_specific_first_comment=pp.platform_specific_first_comment,
                        platform_specific_media=copy.deepcopy(pp.platform_specific_media),
                        platform_extra=copy.deepcopy(
                            authored_youtube_settings(pp.platform_extra)
                            if pp.social_account.platform == "youtube"
                            else pp.platform_extra or {}
                        ),
                    )
                    for pp in children
                ]
            )
        media = list(post.media_attachments.all())
        if media:
            PostMedia.objects.bulk_create(
                [
                    PostMedia(
                        post=new_post,
                        media_asset=pm.media_asset,
                        position=pm.position,
                        alt_text=pm.alt_text,
                        platform_overrides=copy.deepcopy(pm.platform_overrides),
                    )
                    for pm in media
                ]
            )
        _clone_thread_parts(post, new_post)
    return new_post


def _clone_thread_parts(post, new_post) -> None:
    """Copy ``post``'s thread parts, their media and per-account overrides onto ``new_post``."""
    from apps.composer.models import PlatformPostPart, PostPart, PostPartMedia

    new_children = {pp.social_account_id: pp for pp in new_post.platform_posts.all()}
    for part in post_thread_parts(post):
        new_part = PostPart.objects.create(
            post=new_post,
            position=part.position,
            caption=part.caption,
            delay_seconds=part.delay_seconds,
            is_finisher=part.is_finisher,
        )
        PostPartMedia.objects.bulk_create(
            [
                PostPartMedia(part=new_part, media_asset=pm.media_asset, position=pm.position, alt_text=pm.alt_text)
                for pm in part.media_attachments.all()
            ]
        )
        overrides = part.platform_states.exclude(platform_specific_caption__isnull=True).select_related("platform_post")
        PlatformPostPart.objects.bulk_create(
            [
                PlatformPostPart(
                    platform_post=new_children[state.platform_post.social_account_id],
                    part=new_part,
                    platform_specific_caption=state.platform_specific_caption,
                )
                for state in overrides
                if state.platform_post.social_account_id in new_children
            ]
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


#: Workspace approval modes that BLOCK a direct draft → scheduled
#: transition. The composer's HTMX path routes around this via a
#: separate ``submit_for_approval`` action; the Agent API surface uses
#: the same service layer so the constraint lives here once.
_APPROVAL_MODES_BLOCKING_DIRECT_SCHEDULE = frozenset({"required_internal", "required_internal_and_client"})


def _require_approval_gate_passes(workspace, platform_post=None) -> None:
    """Raise ``ValueError`` if the workspace forbids direct scheduling.

    Drafts are always allowed (they're explicit save-for-later);
    scheduling implies "ready to publish," which is exactly what the
    approval workflow gates.
    """
    if getattr(workspace, "approval_workflow_mode", "none") not in _APPROVAL_MODES_BLOCKING_DIRECT_SCHEDULE:
        return
    if platform_post is not None:
        from apps.approvals.policy import approval_allows_publish

        if approval_allows_publish(platform_post):
            return
    raise ValueError(
        "Workspace requires approval before scheduling; create the post as a "
        "draft and route it through the approval workflow."
    )


def usable_media_types(assets) -> set[str]:
    """The media types among ``assets`` the publisher will actually upload.

    It skips an attachment whose asset has no stored file, so one of those
    can't satisfy a platform that needs media — or a video.
    """
    return {asset.media_type for asset in assets if asset.file}


def _usable_attachments():
    """``PostMedia`` rows ``usable_media_types`` would count, as a queryset."""
    from apps.composer.models import PostMedia

    return PostMedia.objects.exclude(media_asset__file="")


def post_media_types(post, *, excluding=None) -> set[str]:
    """``usable_media_types`` for ``post``'s attachments, in one query.

    ``excluding`` (a ``PostMedia``) answers for the post as it would be once
    that attachment is removed.
    """
    attachments = _usable_attachments().filter(post_id=post.pk)
    if excluding is not None:
        attachments = attachments.exclude(pk=excluding.pk)
    return set(attachments.values_list("media_asset__media_type", flat=True).distinct())


def media_types_by_post(post_ids) -> dict:
    """``post_media_types`` for many posts in one query; absent ids have none."""
    by_post: dict = {}
    rows = _usable_attachments().filter(post_id__in=post_ids).values_list("post_id", "media_asset__media_type")
    for post_id, media_type in rows.distinct():
        by_post.setdefault(post_id, set()).add(media_type)
    return by_post


def media_blocker(social_account, *, media_types) -> str:
    """Why ``social_account`` can't publish a post with these media, or "".

    TikTok and YouTube publish only video; Instagram and Pinterest need an
    image or a video. Their providers refuse anything else before calling out.
    """
    from apps.media_library.models import MediaAsset
    from providers import is_video_only, requires_media

    platform = social_account.get_platform_display()
    if is_video_only(social_account.platform) and MediaAsset.MediaType.VIDEO not in media_types:
        return f"{platform} can only publish videos. Add a video to this post before scheduling it."
    if requires_media(social_account.platform) and not media_types:
        return f"{platform} needs an image or a video. Add one to this post before scheduling it."
    return ""


def post_media_assets(post, *, excluding=None) -> list:
    """``post``'s attached ``MediaAsset``s the publisher would upload, in order.

    ``excluding`` (a ``PostMedia``) answers for the post as it would be once
    that attachment is removed.
    """
    attachments = _usable_attachments().filter(post_id=post.pk).select_related("media_asset").order_by("position")
    if excluding is not None:
        attachments = attachments.exclude(pk=excluding.pk)
    return [pm.media_asset for pm in attachments]


def media_assets_by_post(post_ids) -> dict:
    """``post_media_assets`` for many posts in one query; absent ids have none."""
    by_post: dict = {}
    rows = _usable_attachments().filter(post_id__in=post_ids).select_related("media_asset")
    for pm in rows.order_by("post_id", "position"):
        by_post.setdefault(pm.post_id, []).append(pm.media_asset)
    return by_post


MEDIA_ISSUE_ERROR = "error"
MEDIA_ISSUE_WARNING = "warning"


@dataclasses.dataclass(frozen=True)
class MediaIssue:
    """One way a post's media breaks a platform's ``MediaRules``.

    An ``error`` stops the post being scheduled; a ``warning`` is shown and
    the user decides.
    """

    level: str
    message: str

    @property
    def is_error(self) -> bool:
        return self.level == MEDIA_ISSUE_ERROR


# How a post type is named in a media issue ("… in a carousel").
_POST_TYPE_NOUNS = {
    "image": "an image post",
    "video": "a video post",
    "carousel": "a carousel",
    "reel": "a Reel",
    "story": "a Story",
    "short": "a Short",
    "pin": "a Pin",
}

# Ratios worth naming the usual way; anything else reads as "1.33:1".
_RATIO_LABELS = {1.0: "1:1", 0.8: "4:5", 0.5625: "9:16", 1.7778: "16:9", 0.6667: "2:3", 1.91: "1.91:1"}

# A shape this close to a recommended one counts as matching it.
_RECOMMENDED_RATIO_TOLERANCE = 0.05

# Slack on a hard ratio bound, so a 1080x1350 image (exactly 4:5) is never
# refused over rounding.
_RATIO_EPSILON = 0.005


def _ratio_label(ratio: float) -> str:
    return _RATIO_LABELS.get(round(ratio, 4)) or f"{ratio:.2f}:1"


def _size_label(width, height) -> str:
    """A resolution bound, which may set only one side: "360x360px", "1080px wide"."""
    if width and height:
        return f"{width}x{height}px"
    return f"{width}px wide" if width else f"{height}px tall"


def _duration_label(seconds: float) -> str:
    # Seconds up to two minutes, so a 90-second limit reads as "90s".
    if seconds < 120:
        return f"{seconds:g}s" if seconds == int(seconds) else f"{seconds:.1f}s"
    minutes, secs = divmod(int(round(seconds)), 60)
    return f"{minutes} min" if not secs else f"{minutes}:{secs:02d} min"


def media_rule_issues(social_account, assets, *, platform_extra: dict | None = None) -> list[MediaIssue]:
    """Where ``assets`` break the media rules ``social_account``'s platform declares.

    ``assets`` are the post's ``MediaAsset``s in attachment order. The post
    type is the one the publisher would pick for them (``_resolve_post_type``),
    so the rules checked are the ones the platform will apply. A dimension or
    duration the library hasn't measured (0) is not checked.
    """
    from django.template.defaultfilters import filesizeformat

    from apps.media_library.models import MediaAsset
    from apps.publisher.engine import PublishEngine
    from providers import is_video_only, media_rules_for

    platform = social_account.platform
    assets = [a for a in assets if a.file]
    # The publisher drops everything but video for these (see _dispatch_to_provider).
    if is_video_only(platform):
        assets = [a for a in assets if a.media_type == MediaAsset.MediaType.VIDEO]
    if not assets:
        return []
    post_type = PublishEngine._resolve_post_type(platform, platform_extra or {}, len(assets), assets[0].media_type)
    rules = media_rules_for(platform, post_type)
    if rules is None:
        return []

    name = social_account.get_platform_display()
    noun = _POST_TYPE_NOUNS.get(post_type.value, "a post")
    issues: list[MediaIssue] = []

    def add(limit, message):
        level = MEDIA_ISSUE_WARNING if limit in rules.advisory else MEDIA_ISSUE_ERROR
        issues.append(MediaIssue(level, message))

    if rules.max_items is not None and len(assets) > rules.max_items:
        add("max_items", f"{name} takes at most {rules.max_items} items in {noun}; this post has {len(assets)}.")
    elif rules.published_items is not None and len(assets) > rules.published_items:
        first = "the first item is" if rules.published_items == 1 else f"the first {rules.published_items} items are"
        issues.append(
            MediaIssue(MEDIA_ISSUE_WARNING, f"Only {first} published to {name}; this post has {len(assets)}.")
        )

    for asset in assets:
        label = asset.filename or "This file"
        is_video = asset.media_type == MediaAsset.MediaType.VIDEO

        if rules.media_types is not None and asset.media_type not in rules.media_types:
            kind = asset.get_media_type_display()
            add("media_types", f"{name} doesn't take {kind} files in {noun} ({label}).")
            continue

        if asset.width and asset.height:
            ratio = asset.width / asset.height
            too_narrow = rules.min_aspect_ratio is not None and ratio < rules.min_aspect_ratio - _RATIO_EPSILON
            too_wide = rules.max_aspect_ratio is not None and ratio > rules.max_aspect_ratio + _RATIO_EPSILON
            if too_narrow or too_wide:
                if rules.min_aspect_ratio is not None and rules.max_aspect_ratio is not None:
                    allowed = (
                        f"between {_ratio_label(rules.min_aspect_ratio)} and {_ratio_label(rules.max_aspect_ratio)}"
                    )
                elif too_wide:
                    allowed = f"no wider than {_ratio_label(rules.max_aspect_ratio)}"
                else:
                    allowed = f"no narrower than {_ratio_label(rules.min_aspect_ratio)}"
                add(
                    "aspect_ratio",
                    f"{label} is {_ratio_label(ratio)}; {name} takes media {allowed} in {noun}. "
                    "Crop it to fit in the media library.",
                )
            elif (
                rules.recommended_aspect_ratio is not None
                and abs(ratio - rules.recommended_aspect_ratio) / rules.recommended_aspect_ratio
                > _RECOMMENDED_RATIO_TOLERANCE
            ):
                issues.append(
                    MediaIssue(
                        MEDIA_ISSUE_WARNING,
                        f"{name} recommends {_ratio_label(rules.recommended_aspect_ratio)} for {noun}; "
                        f"{label} is {_ratio_label(ratio)} and may be cropped.",
                    )
                )

            if (rules.min_width and asset.width < rules.min_width) or (
                rules.min_height and asset.height < rules.min_height
            ):
                add(
                    "min_resolution",
                    f"{label} is {asset.width}x{asset.height}px; {name} needs at least "
                    f"{_size_label(rules.min_width, rules.min_height)}.",
                )
            if (rules.max_width and asset.width > rules.max_width) or (
                rules.max_height and asset.height > rules.max_height
            ):
                add(
                    "max_resolution",
                    f"{label} is {asset.width}x{asset.height}px; {name} takes at most "
                    f"{_size_label(rules.max_width, rules.max_height)}.",
                )

        if is_video and asset.duration:
            if rules.min_duration and asset.duration < rules.min_duration:
                add(
                    "min_duration",
                    f"{label} is {_duration_label(asset.duration)} long; {name} needs at least "
                    f"{_duration_label(rules.min_duration)} in {noun}.",
                )
            if rules.max_duration is not None and asset.duration > rules.max_duration:
                add(
                    "max_duration",
                    f"{label} is {_duration_label(asset.duration)} long; {name} takes at most "
                    f"{_duration_label(rules.max_duration)} in {noun}.",
                )

        limit_name, max_bytes = (
            ("max_video_bytes", rules.max_video_bytes) if is_video else ("max_image_bytes", rules.max_image_bytes)
        )
        if max_bytes is not None and asset.file_size > max_bytes:
            add(
                limit_name,
                f"{label} is {filesizeformat(asset.file_size)}; {name} takes files up to {filesizeformat(max_bytes)}.",
            )

    return issues


def media_rules_blocker(social_account, assets, *, platform_extra: dict | None = None) -> str:
    """The first ``media_rule_issues`` error for ``social_account``, or ""."""
    issues = media_rule_issues(social_account, assets, platform_extra=platform_extra)
    return next((issue.message for issue in issues if issue.is_error), "")


def publish_blocker(social_account, *, media_types, platform_extra: dict | None = None) -> str:
    """Why a post can't publish to ``social_account`` as it stands, or "".

    The one statement of this rule. The publisher checks it before calling a
    platform, and every path that schedules a post checks it first so the user
    hears about it then rather than after the post's other platforms have gone
    out. Only failures that are certain before the platform is ever called: the
    media a platform can't do without (``media_blocker``), and a pin with no
    board, which our own Pinterest provider refuses.
    """
    reason = media_blocker(social_account, media_types=media_types)
    if reason:
        return reason
    if social_account.platform == "pinterest" and not (platform_extra or {}).get("board_id"):
        return "Pinterest needs a board for every pin. Choose one for this post in the composer before scheduling it."
    return ""


def require_publishable(platform_post, *, media_types=None, assets=None) -> None:
    """Raise ``ValueError`` if ``platform_post`` is certain to fail at publish time.

    Also refuses media that breaks a blocking platform media rule
    (``media_rules_blocker``) and a thread the platform can't publish
    (``platform_post_thread_blocker``) — checked here, where a post is scheduled, and
    not in ``publish_blocker``, so a post scheduled before a rule existed
    still gets its attempt. ``media_types`` and ``assets`` let a caller
    checking many rows answer them for all of them in one query
    (``media_types_by_post``, ``media_assets_by_post``) instead of one per row.
    """
    if media_types is None:
        media_types = post_media_types(platform_post.post)
    reason = publish_blocker(
        platform_post.social_account,
        media_types=media_types,
        platform_extra=platform_post.platform_extra,
    )
    if not reason:
        if assets is None:
            assets = post_media_assets(platform_post.post)
        reason = media_rules_blocker(platform_post.social_account, assets, platform_extra=platform_post.platform_extra)
    if not reason:
        # A thread the platform can't carry would publish its first part and
        # then strand the rest, so it is refused here with everything else.
        reason = platform_post_thread_blocker(platform_post)
    if reason:
        raise ValueError(reason)


def media_change_blocker(post, *, media_types_after, assets_after=None) -> str:
    """Why a media change would strand a scheduled row, or "".

    Scheduling checks the post as it stood then; this stops the media a
    platform needs being taken away afterwards — the video from a TikTok post,
    the last image from a pin — which would leave the row queued to fail. Only
    rows already ``scheduled`` count; anything else is checked again when it is
    scheduled. ``assets_after``, when given, also holds the new media to the
    platform's blocking media rules (``media_rules_blocker``).
    """
    from apps.composer.models import PlatformPost
    from providers import is_video_only

    children = post.platform_posts.filter(status=PlatformPost.Status.SCHEDULED).select_related("social_account")
    for child in children:
        if media_blocker(child.social_account, media_types=media_types_after):
            platform = child.social_account.get_platform_display()
            need = "a video" if is_video_only(child.social_account.platform) else "an image or a video"
            return (
                f"This post is scheduled to {platform}, which needs {need} on it. "
                f"Keep one on the post, or unschedule it for {platform} first."
            )
        if assets_after is not None:
            reason = media_rules_blocker(child.social_account, assets_after, platform_extra=child.platform_extra)
            if reason:
                return reason
    return ""


# ---------------------------------------------------------------------------
# Threads
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class ThreadPartSpec:
    """One later part of a thread, as the composer, the API or MCP describe it.

    ``caption_overrides`` maps a social account id to that account's caption
    for this part — the part-level ``platform_specific_caption``. ``None`` (or
    a missing key) means the part's own caption.
    """

    caption: str = ""
    media_asset_ids: list = dataclasses.field(default_factory=list)
    delay_seconds: int = 0
    is_finisher: bool = False
    caption_overrides: dict = dataclasses.field(default_factory=dict)


def _as_uuid(value):
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def parse_thread_parts(raw) -> list[ThreadPartSpec]:
    """``ThreadPartSpec``s from the composer's JSON thread payload.

    Raises ``ValueError`` on a shape the composer never sends, so a crafted
    request is refused rather than half-applied.
    """
    from apps.composer.models import PostPart

    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        raise ValueError("Thread parts must be a list.")
    specs = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("Each thread part must be an object.")
        try:
            delay = int(item.get("delay_seconds") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("A thread part's delay must be a whole number of seconds.") from exc
        if not 0 <= delay <= PostPart.MAX_DELAY_SECONDS:
            raise ValueError("A thread part can wait at most 24 hours after the part before it.")
        overrides = item.get("caption_overrides") or {}
        if not isinstance(overrides, dict):
            raise ValueError("Thread part overrides must be an object.")
        specs.append(
            ThreadPartSpec(
                caption=str(item.get("caption") or ""),
                media_asset_ids=list(item.get("media_asset_ids") or []),
                delay_seconds=delay,
                is_finisher=bool(item.get("is_finisher")),
                caption_overrides={
                    str(acc_id): (None if text is None else str(text)) for acc_id, text in overrides.items()
                },
            )
        )
    return specs


def resolve_thread_part_assets(workspace, specs) -> dict:
    """Every ``MediaAsset`` the parts name, keyed by id, in one query.

    Fails closed like ``create_post``: an id that is malformed or outside the
    workspace's library raises ``ValueError``.
    """
    from apps.media_library.models import MediaAsset

    wanted = [(mid, _as_uuid(mid)) for spec in specs for mid in spec.media_asset_ids]
    if not wanted:
        return {}
    found = {a.id: a for a in MediaAsset.objects.filter(id__in=[u for _, u in wanted if u], workspace=workspace)}
    missing = [mid for mid, u in wanted if u is None or u not in found]
    if missing:
        raise ValueError(f"Media asset(s) not found in workspace {workspace.id}: {missing}")
    return found


def _ordered_specs(specs) -> list:
    """``specs`` with the finisher moved last; at most one finisher."""
    finishers = [spec for spec in specs if spec.is_finisher]
    if len(finishers) > 1:
        raise ValueError("A thread can have only one finisher.")
    return [spec for spec in specs if not spec.is_finisher] + finishers


def spec_thread_parts(social_account, specs, asset_map) -> list[tuple[str, list]]:
    """``(caption, assets)`` per part as ``social_account`` would publish ``specs``."""
    key = str(social_account.id)
    parts = []
    for spec in _ordered_specs(specs):
        override = spec.caption_overrides.get(key)
        caption = spec.caption if override is None else override
        parts.append((caption, [asset_map[_as_uuid(mid)] for mid in spec.media_asset_ids]))
    return parts


def set_post_parts(post, specs, *, asset_map=None) -> None:
    """Replace ``post``'s thread parts with ``specs``, in order.

    ``asset_map`` is ``resolve_thread_part_assets`` for the same specs, when the
    caller already has it. Overrides are stored only for accounts the post
    targets. Refused once any part has gone out on any account: those rows are
    the record of what is live, and replacing the parts would delete it.
    """
    from django.db import transaction

    from apps.composer.models import PlatformPostPart, PostPart, PostPartMedia

    specs = _ordered_specs(specs)
    if asset_map is None:
        asset_map = resolve_thread_part_assets(post.workspace, specs)
    started = PlatformPostPart.objects.filter(platform_post__post=post).exclude(status=PlatformPostPart.Status.PENDING)
    if started.exists():
        raise ValueError("This thread has already started publishing, so its parts can't be changed.")

    children = {str(pp.social_account_id): pp for pp in post.platform_posts.all()}
    with transaction.atomic():
        post.thread_parts.all().delete()
        for position, spec in enumerate(specs, start=1):
            part = PostPart.objects.create(
                post=post,
                position=position,
                caption=spec.caption,
                delay_seconds=spec.delay_seconds,
                is_finisher=spec.is_finisher,
            )
            seen = set()
            for media_position, mid in enumerate(spec.media_asset_ids):
                asset = asset_map[_as_uuid(mid)]
                if asset.id in seen:
                    continue
                seen.add(asset.id)
                PostPartMedia.objects.create(part=part, media_asset=asset, position=media_position)
            for acc_id, text in spec.caption_overrides.items():
                child = children.get(str(acc_id))
                if child is not None and text is not None:
                    PlatformPostPart.objects.create(platform_post=child, part=part, platform_specific_caption=text)


def post_thread_parts(post) -> list:
    """``post``'s thread parts in order, with their media."""
    return list(post.thread_parts.prefetch_related("media_attachments__media_asset").order_by("position", "created_at"))


def thread_parts_for(platform_post, parts=None) -> list[tuple]:
    """``(part, caption, assets)`` per thread part as ``platform_post``'s account publishes it.

    ``caption`` honours the account's override; ``assets`` are the part's
    attached ``MediaAsset``s the publisher would upload, in order.
    """
    if parts is None:
        parts = post_thread_parts(platform_post.post)
    if not parts:
        return []
    overrides = dict(
        platform_post.part_states.exclude(platform_specific_caption__isnull=True).values_list(
            "part_id", "platform_specific_caption"
        )
    )
    result = []
    for part in parts:
        caption = overrides.get(part.id, part.caption)
        assets = [pm.media_asset for pm in part.media_attachments.all() if pm.media_asset.file]
        result.append((part, caption, assets))
    return result


def thread_blocker(social_account, parts) -> str:
    """Why ``social_account`` can't publish these later thread parts, or "".

    ``parts`` is ``(caption, assets)`` per part after the first post. The
    rules come from the platform's ``ThreadCapability``: whether it takes a
    thread at all, how many parts, how long each may be, and whether a part
    can carry media — which then has to pass the platform's media rules like
    the first post's does.
    """
    from providers import thread_capability_for
    from providers.types import ThreadMode

    if not parts:
        return ""
    capability = thread_capability_for(social_account.platform)
    name = social_account.get_platform_display()
    if not capability.supports_threads:
        return f"{name} can't publish a thread. Remove the extra parts, or publish this to {name} as a single post."
    if capability.mode is ThreadMode.COMMENTS and not social_account.supports_first_comment():
        return (
            f"{name} adds a thread's later parts as comments, and this account can't post comments. "
            "Remove the extra parts for it."
        )
    if 1 + len(parts) > capability.max_parts:
        return f"{name} takes at most {capability.max_parts} parts in a thread; this one has {1 + len(parts)}."

    limit = capability.part_max_length or social_account.char_limit
    noun = "comment" if capability.mode is ThreadMode.COMMENTS else "post"
    for number, (caption, assets) in enumerate(parts, start=2):
        if not (caption or "").strip() and not assets:
            return f"Part {number} of the thread is empty. Write something in it or remove it."
        length = social_account.caption_wire_length(caption or "")
        if length > limit:
            return f"Part {number} is {length} characters; {name} takes at most {limit} per {noun}."
        if assets and not capability.media_per_part:
            return f"{name} adds later parts as comments, which can't carry media. Remove the media from part {number}."
        if assets:
            reason = media_rules_blocker(social_account, assets)
            if reason:
                return f"Part {number}: {reason}"
    return ""


def platform_post_thread_blocker(platform_post, parts=None) -> str:
    """``thread_blocker`` for the thread ``platform_post`` would publish."""
    return thread_blocker(
        platform_post.social_account,
        [(caption, assets) for _part, caption, assets in thread_parts_for(platform_post, parts)],
    )


def thread_change_blocker(post, specs, asset_map) -> str:
    """Why replacing the thread with ``specs`` would strand a scheduled row, or "".

    The thread counterpart of ``media_change_blocker``: only rows already
    ``scheduled`` count, the rest are checked when they are scheduled.
    """
    from apps.composer.models import PlatformPost

    children = post.platform_posts.filter(status=PlatformPost.Status.SCHEDULED).select_related("social_account")
    for child in children:
        reason = thread_blocker(child.social_account, spec_thread_parts(child.social_account, specs, asset_map))
        if reason:
            return reason
    return ""
