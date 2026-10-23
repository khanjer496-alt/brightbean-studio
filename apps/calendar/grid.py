"""Grid preview: how an account's profile grid will look, with scheduled posts among the published ones.

Published posts come first by when they went out; upcoming posts (scheduled,
approved, in review, drafts that have a suggested time) sit above them, the
furthest-future top-left, exactly where they will land once published. An
upcoming post can be traded for another: the two swap scheduled times, so the
grid changes and nothing else does. Who may re-time a post is the calendar's
rule (:func:`apps.calendar.services.reschedule_denial`); an approval covers a
post's content, not its time, so re-timing keeps its approved status.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, replace

from django.db import transaction
from django.db.models import Prefetch
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.composer.models import PlatformPost, PostMedia
from apps.social_accounts.models import SocialAccount

from . import services
from .models import QueueEntry

logger = logging.getLogger(__name__)

GRID_COLUMNS = 3
PUBLISHED_LIMIT = 30
UPCOMING_LIMIT = 60

# Statuses of a post that has a time and has not gone out yet.
_UPCOMING = (
    PlatformPost.Status.SCHEDULED,
    PlatformPost.Status.APPROVED,
    PlatformPost.Status.PENDING_REVIEW,
    PlatformPost.Status.PENDING_CLIENT,
    PlatformPost.Status.PUBLISHING,
)
_DRAFT = PlatformPost.Status.DRAFT
# What the grid can re-time: the calendar's draggable statuses, minus the ones a drop would schedule.
_MOVABLE = tuple(s for s in PlatformPost.RESCHEDULABLE_STATUSES if s not in services.IMPLICIT_SCHEDULE_STATUSES)


class GridError(Exception):
    """A refused change; ``status`` is the HTTP status it deserves."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class Tile:
    platform_post: PlatformPost
    state: str  # "published" | "scheduled" | "draft"
    moment: dt.datetime | None
    image_url: str
    caption: str
    movable: bool
    can_earlier: bool = False
    can_later: bool = False
    # Where a published post can be seen on the platform, when known.
    public_url: str = ""

    @property
    def id(self):
        return self.platform_post.id

    @property
    def moves(self) -> list[tuple[str, str, bool]]:
        """The keyboard-friendly alternative to dragging: (direction, button label, enabled).

        Later first: the grid runs from the furthest-future post (top-left) forward, so a later
        time moves a tile back towards the start and an earlier one moves it on.
        """
        return [("later", "Later", self.can_later), ("earlier", "Earlier", self.can_earlier)]

    @property
    def status_label(self) -> str:
        return self.platform_post.get_status_display()


@dataclass(frozen=True)
class Grid:
    tiles: list[Tile]
    unscheduled: list[Tile]
    columns: int = GRID_COLUMNS

    @property
    def upcoming_count(self) -> int:
        return sum(1 for tile in self.tiles if tile.state != "published")


def grid_platforms() -> list[str]:
    """Platforms whose provider says a grid preview makes sense."""
    from providers import PROVIDER_REGISTRY

    return [name for name, provider in PROVIDER_REGISTRY.items() if provider.supports_grid_preview]


def grid_accounts(workspace):
    return SocialAccount.objects.filter(workspace=workspace, platform__in=grid_platforms()).order_by(
        "account_name", "id"
    )


def supports_grid(account: SocialAccount) -> bool:
    return account.platform in grid_platforms()


def _media_prefetch() -> Prefetch:
    return Prefetch(
        "post__media_attachments",
        queryset=PostMedia.objects.select_related("media_asset").order_by("position", "id"),
    )


def _tile(pp: PlatformPost, state: str, moment, user, membership, now) -> Tile:
    attachments = list(pp.post.media_attachments.all())
    image = attachments[0].media_asset.preview_url if attachments else ""
    caption = (pp.platform_specific_caption or pp.post.caption or pp.post.title or "").strip()
    movable = (
        state == "scheduled"
        and pp.status in _MOVABLE
        and pp.real_time is not None
        and pp.real_time > now
        and services.reschedule_denial(pp, user, membership) is None
    )
    public_url = pp.public_url() if state == "published" else ""
    return Tile(pp, state, moment, image, caption, movable, public_url=public_url)


