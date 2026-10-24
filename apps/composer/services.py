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
import hashlib
import logging
import re
import unicodedata
import uuid
from collections.abc import Iterable
from typing import Any

from providers import mention_lookup_for, post_settings_error_for

logger = logging.getLogger(__name__)


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
    post_settings: dict[str, Any] | None = None,
    shorten_links: bool | None = None,
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
    * ``post_settings`` are the per-post settings the platform declares
      (``SocialProvider.post_settings``, listed on the account); a key the
      platform doesn't declare is refused.
    * ``shorten_links`` is the author's choice about shortening the post's links when it is scheduled
      (``apps.links``); ``None`` follows the workspace's preference.
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
    if status == "scheduled":
        from apps.onboarding.sample import SAMPLE_REFUSAL, is_sample_workspace

        if is_sample_workspace(workspace.id):
            raise ValueError(SAMPLE_REFUSAL)
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

    extra = clean_post_settings(social_account.platform, post_settings)

    if status == "scheduled":
        # A new post has no platform_extra, so a Pinterest pin has no board
        # yet and is refused here: it can be created as a draft, given a
        # board in the composer, and scheduled from there.
        reason = publish_blocker(
            social_account,
            media_types=usable_media_types(asset_map.values()),
            platform_extra=extra,
        ) or media_rules_blocker(social_account, [asset_map[u] for _mid, u in resolved if u is not None])
        override_caption = ((platform_overrides or {}).get(social_account.id) or {}).get("caption")
        reason = reason or text_blocker(social_account, caption if override_caption is None else override_caption)
        if reason:
            raise ValueError(reason)

    override = (platform_overrides or {}).get(social_account.id) or {}

    part_specs = list(parts or [])
    part_assets = resolve_thread_part_assets(workspace, part_specs)
    if status == "scheduled":
        override_comment = override.get("first_comment")
        reason = thread_blocker(
            social_account,
            spec_thread_parts(social_account, part_specs, part_assets),
            first_comment=first_comment if override_comment is None else override_comment,
        )
        if reason:
            raise ValueError(reason)

    with transaction.atomic():
        post = Post.objects.create(
            workspace=workspace,
            author=author,
            title=title,
            caption=caption,
            first_comment=first_comment,
            internal_notes=internal_notes,
            shorten_links=shorten_links,
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
            platform_extra=extra,
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

    YouTube publishes only video; TikTok, Instagram and Pinterest need an
    image or a video. Their providers refuse anything else before calling out.
    """
    from apps.media_library.models import MediaAsset
    from providers import accepts_media_for, is_video_only, publishes_documents_for, requires_media

    platform = social_account.get_platform_display()
    if media_types and not accepts_media_for(social_account.platform):
        return f"{platform} posts are text or links. Remove the media from this post, or deselect {platform}."
    # A document is never dropped to publish the caption alone: a network without a documents API refuses it,
    # and one with it publishes the document as the post's only attachment.
    if MediaAsset.MediaType.DOCUMENT in media_types:
        if not publishes_documents_for(social_account.platform):
            return (
                f"{platform} can't publish PDF documents. Remove the document from this post, or deselect {platform}."
            )
        if len(media_types) > 1:
            return f"A document posts on its own on {platform}. Remove the other media from this post, or the document."
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
    "document": "a document post",
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


# What a post format a person picks in the composer needs, for one that its attachments don't fit.
_FORMAT_NEEDS = {
    "reel": "exactly one video",
    "story": "one photo or video",
    "video": "a video",
}


def _chosen_format_issue(social_account, assets, platform_extra: dict) -> MediaIssue | None:
    """An error when the post format chosen in the platform's own settings doesn't fit the media, else None.

    The publisher ignores a stale ``post_type`` hint and falls back to the media's own type, which is right for
    a hint written behind the person's back but not for one they picked from the platform's settings: a Story
    with no photo would go out as a text post, a Reel of an image as a feed post. Only platforms that declare
    ``post_type`` as a setting are checked.
    """
    from apps.publisher.engine import PublishEngine
    from providers import post_settings_for
    from providers.types import PostType

    hint = platform_extra.get("post_type") or ""
    if hint not in _FORMAT_NEEDS or "post_type" not in {f.key for f in post_settings_for(social_account.platform)}:
        return None
    first = assets[0].media_type if assets else None
    if PublishEngine._hint_matches_media(PostType(hint), len(assets), first):
        return None
    noun = _POST_TYPE_NOUNS.get(hint, "this format")
    return MediaIssue(
        MEDIA_ISSUE_ERROR,
        f"{social_account.get_platform_display()} {noun.split(' ', 1)[1]} needs {_FORMAT_NEEDS[hint]}. "
        "Change the media, or choose another post format.",
    )


def _has_shape_rules(rules) -> bool:
    """Whether ``rules`` limit anything that needs a video's measured duration or dimensions."""
    return bool(
        rules.min_duration
        or rules.max_duration is not None
        or rules.min_aspect_ratio is not None
        or rules.max_aspect_ratio is not None
        or rules.min_width
        or rules.min_height
        or rules.max_width
        or rules.max_height
    )


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
    chosen = _chosen_format_issue(social_account, assets, platform_extra or {})
    if chosen is not None:
        return [chosen]
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

        if asset.metadata_unverified and _has_shape_rules(rules):
            issues.append(
                MediaIssue(
                    MEDIA_ISSUE_WARNING,
                    f"{label} couldn't be measured, so its length and shape weren't checked against {name}'s "
                    "limits; the file size was. Re-upload it, or check it by hand.",
                )
            )

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

        if is_video:
            limit_name, max_bytes = "max_video_bytes", rules.max_video_bytes
        elif asset.media_type == MediaAsset.MediaType.DOCUMENT:
            limit_name, max_bytes = "max_document_bytes", rules.max_document_bytes
        else:
            limit_name, max_bytes = "max_image_bytes", rules.max_image_bytes
        if max_bytes is not None and asset.file_size > max_bytes:
            if limit_name == "max_image_bytes" and rules.compress_images:
                issues.append(
                    MediaIssue(
                        MEDIA_ISSUE_WARNING,
                        f"{label} is {filesizeformat(asset.file_size)}; {name} takes images up to "
                        f"{filesizeformat(max_bytes)}, so a smaller copy is sent. Your original is unchanged.",
                    )
                )
            else:
                add(
                    limit_name,
                    f"{label} is {filesizeformat(asset.file_size)}; {name} takes files up to "
                    f"{filesizeformat(max_bytes)}.",
                )

    return issues


def clean_post_settings(platform: str, settings: dict[str, Any] | None) -> dict[str, Any]:
    """``settings`` restricted to what ``platform`` declares, or ``ValueError``.

    The API and MCP take the same keys the composer's form does. An undeclared
    key would be stored and silently ignored at publish time, and a value of
    the wrong type (``"false"`` for a checkbox) would publish wrongly, so both
    are refused where the caller can still fix them.
    """
    from providers import post_settings_for

    if not settings:
        return {}
    declared = {field.key: field for field in post_settings_for(platform)}
    unknown = sorted(set(settings) - set(declared))
    if unknown:
        allowed = ", ".join(sorted(declared)) or "none"
        raise ValueError(f"Unknown post setting(s) {unknown} for {platform}; this platform accepts: {allowed}.")
    cleaned: dict[str, Any] = {}
    for key, value in settings.items():
        if value is None or value == "":
            continue
        if declared[key].kind == "checkbox":
            if not isinstance(value, bool):
                raise ValueError(f"Post setting {key!r} must be true or false.")
        elif isinstance(value, str):
            value = value.strip()
        else:
            raise ValueError(f"Post setting {key!r} must be a string.")
        cleaned[key] = value
    return cleaned


def replace_post_settings(platform: str, platform_extra: dict | None, settings: dict[str, Any]) -> dict:
    """``platform_extra`` with the keys ``platform`` declares replaced by ``settings``.

    A declared key missing from ``settings`` is cleared; anything else already
    stored (state the publisher keeps there) stays.
    """
    from providers import post_settings_for

    declared = {field.key for field in post_settings_for(platform)}
    kept = {key: value for key, value in (platform_extra or {}).items() if key not in declared}
    return {**kept, **settings}


def parse_post_settings(platform: str, get, account_id) -> dict:
    """The ``platform_extra`` entries a platform's ``post_settings`` form fields describe.

    ``get`` reads one form value (``request.POST.get``). Fields are named
    ``ps_<key>_<account id>``, the way the other per-account composer fields
    are. A blank text or select field is left out so a cleared setting is
    removed; a checkbox is always recorded.
    """
    from providers import post_settings_for

    values: dict = {}
    for field in post_settings_for(platform):
        raw = get(f"ps_{field.key}_{account_id}", "")
        if field.kind == "checkbox":
            values[field.key] = raw == "true"
        elif str(raw).strip():
            values[field.key] = str(raw).strip()
    return values


def media_rules_blocker(social_account, assets, *, platform_extra: dict | None = None) -> str:
    """The first ``media_rule_issues`` error for ``social_account``, or ""."""
    issues = media_rule_issues(social_account, assets, platform_extra=platform_extra)
    return next((issue.message for issue in issues if issue.is_error), "")


def text_blocker(social_account, text: str, *, counting=None) -> str:
    """Why ``text`` can't be published to ``social_account`` as written, or "".

    The one place a caption is held to the platform's limits, so the API, the
    MCP tools, the calendar and the publisher refuse what the composer's
    counter already flags, in the same units (``SocialProvider.text_length_unit``).
    Nothing is cut to fit: a caption the platform would shorten or reject is
    returned to its author with the sentence to act on. ``counting`` rewrites
    links still to be shortened to the length they will have (``link_counting``).
    """
    from providers import PROVIDER_REGISTRY

    measured = counting(text or "") if counting else text or ""
    length = social_account.caption_wire_length(measured)
    limit = social_account.char_limit
    if length > limit:
        name = social_account.get_platform_display()
        return f"The caption is {length} characters; {name} takes at most {limit}."
    provider_cls = PROVIDER_REGISTRY.get(social_account.platform)
    return provider_cls.text_issue(measured) if provider_cls else ""


def platform_post_text_blocker(platform_post) -> str:
    """``text_blocker`` for the caption ``platform_post`` would publish."""
    from apps.links.services import link_counting

    return text_blocker(
        platform_post.social_account,
        platform_post.effective_caption,
        counting=link_counting(platform_post.post.workspace, post=platform_post.post),
    )


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
    return post_settings_error_for(social_account.platform, platform_extra)


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
        reason = platform_post_text_blocker(platform_post)
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
    from apps.media_library.models import MediaAsset
    from providers import is_video_only

    children = post.platform_posts.filter(status=PlatformPost.Status.SCHEDULED).select_related("social_account")
    for child in children:
        reason = media_blocker(child.social_account, media_types=media_types_after)
        if reason and MediaAsset.MediaType.DOCUMENT in media_types_after:
            return reason
        if reason:
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
    if thread_started(post):
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
    # Parts saved after their account was scheduled still get their links tagged and shortened.
    from apps.links.services import prepare_post

    prepare_post(post)


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


def thread_blocker(social_account, parts, *, first_comment: str = "", counting=None) -> str:
    """Why ``social_account`` can't publish these later thread parts, or "".

    ``parts`` is ``(caption, assets)`` per part after the first post. The
    rules come from the platform's ``ThreadCapability``: whether it takes a
    thread at all, how many parts, how long each may be, and whether a part
    can carry media — which then has to pass the platform's media rules like
    the first post's does.

    ``first_comment`` is the comment this account would post on the first
    post. A ``COMMENTS`` thread refuses one: both are comments on the same
    post, queued on different delays, so the first comment could land in the
    middle of the thread. The author moves it into the thread instead.
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
    if capability.mode is ThreadMode.COMMENTS and (first_comment or "").strip():
        return (
            f"{name} adds a thread's later parts as comments, so it can't also post a first comment. "
            "Move the first comment into the thread as part 2, or remove it."
        )
    if 1 + len(parts) > capability.max_parts:
        return f"{name} takes at most {capability.max_parts} parts in a thread; this one has {1 + len(parts)}."

    limit = capability.part_max_length or social_account.char_limit
    noun = "comment" if capability.mode is ThreadMode.COMMENTS else "post"
    for number, (caption, assets) in enumerate(parts, start=2):
        if not (caption or "").strip() and not assets:
            return f"Part {number} of the thread is empty. Write something in it or remove it."
        length = social_account.caption_wire_length(counting(caption or "") if counting else caption or "")
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
    from apps.links.services import link_counting

    return thread_blocker(
        platform_post.social_account,
        [(caption, assets) for _part, caption, assets in thread_parts_for(platform_post, parts)],
        first_comment=_account_first_comment(platform_post, platform_post.effective_first_comment),
        # Links still to be shortened count at their short length.
        counting=link_counting(platform_post.post.workspace, post=platform_post.post),
    )


def _account_first_comment(platform_post, first_comment) -> str:
    """The first comment ``platform_post`` would actually post: none where the account can't comment."""
    return first_comment if platform_post.social_account.supports_first_comment() else ""


def thread_change_blocker(post, specs=None, asset_map=None, *, first_comment=None) -> str:
    """Why changing the thread or first comment would strand a scheduled row, or "".

    The thread counterpart of ``media_change_blocker``: only rows already
    ``scheduled`` count, the rest are checked when they are scheduled.
    ``specs`` (with ``asset_map``) replaces the parts and ``first_comment``
    the post's first comment; ``None`` keeps what is stored.
    """
    from apps.composer.models import PlatformPost

    children = post.platform_posts.filter(status=PlatformPost.Status.SCHEDULED).select_related("social_account")
    for child in children:
        if specs is None:
            parts = [(caption, assets) for _part, caption, assets in thread_parts_for(child)]
        else:
            parts = spec_thread_parts(child.social_account, specs, asset_map or {})
        comment = child.effective_first_comment
        if first_comment is not None and child.platform_specific_first_comment is None:
            comment = first_comment
        reason = thread_blocker(child.social_account, parts, first_comment=_account_first_comment(child, comment))
        if reason:
            return reason
    return ""


def thread_started(post) -> bool:
    """Whether any account has begun publishing ``post``'s thread.

    True once a first post with parts is live (its ``thread_status`` leaves
    ``none`` the moment the second part is queued, possibly hours before that
    part's row exists) or any part has left ``pending``. From then on the
    parts are the record of what is live and can't be replaced.
    """
    from apps.composer.models import PlatformPost, PlatformPostPart

    if post.platform_posts.exclude(thread_status=PlatformPost.ThreadStatus.NONE).exists():
        return True
    return (
        PlatformPostPart.objects.filter(platform_post__post=post)
        .exclude(status=PlatformPostPart.Status.PENDING)
        .exists()
    )


def thread_stopped_state(platform_post):
    """The ``PlatformPostPart`` a partial thread stopped at, or None.

    The first part in order that isn't live — the one a resume re-sends.
    """
    from apps.composer.models import PlatformPostPart

    if not platform_post.is_thread_partial:
        return None
    return (
        PlatformPostPart.objects.filter(platform_post=platform_post)
        .exclude(status=PlatformPostPart.Status.PUBLISHED)
        .order_by("part__position", "part__created_at")
        .first()
    )


# ---------------------------------------------------------------------------
# Hashtag sets and signatures
# ---------------------------------------------------------------------------

MAX_HASHTAGS_PER_SET = 30
RECENT_HASHTAG_POSTS = 50
RECENT_HASHTAG_LIMIT = 15


def _combining_marks() -> str:
    """A regex class body for the Basic Multilingual Plane's combining marks.

    ``\\w`` leaves marks out, so "#हिंदी" or a vowelled "#قَهوة" would stop at
    their first letter. With the marks (and the zero-width joiners Persian
    writes inside words) a hashtag runs to the end of the word.
    """
    ranges: list[str] = []
    start = prev = None
    for code in range(0x300, 0x10000):
        if unicodedata.category(chr(code)).startswith("M"):
            if start is None:
                start = code
            elif code != prev + 1:
                ranges.append(f"\\u{start:04x}-\\u{prev:04x}")
                start = code
            prev = code
    if start is not None:
        ranges.append(f"\\u{start:04x}-\\u{prev:04x}")
    return "".join(ranges) + "\\u200c\\u200d"


_TAG_CHAR = rf"[\w{_combining_marks()}]"
# At least one letter, so "#2024" or "#1" is not a hashtag.
_TAG_WORD = rf"{_TAG_CHAR}*[^\W\d_]{_TAG_CHAR}*"
_HASHTAG_TOKEN = re.compile(rf"^{_TAG_WORD}$")
# Not inside a word, an HTML entity ("&#123;") or a URL fragment ("/#top").
_HASHTAG_IN_TEXT = re.compile(rf"(?<![\w#&/])#({_TAG_WORD})")


def parse_hashtags(raw: str) -> list[str]:
    """Turn typed text ("coffee, #latteart  Coffee") into ``["#coffee", "#latteart"]``.

    Splits on whitespace and commas, drops the leading ``#`` and anything that
    is not a hashtag (punctuation, digits only), and keeps the first spelling of
    a tag that repeats in another case. Order is preserved.
    """
    seen: set[str] = set()
    tags: list[str] = []
    for token in re.split(r"[\s,]+", raw or ""):
        word = token.lstrip("#")
        if not _HASHTAG_TOKEN.match(word) or word.lower() in seen:
            continue
        seen.add(word.lower())
        tags.append(f"#{word}")
    return tags


def recent_hashtags(workspace_id, limit: int = RECENT_HASHTAG_LIMIT) -> list[str]:
    """Hashtags the workspace used lately, most recently used first.

    Read from the captions of its latest posts rather than stored, so there is
    nothing to keep in step as posts are edited or deleted.
    """
    from apps.composer.models import Post

    captions = (
        Post.objects.filter(workspace_id=workspace_id).order_by("-created_at").values_list("caption", flat=True)
    )[:RECENT_HASHTAG_POSTS]
    seen: set[str] = set()
    tags: list[str] = []
    for caption in captions:
        for word in _HASHTAG_IN_TEXT.findall(caption or ""):
            if word.lower() in seen:
                continue
            seen.add(word.lower())
            tags.append(f"#{word}")
            if len(tags) >= limit:
                return tags
    return tags


def default_signature(workspace, user):
    """The signature a new post starts with, or None.

    The user's own default wins over the workspace's, so a person can sign
    their own way inside a workspace that has a house sign-off.
    """
    from apps.composer.models import Signature

    if user is not None and user.is_authenticated:
        personal = Signature.objects.for_user(user).filter(is_default=True).first()
        if personal:
            return personal
    return Signature.objects.for_workspace(workspace.id).filter(is_default=True).first()


def with_signature(caption: str, signature) -> str:
    """``caption`` followed by the signature body, a blank line apart."""
    if signature is None:
        return caption
    return f"{caption.rstrip()}\n\n{signature.body}" if caption.strip() else f"\n\n{signature.body}"


# ---------------------------------------------------------------------------
# @mention suggestions
# ---------------------------------------------------------------------------

MENTION_CACHE_SECONDS = 60
MENTION_MAX_ACCOUNTS = 5
MENTION_QUERY_MAX = 64
_MENTION_TEXT_MAX = 100


def _mention_item(suggestion) -> dict[str, str]:
    """A suggestion as the composer's JSON, trimmed to what is safe to show.

    Names and handles come from strangers' profiles, so they are capped; the
    page renders them as text. An avatar is kept only when it is an https URL,
    so a profile cannot point the page at another scheme.
    """
    avatar = suggestion.avatar_url or ""
    item = {
        "handle": suggestion.handle[:_MENTION_TEXT_MAX],
        "name": (suggestion.name or "")[:_MENTION_TEXT_MAX],
        "avatar_url": avatar if avatar.startswith("https://") else "",
    }
    if suggestion.insert:
        item["insert"] = suggestion.insert[:_MENTION_TEXT_MAX]
    return item


def mention_suggestions(accounts: Iterable, query: str) -> list[dict[str, str]]:
    """Accounts to @mention that match ``query``, asked of each of ``accounts``.

    Every account is searched with its own credentials (its own instance, for
    Mastodon) and answers are cached briefly per account and query, so typing
    on does not re-ask. A platform that cannot be reached contributes nothing:
    the composer treats this as a nicety, so an error here is logged and never
    shown.
    """
    from django.core.cache import cache

    from apps.publisher.engine import _provider_and_access_token

    digest = hashlib.sha256(query.lower().encode()).hexdigest()[:24]
    suggestions: list[dict[str, str]] = []
    seen: set[str] = set()
    for account in list(accounts)[:MENTION_MAX_ACCOUNTS]:
        if not mention_lookup_for(account.platform):
            continue
        cache_key = f"composer:mentions:{account.id}:{digest}"
        found = cache.get(cache_key)
        if found is None:
            try:
                provider, access_token = _provider_and_access_token(account)
                found = [_mention_item(m) for m in provider.search_mentions(access_token, query)]
            except Exception:
                logger.info("Mention lookup failed for %s", account.id, exc_info=True)
                continue
            cache.set(cache_key, found, MENTION_CACHE_SECONDS)
        for item in found:
            if item["handle"].lower() not in seen:
                seen.add(item["handle"].lower())
                suggestions.append(item)
    return suggestions