def build_grid(account: SocialAccount, user, membership, now: dt.datetime | None = None) -> Grid:
    now = now or timezone.now()
    base = (
        PlatformPost.objects.filter(social_account=account)
        .select_related("post", "social_account")
        .prefetch_related(_media_prefetch())
        .annotate(
            real_time=Coalesce("scheduled_at", "post__scheduled_at"),
            shown_time=Coalesce("scheduled_at", "post__scheduled_at", "post__proposed_publish_at"),
        )
    )
    upcoming_rows = list(
        base.filter(status__in=[*_UPCOMING, _DRAFT], shown_time__isnull=False).order_by("-shown_time", "-id")[
            :UPCOMING_LIMIT
        ]
    )
    published_rows = list(
        base.filter(status=PlatformPost.Status.PUBLISHED, published_at__isnull=False).order_by("-published_at", "-id")[
            :PUBLISHED_LIMIT
        ]
    )
    unscheduled_rows = list(base.filter(status=_DRAFT, shown_time__isnull=True).order_by("-updated_at", "-id")[:12])

    tiles = [
        _tile(pp, "draft" if pp.status == _DRAFT else "scheduled", pp.shown_time, user, membership, now)
        for pp in upcoming_rows
    ]
    tiles += [_tile(pp, "published", pp.published_at, user, membership, now) for pp in published_rows]
    # Neighbours are the movable tiles next to each other in time; the buttons are off at either end.
    movable_ids = [t.id for t in tiles if t.movable]
    flagged = []
    for tile in tiles:
        if tile.movable:
            index = movable_ids.index(tile.id)
            tile = replace(tile, can_later=index > 0, can_earlier=index < len(movable_ids) - 1)
        flagged.append(tile)
    unscheduled = [_tile(pp, "draft", None, user, membership, now) for pp in unscheduled_rows]
    return Grid(flagged, unscheduled)


def _lock(account: SocialAccount, ids) -> dict:
    rows = (
        PlatformPost.objects.select_for_update(of=("self",))
        .select_related("post")
        .filter(social_account=account, id__in=ids)
        .annotate(real_time=Coalesce("scheduled_at", "post__scheduled_at"))
        .order_by("id")
    )
    return {row.id: row for row in rows}


def _check_movable(pp: PlatformPost, user, membership, now) -> None:
    denial = services.reschedule_denial(pp, user, membership)
    if denial:
        raise GridError(*denial)
    if pp.status not in _MOVABLE or pp.real_time is None:
        raise GridError(400, "Only scheduled posts can swap places.")
    if pp.real_time <= now:
        raise GridError(409, "That post is already due to go out, so it can't move.")


def swap_times(account: SocialAccount, first_id, second_id, user, membership, now=None):
    """Trade the scheduled times of two upcoming posts on ``account``; returns the two rows."""
    now = now or timezone.now()
    if first_id == second_id:
        raise GridError(400, "Pick two different posts.")
    with transaction.atomic():
        rows = _lock(account, [first_id, second_id])
        if len(rows) != 2:
            raise GridError(404, "That post isn't on this account's grid any more.")
        first, second = rows[first_id], rows[second_id]
        for pp in (first, second):
            _check_movable(pp, user, membership, now)
        first_at, second_at = first.real_time, second.real_time
        for pp, new_time in ((first, second_at), (second, first_at)):
            pp.scheduled_at = new_time
            pp.save(update_fields=["scheduled_at", "updated_at"])
            # Same bookkeeping as a calendar drop: queue mirror and the post's earliest time.
            QueueEntry.objects.filter(post=pp.post, queue__social_account=account).update(
                assigned_slot_datetime=new_time
            )
        from apps.composer.services import sync_post_scheduled_at

        for pp in (first, second):
            sync_post_scheduled_at(pp.post)
    for pp in (first, second):
        if pp.post.recurrence_source_id is None:
            services.refresh_series(pp.post)
    return first, second


def move(account: SocialAccount, platform_post_id, direction: str, user, membership, now=None):
    """Swap a post with its neighbour in time: ``earlier`` or ``later``; returns ``(post, neighbour)``."""
    if direction not in ("earlier", "later"):
        raise GridError(400, "direction must be earlier or later.")
    now = now or timezone.now()
    candidates = (
        PlatformPost.objects.filter(social_account=account, status__in=_MOVABLE)
        .select_related("post")
        .annotate(real_time=Coalesce("scheduled_at", "post__scheduled_at"))
        .filter(real_time__gt=now)
        .order_by("real_time", "id")
    )
    # The neighbour is the next post this person may move too, as the buttons on the grid assume.
    ordered = [pp.id for pp in candidates if services.reschedule_denial(pp, user, membership) is None]
    if platform_post_id not in ordered:
        raise GridError(404, "That post isn't a scheduled post on this grid.")
    index = ordered.index(platform_post_id) + (-1 if direction == "earlier" else 1)
    if not 0 <= index < len(ordered):
        raise GridError(409, f"There is no scheduled post {direction} than this one to swap with.")
    return swap_times(account, platform_post_id, ordered[index], user, membership, now)
