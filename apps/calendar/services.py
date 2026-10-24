"""Queue scheduling services for the Content Calendar (F-2.3)."""

import zoneinfo
from datetime import datetime, time, timedelta

from django.db import transaction
from django.db.models import F
from django.db.models.functions import Coalesce
from django.utils import timezone

from .models import PostingSlot, Queue, QueueEntry

# Statuses a drop promotes to "scheduled" (an implicit schedule / retry) rather than only moving in time.
IMPLICIT_SCHEDULE_STATUSES = ("draft", "failed")


def reschedule_denial(platform_post, user, membership) -> tuple[int, str] | None:
    """Why ``user`` may not move ``platform_post`` in time, as ``(http_status, message)``; None when they may.

    The one rule behind the calendar's drag-and-drop and the Instagram grid: only draggable
    statuses move, the author or an editor of others' posts may move it, and a draft or failed row
    (which the move would schedule) also needs the publish privilege. Approved and in-review posts
    keep their status when re-timed, because an approval covers the content, not the time.
    """
    if not platform_post.is_reschedulable:
        return 400, "Post cannot be rescheduled in its current status."
    perms = membership.effective_permissions if membership else {}
    if not (platform_post.post.author_id == user.id or perms.get("edit_others_posts", False)):
        return 403, "Permission denied."
    if platform_post.status in IMPLICIT_SCHEDULE_STATUSES and not perms.get("publish_directly", False):
        return 403, "You do not have permission to schedule this post."
    return None


# Default posting slots created automatically for newly connected channels.
DEFAULT_POSTING_SLOTS = {
    0: [time(9, 24), time(10, 10), time(11, 26), time(12, 42)],  # Monday
    1: [time(9, 55), time(10, 41), time(11, 57), time(12, 13)],  # Tuesday
    2: [time(9, 30), time(10, 17), time(11, 32), time(12, 41)],  # Wednesday
    3: [time(9, 38), time(10, 52)],  # Thursday
}


def create_default_queue_and_slots(social_account):
    """Create a default Queue and PostingSlots for a newly connected social account.

    Skips creation if the account already has a queue (e.g. on re-connection).
    """
    if Queue.objects.filter(social_account=social_account).exists():
        return None

    queue = Queue.objects.create(
        workspace=social_account.workspace,
        name=f"{social_account.account_name or social_account.account_handle} Queue",
        social_account=social_account,
    )

    slots = []
    for day, times in DEFAULT_POSTING_SLOTS.items():
        for t in times:
            slots.append(PostingSlot(social_account=social_account, day_of_week=day, time=t))
    PostingSlot.objects.bulk_create(slots, ignore_conflicts=True)

    return queue


def _next_slot_datetimes(social_account, after_dt, count=30):
    """Compute the next `count` PostingSlot datetimes for a social account.

    Starting from `after_dt`, walks forward through the week to find
    upcoming slot times based on the account's PostingSlot configuration.

    Slot times are naive wall-clock times in the account's workspace timezone
    (see ``PostingSlot.time``), so they are resolved in that zone regardless of
    the tzinfo carried by ``after_dt`` — the caller's baseline only sets the
    "not before" instant. ``after_dt`` is always timezone-aware (callers pass
    ``timezone.now()`` or a tz-aware floor).
    """
    slots = PostingSlot.objects.filter(social_account=social_account, is_active=True).order_by("day_of_week", "time")
    if not slots.exists():
        return []

    ws_tz = zoneinfo.ZoneInfo(social_account.workspace.effective_timezone or "UTC")
    after_local = after_dt.astimezone(ws_tz)

    slot_list = list(slots)
    results = []
    current_date = after_local.date()

    # Walk up to 60 days forward to find enough slots
    for day_offset in range(60):
        check_date = current_date + timedelta(days=day_offset)
        weekday = check_date.weekday()  # 0=Monday

        for slot in slot_list:
            if slot.day_of_week != weekday:
                continue

            # Interpret the slot's wall-clock time in the workspace zone (DST
            # offsets resolve per-date), then compare as instants (both aware).
            slot_dt = datetime.combine(check_date, slot.time).replace(tzinfo=ws_tz)
            if slot_dt <= after_dt:
                continue

            results.append(slot_dt)
            if len(results) >= count:
                return results

    return results


# ---------------------------------------------------------------------------
# Stable slot-occupancy model
#
# A queue's "slots" are the upcoming PostingSlot datetimes for its channel; each
# is either OCCUPIED (a non-published post on the channel already holds that
# instant) or a GAP. Operations are LOCAL — they fill or vacate a single slot
# and never recompute unrelated entries — so an entry's time stays stable until
# that entry is explicitly moved. Occupancy is keyed off PlatformPost.scheduled_at
# (the publisher's source of truth), not the queue rows, so a manually-scheduled
# post or a recurrence clone on the same channel also reserves its instant — no
# double-booking. PROTECTED_STATUSES (published/publishing) are history and never
# occupy a future candidate or get moved.
# ---------------------------------------------------------------------------


class QueueFullError(Exception):
    """No open posting slot exists within the scheduling lookahead horizon."""


def _occupied_datetimes(social_account, *, exclude_pp_ids=()):
    """Future slot instants already claimed on this channel.

    Non-published/-publishing PlatformPosts with a future ``scheduled_at`` on
    ``social_account``. ``exclude_pp_ids`` drops a post's own row so it never
    blocks itself while being re-slotted.
    """
    from apps.composer.models import PlatformPost

    qs = (
        PlatformPost.objects.filter(
            social_account=social_account,
            scheduled_at__isnull=False,
            scheduled_at__gt=timezone.now(),
        )
        .exclude(status__in=PlatformPost.PROTECTED_STATUSES)
        .exclude(id__in=list(exclude_pp_ids))
    )
    return set(qs.values_list("scheduled_at", flat=True))


def _next_available_slot(social_account, *, exclude_pp_ids=(), after=None):
    """First upcoming PostingSlot datetime not already occupied, or ``None``.

    ``None`` means every slot within the lookahead horizon (``_next_slot_datetimes``
    caps at 60 days) is taken — the queue is full.
    """
    after = after or timezone.now()
    occupied = _occupied_datetimes(social_account, exclude_pp_ids=exclude_pp_ids)
    # Among the first ``len(occupied) + 1`` distinct slots at most ``len(occupied)``
    # can be taken, so one is guaranteed free if any free slot exists at all.
    candidates = _next_slot_datetimes(social_account, after, count=len(occupied) + 1)
    for slot_dt in candidates:
        if slot_dt not in occupied:
            return slot_dt
    return None


def next_available_slots(social_account, count=1, *, after=None):
    """The next ``count`` open PostingSlot datetimes on ``social_account``, soonest first.

    Same occupancy rule queues use (see ``_next_available_slot``): a slot is
    taken when a non-published post on the channel is scheduled at that
    instant. Fewer than ``count`` come back when the 60-day lookahead runs out.
    """
    after = after or timezone.now()
    occupied = _occupied_datetimes(social_account)
    candidates = _next_slot_datetimes(social_account, after, count=len(occupied) + count)
    return [slot_dt for slot_dt in candidates if slot_dt not in occupied][:count]


def _platform_post_for(post, social_account):
    """The post's PlatformPost child on this channel (or ``None``)."""
    return post.platform_posts.filter(social_account=social_account).first()


def _is_protected(pp):
    from apps.composer.models import PlatformPost

    return pp is not None and pp.status in PlatformPost.PROTECTED_STATUSES


def _lock_channel(queue):
    """Serialize slot writes for a channel within the current transaction.

    Occupancy is computed from ``PlatformPost`` rows (the publisher's truth),
    which a per-queue ``select_for_update`` on QueueEntry rows does not cover —
    two concurrent ops on *different* queues of the same channel would otherwise
    read the same free slot and double-book. Locking the ``SocialAccount`` row
    makes every slot op on a channel mutually exclusive.
    """
    from apps.social_accounts.models import SocialAccount

    list(SocialAccount.objects.select_for_update().filter(id=queue.social_account_id))


def _ensure_entry(post, queue):
    """Return the post's QueueEntry, creating it (appended) if absent."""
    from django.db.models import Max

    entry = queue.entries.filter(post=post).first()
    if entry is None:
        max_pos = queue.entries.aggregate(m=Max("position"))["m"]
        entry = QueueEntry.objects.create(queue=queue, post=post, position=(max_pos or 0) + 1)
    return entry


def _write_slot(entry, pp, slot_dt):
    """Persist one slot assignment to the QueueEntry and its PlatformPost."""
    from apps.composer.services import sync_post_scheduled_at

    entry.assigned_slot_datetime = slot_dt
    entry.save(update_fields=["assigned_slot_datetime"])
    if pp is not None:
        pp.scheduled_at = slot_dt
        pp.save(update_fields=["scheduled_at", "updated_at"])
        sync_post_scheduled_at(pp.post)


def add_post_next_available(post, queue):
    """Place ``post`` into this queue's first open slot (upsert-aware).

    Comment §1 (Create · Next Available) and §4 (Edit · Next Available): if the
    post is already queued, its own slot is vacated first and it is re-slotted to
    the next gap; existing entries are never disturbed. Raises ``QueueFullError`` when
    no slot is free within the horizon.
    """
    from django.db import transaction

    with transaction.atomic():
        _lock_channel(queue)

        pp = _platform_post_for(post, queue.social_account)
        # A published/publishing post's schedule is history — never re-slot it
        # (mirrors the guard in prioritize/reorder; protects the reslot endpoint).
        if _is_protected(pp):
            return queue.entries.filter(post=post).first()
        exclude = [pp.id] if pp is not None else []
        slot_dt = _next_available_slot(queue.social_account, exclude_pp_ids=exclude)
        if slot_dt is None:
            raise QueueFullError(f"Queue {queue.id} has no open slot within the scheduling horizon.")

        entry = _ensure_entry(post, queue)
        _write_slot(entry, pp, slot_dt)
        return entry


def prioritize(post, queue):
    """Place ``post`` at the queue's earliest slot, laddering others up by one.

    Comment §2: if the earliest upcoming slot is free, ``post`` takes it and
    nothing moves. Otherwise every other occupied queue entry shifts one slot
    later (``[k]→[k+1]``, preserving the gap pattern) before ``post`` takes
    slot ``[0]``. Upsert-aware. Raises ``QueueFullError`` past the horizon.
    """
    from django.db import transaction

    with transaction.atomic():
        _lock_channel(queue)

        now = timezone.now()
        pp = _platform_post_for(post, queue.social_account)

        # This queue's movable entries (exclude `post`, exclude protected), each
        # keyed on the ``PlatformPost.scheduled_at`` the publisher fires on — NOT
        # ``QueueEntry.assigned_slot_datetime``, which a manual calendar drag via
        # ``reschedule_post`` can leave stale and divergent (then the ladder would
        # mis-place the post or double-book a slot another post really holds).
        movable = []  # (entry, pp, current_slot_dt)
        for e in queue.entries.exclude(post=post).select_related("post"):
            p = _platform_post_for(e.post, queue.social_account)
            if p is None or _is_protected(p) or p.scheduled_at is None or p.scheduled_at <= now:
                continue
            movable.append((e, p, p.scheduled_at))
        movable_dts = {dt for _, _, dt in movable}

        candidates = _next_slot_datetimes(queue.social_account, now, count=len(movable) + 2)
        if not candidates:
            raise QueueFullError(f"Queue {queue.id} has no posting slots within the horizon.")

        entry = _ensure_entry(post, queue)
        target = candidates[0]

        # Slot 0 is free among the queue's own entries.
        if target not in movable_dts:
            # If a foreign/fixed post (manual schedule, clone, other queue) holds
            # slot 0, we cannot preempt it — degrade to the next genuine gap.
            occupied = _occupied_datetimes(queue.social_account, exclude_pp_ids=[pp.id] if pp else [])
            if target in occupied:
                free = _next_available_slot(queue.social_account, exclude_pp_ids=[pp.id] if pp else [])
                if free is None:
                    raise QueueFullError(f"Queue {queue.id} has no open slot within the scheduling horizon.")
                _write_slot(entry, pp, free)
                return entry
            _write_slot(entry, pp, target)
            return entry

        # Slot 0 taken by a queue entry: ladder every movable entry up to its
        # next free slot. Each mover lands on the earliest candidate after its
        # current index that is neither held by a post we can't move (foreign /
        # manual / out-of-horizon) nor already claimed by another mover — so the
        # ladder never double-books and never runs off the lookahead horizon.
        index_of = {dt: i for i, dt in enumerate(candidates)}
        movers = [(index_of[dt], e, p) for e, p, dt in movable if dt in index_of]
        mover_old_dts = {dt for _, _, dt in movable if dt in index_of}
        # Datetimes that must stay free: everything occupied on this channel
        # except the movers we are about to relocate (their slots are vacated).
        avoid_dts = (
            _occupied_datetimes(queue.social_account, exclude_pp_ids=[pp.id] if pp is not None else []) - mover_old_dts
        )

        assigned_idx = {0}  # slot 0 is reserved for the prioritized post
        # Descending so a destination is always vacated before it is written.
        for cur_idx, e, p in sorted(movers, key=lambda t: t[0], reverse=True):
            j = cur_idx + 1
            while True:
                if j >= len(candidates):
                    candidates = _next_slot_datetimes(queue.social_account, now, count=j + 2)
                    if j >= len(candidates):
                        raise QueueFullError(f"Queue {queue.id} has no open slot within the scheduling horizon.")
                if j not in assigned_idx and candidates[j] not in avoid_dts:
                    break
                j += 1
            assigned_idx.add(j)
            _write_slot(e, p, candidates[j])
        _write_slot(entry, pp, candidates[0])
        return entry


def remove_from_queue(entry):
    """Remove a single entry, leaving a gap; neighbours are untouched.

    Comment §3: delete the QueueEntry and clear the matching PlatformPost's
    schedule. The child is transitioned ``scheduled→draft`` (which nulls
    ``scheduled_at``) rather than nulled directly, so it can't become instantly
    due via the publisher's ``Coalesce(scheduled_at, post__scheduled_at)``
    fallback while the parent aggregate still points at a past time.
    """
    from django.db import transaction

    from apps.composer.services import sync_post_scheduled_at, transition_platform_post

    with transaction.atomic():
        post = entry.post
        queue = entry.queue
        _lock_channel(queue)
        pp = _platform_post_for(post, queue.social_account)
        # Clear the schedule for any non-protected child (scheduled OR failed),
        # not just 'scheduled', so a removed failed post stops rendering on the
        # calendar at its past attempt time. Published/publishing stay (history).
        if pp is not None and not _is_protected(pp) and pp.can_transition_to("draft"):
            transition_platform_post(pp, "draft")
        entry.delete()
        sync_post_scheduled_at(post)


def reslot_to_next_available(entry):
    """Free this entry's slot and move it to the queue's next gap."""
    return add_post_next_available(entry.post, entry.queue)


def add_to_queue(post, queue, priority=False):
    """Add a post to a queue (back-compat dispatcher).

    Routes to the stable-slot ops: ``prioritize`` (top of queue) or
    ``add_post_next_available`` (first open gap). Kept so existing callers and
    the composer's ``add_to_queue`` / ``add_to_queue_priority`` actions only
    need their resolved service swapped, not their wiring.
    """
    if priority:
        return prioritize(post, queue)
    return add_post_next_available(post, queue)


def reorder_queue(queue, ordered_entry_ids):
    """Reassign the queue's occupied slot times to entries in a new order.

    Drag-reorder under the slot model: the SET of occupied datetimes is
    preserved (gaps stay put); only which post sits in which slot changes. The
    movable, slotted entries named in ``ordered_entry_ids`` are matched — in that
    visual order — to their datetimes sorted ascending. Protected
    (published/publishing) entries are immovable and skipped.
    """
    from django.db import transaction

    with transaction.atomic():
        _lock_channel(queue)

        ordered = []
        for eid in ordered_entry_ids:
            e = queue.entries.filter(id=eid).select_related("post").first()
            if e is None:
                continue
            p = _platform_post_for(e.post, queue.social_account)
            if _is_protected(p):
                continue
            ordered.append((e, p))

        # Redistribute the slot instants already held — keyed on the publisher's
        # ``PlatformPost.scheduled_at`` (not ``assigned_slot_datetime``, which a
        # manual drag can leave stale) — among the entries that hold one. Never
        # invent or drop a slot during a reorder.
        slotted = [(e, p) for e, p in ordered if p is not None and p.scheduled_at is not None]
        slots = sorted(p.scheduled_at for _, p in slotted)
        for idx, (e, p) in enumerate(slotted):
            _write_slot(e, p, slots[idx])

        # Keep ``position`` as a stable visual tie-break in the dragged order.
        for idx, (e, _p) in enumerate(ordered):
            if e.position != idx:
                QueueEntry.objects.filter(id=e.id).update(position=idx)


def repair_future_published_scheduled_at(*, workspace_id=None, apply=True):
    """Reset ``scheduled_at`` for published posts dragged into the future.

    Before the ``assign_queue_slots`` guard landed, re-running a queue (adding or
    reordering a post) re-slotted *every* entry — including already-published
    ones — pushing their ``scheduled_at`` onto a future posting slot. Because the
    calendar places chips by ``scheduled_at``, those posts then showed as
    "published" up to a week ahead.

    A correctly-published post always has ``scheduled_at <= published_at`` (you
    schedule, then it fires at or after that instant). ``scheduled_at >
    published_at`` is therefore an unambiguous signature of this corruption, so
    the repair targets exactly those rows and resets ``scheduled_at`` to the true
    ``published_at``. Idempotent and safe to re-run: once reset, a row no longer
    matches the filter.

    The same bad future slot was also written to the matching
    ``QueueEntry.assigned_slot_datetime`` (the queue the bug walked, where
    ``queue.social_account == platform_post.social_account``). ``queue_detail``
    renders that field and the published-status guard stops ``assign_queue_slots``
    from ever recomputing a published entry, so the repair snaps that stale queue
    timestamp back to ``published_at`` too (same ``> published_at`` signature).

    Returns a summary dict ``{"rows": [...], "platform_post_count": int,
    "post_count": int, "queue_entry_count": int, "applied": bool}`` where each
    row is a plain dict describing one affected ``PlatformPost`` (for dry-run
    reporting).
    """
    from django.db.models import F

    from apps.composer.models import PlatformPost, Post
    from apps.composer.services import sync_post_scheduled_at

    affected = (
        PlatformPost.objects.filter(
            status=PlatformPost.Status.PUBLISHED,
            published_at__isnull=False,
            scheduled_at__isnull=False,
            scheduled_at__gt=F("published_at"),
        )
        .select_related("social_account", "post")
        .order_by("scheduled_at")
    )
    if workspace_id is not None:
        affected = affected.filter(post__workspace_id=workspace_id)

    rows = []
    post_ids = set()
    queue_targets = []
    for pp in affected:
        rows.append(
            {
                "platform_post_id": str(pp.id),
                "post_id": str(pp.post_id),
                "workspace_id": str(pp.post.workspace_id),
                "platform": pp.social_account.platform,
                "account": pp.social_account.account_name or pp.social_account.account_handle,
                "old_scheduled_at": pp.scheduled_at,
                "new_scheduled_at": pp.published_at,
            }
        )
        post_ids.add(pp.post_id)
        queue_targets.append((pp.post_id, pp.social_account_id, pp.published_at))

    # The bug stamped the same future slot onto the matching QueueEntry
    # (queue.social_account == pp.social_account); queue_detail renders it, and
    # the published-status guard now keeps assign_queue_slots from ever
    # recomputing a published entry. Snap those stale timestamps back too — same
    # ``> published_at`` signature, so a correctly null/past entry is left alone.
    queue_entry_targets = [
        (
            published_at,
            QueueEntry.objects.filter(
                post_id=post_id,
                queue__social_account_id=social_account_id,
                assigned_slot_datetime__gt=published_at,
            ),
        )
        for post_id, social_account_id, published_at in queue_targets
    ]
    queue_entry_count = 0

    if apply and rows:
        from django.db import transaction

        with transaction.atomic():
            posts = list(Post.objects.filter(id__in=post_ids))
            # Snapping a published child back to its past ``published_at`` lowers
            # the parent ``Post.scheduled_at`` aggregate (min-of-children) into
            # the past. A SCHEDULED sibling with ``scheduled_at=NULL`` resolves
            # its due time through the publisher's
            # ``Coalesce(scheduled_at, post__scheduled_at)`` fallback, so a
            # backward parent move would make that sibling instantly due and
            # publish it early. Pin such siblings to their current effective time
            # *before* lowering the parent, so the repair never drags a pending
            # post's schedule into the past.
            for post in posts:
                if post.scheduled_at is not None:
                    post.platform_posts.filter(
                        status=PlatformPost.Status.SCHEDULED,
                        scheduled_at__isnull=True,
                    ).update(scheduled_at=post.scheduled_at)
            # Snap each corrupt published child back to its real publish instant,
            # then recompute the parent aggregate so listings and Coalesce
            # fallbacks line up again.
            affected.update(scheduled_at=F("published_at"))
            for published_at, stale_entries in queue_entry_targets:
                queue_entry_count += stale_entries.update(assigned_slot_datetime=published_at)
            for post in posts:
                sync_post_scheduled_at(post)
    else:
        # Dry run: report how many stale QueueEntry rows would be reset.
        for _published_at, stale_entries in queue_entry_targets:
            queue_entry_count += stale_entries.count()

    return {
        "rows": rows,
        "platform_post_count": len(rows),
        "post_count": len(post_ids),
        "queue_entry_count": queue_entry_count,
        "applied": bool(apply and rows),
    }


# ---------------------------------------------------------------------------
# Recurring posts
# ---------------------------------------------------------------------------

WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

# Upper bound on "every N days/weeks/months"; anything larger is a typo.
RECURRENCE_MAX_INTERVAL = 99


def clean_recurrence(frequency, *, interval=1, weekdays=None, end_date=None, count=None, start=None):
    """Validate a repeat setting and return it as ``RecurrenceRule`` fields.

    ``start`` is the series' first post time; a weekly rule without weekdays
    takes its weekday (in the workspace timezone), and an end date before it
    is refused. Raises ``ValueError`` with a message fit to show the user.
    """
    from .models import RecurrenceRule

    if frequency not in RecurrenceRule.Frequency.values:
        raise ValueError("Choose how often the post repeats: daily, weekly or monthly.")
    try:
        interval = int(interval)
    except (TypeError, ValueError):
        raise ValueError("The repeat interval must be a whole number.") from None
    if not 1 <= interval <= RECURRENCE_MAX_INTERVAL:
        raise ValueError(f"The repeat interval must be between 1 and {RECURRENCE_MAX_INTERVAL}.")

    days = []
    if frequency == RecurrenceRule.Frequency.WEEKLY:
        try:
            days = sorted({int(w) for w in weekdays or []})
        except (TypeError, ValueError):
            raise ValueError("Weekdays must be numbers from 0 (Monday) to 6 (Sunday).") from None
        if any(not 0 <= w <= 6 for w in days):
            raise ValueError("Weekdays must be numbers from 0 (Monday) to 6 (Sunday).")
        if not days and start is not None:
            days = [start.weekday()]

    if end_date is not None and count is not None:
        raise ValueError("A series can end on a date or after a number of posts, not both.")
    if count is not None:
        try:
            count = int(count)
        except (TypeError, ValueError):
            raise ValueError("The number of posts must be a whole number.") from None
        if count < 2:
            raise ValueError("A repeating post needs at least 2 posts in the series.")
    if end_date is not None and start is not None and end_date < start.date():
        raise ValueError("The series can't end before its first post.")

    return {"frequency": frequency, "interval": interval, "weekdays": days, "end_date": end_date, "count": count}


def recurrence_summary(frequency, *, interval=1, weekdays=None, end_date=None, count=None):
    """Describe a repeat setting, e.g. "Every Tuesday and Thursday until 31 Dec".

    Mirrors ``repeatPanel().summary`` in the composer, which words the same
    setting live as it is edited.
    """
    from django.utils.dateformat import format as date_format
    from django.utils.text import get_text_list

    interval = interval or 1
    units = {"daily": "day", "weekly": "week", "monthly": "month"}
    unit = units.get(frequency, "")
    if frequency == "weekly" and weekdays:
        names = get_text_list([WEEKDAY_NAMES[w] for w in sorted(weekdays)], "and")
        text = f"Every {names}" if interval == 1 else f"Every {interval} weeks on {names}"
    else:
        text = f"Every {unit}" if interval == 1 else f"Every {interval} {unit}s"

    if end_date is not None:
        fmt = "j M" if end_date.year == timezone.localdate().year else "j M Y"
        text += f" until {date_format(end_date, fmt)}"
    elif count:
        text += f", {count} times"
    return text


def recurrence_initial(post):
    """The composer's Repeat control state for ``post`` — never repeating if none."""
    rule = None
    if post is not None and post.pk:
        rule = post.series_rule if post.recurrence_source_id is None else None
    if rule is None or not rule.is_active:
        return {"frequency": "never", "interval": 1, "weekdays": [], "end": "never", "end_date": "", "count": None}
    return {
        "frequency": rule.frequency,
        "interval": rule.interval,
        "weekdays": list(rule.weekdays or []),
        "end": "on" if rule.end_date else ("after" if rule.count else "never"),
        "end_date": rule.end_date.isoformat() if rule.end_date else "",
        "count": rule.count,
    }


def remove_upcoming_occurrences(post):
    """Delete the copies ``post``'s series has queued but not yet published.

    Copies already publishing or published are history and stay. Returns the
    number of copies removed.
    """
    from apps.composer.models import PlatformPost, Post

    upcoming_ids = list(
        Post.objects.filter(recurrence_source=post, scheduled_at__gt=timezone.now())
        .exclude(platform_posts__status__in=PlatformPost.PROTECTED_STATUSES)
        .values_list("id", flat=True)
        .distinct()
    )
    if not upcoming_ids:
        return 0
    _total, by_model = Post.objects.filter(id__in=upcoming_ids).delete()
    return by_model.get(Post._meta.label, 0)


def save_recurrence(post, *, frequency, interval=1, weekdays=None, end_date=None, count=None, refresh=False):
    """Make ``post`` the first of a repeating series, or update its series.

    When the rule changes (or ``refresh`` says the first post moved), the
    upcoming copies made under the old rule are replaced; then the copies the
    lookahead window needs are generated straight away rather than at the next
    hourly sweep. A post still waiting for approval has only a proposed time:
    its rule is kept, and copies start once it is approved and scheduled.
    Raises ``ValueError`` for an invalid setting.
    """
    from django.db import transaction

    from .models import RecurrenceRule
    from .tasks import generate_occurrences

    start = post.scheduled_at or post.proposed_publish_at
    if not start:
        raise ValueError("Schedule the post before making it repeat.")
    ws_tz = zoneinfo.ZoneInfo(post.workspace.effective_timezone or "UTC")
    fields = clean_recurrence(
        frequency,
        interval=interval,
        weekdays=weekdays,
        end_date=end_date,
        count=count,
        start=start.astimezone(ws_tz),
    )

    with transaction.atomic():
        rule = RecurrenceRule.objects.select_for_update().filter(post=post).first()
        changed = refresh or rule is None or not rule.is_active
        if rule is None:
            rule = RecurrenceRule(post=post)
        for name, value in fields.items():
            if getattr(rule, name) != value:
                changed = True
                setattr(rule, name, value)
        rule.is_active = True
        rule.save()
        if changed:
            remove_upcoming_occurrences(post)
    generate_occurrences(rule)
    return rule


def stop_recurrence(post):
    """End ``post``'s series: no more copies, and the upcoming ones are removed.

    Returns the number of upcoming copies removed; 0 when it wasn't repeating.
    """
    from .models import RecurrenceRule

    updated = RecurrenceRule.objects.filter(post=post, is_active=True).update(is_active=False)
    if not updated:
        return 0
    return remove_upcoming_occurrences(post)


def refresh_series(post):
    """Regenerate ``post``'s upcoming copies after its first post moved.

    Copies are placed relative to the first post, so moving it (a calendar
    drag) replaces the unpublished copies with ones at the new time. Returns
    the number of copies generated; 0 when ``post`` doesn't start a series.
    """
    from django.db import transaction

    from .models import RecurrenceRule
    from .tasks import generate_occurrences

    with transaction.atomic():
        rule = RecurrenceRule.objects.select_for_update().filter(post=post, is_active=True).first()
        if rule is None:
            return 0
        remove_upcoming_occurrences(post)
        return generate_occurrences(rule)


# ---------------------------------------------------------------------------
# Suggested posting slots (best time to post)
# ---------------------------------------------------------------------------

SUGGESTED_SLOTS_DEFAULT = 5
SUGGESTED_SLOTS_MAX = 14
SLOT_MODES = ("replace", "add")


def suggest_slots(social_account, count=SUGGESTED_SLOTS_DEFAULT):
    """The account's best ``count`` weekday/hour slots and the grid they came from.

    Returns ``(best_times, suggestions)``: ``best_times`` says whether the
    scores come from the account's own posts or industry defaults, and
    ``suggestions`` are ``BestTime`` rows sorted by day and hour.
    """
    from apps.analytics.best_times import get_best_times

    count = max(1, min(int(count), SUGGESTED_SLOTS_MAX))
    best = get_best_times(social_account)
    return best, sorted(best.top(count), key=lambda s: (s.weekday, s.hour))


def plan_slot_changes(social_account, suggestions, mode="replace"):
    """What applying ``suggestions`` would do: ``(to_add, to_remove, kept)`` as lists of ``(weekday, time)``/slots.

    ``replace`` swaps every posting slot for the suggestions; ``add`` keeps the
    existing slots and only adds the missing ones. Nothing is written.
    """
    if mode not in SLOT_MODES:
        raise ValueError(f"mode must be one of {', '.join(SLOT_MODES)}.")
    wanted = {(s.weekday, time(s.hour, 0)) for s in suggestions}
    existing = list(PostingSlot.objects.filter(social_account=social_account))
    existing_keys = {(slot.day_of_week, slot.time) for slot in existing}
    to_add = sorted(wanted - existing_keys)
    kept = [slot for slot in existing if (slot.day_of_week, slot.time) in wanted or mode == "add"]
    to_remove = [slot for slot in existing if slot not in kept]
    return to_add, to_remove, kept


def apply_suggested_slots(social_account, suggestions, mode="replace"):
    """Write ``suggestions`` as the account's posting slots; returns ``(added, removed)`` counts.

    Posts already holding a time keep it: slots only decide where *future*
    queue entries go. Idempotent: applying the same suggestions twice changes
    nothing the second time.
    """
    from django.db import transaction

    suggestions = list(suggestions)
    if not suggestions:
        raise ValueError("There are no suggested times to apply.")
    with transaction.atomic():
        list(type(social_account).objects.select_for_update().filter(pk=social_account.pk))
        to_add, to_remove, _kept = plan_slot_changes(social_account, suggestions, mode)
        if to_remove:
            PostingSlot.objects.filter(id__in=[slot.id for slot in to_remove]).delete()
        PostingSlot.objects.bulk_create(
            [PostingSlot(social_account=social_account, day_of_week=day, time=at) for day, at in to_add],
            ignore_conflicts=True,
        )
    return len(to_add), len(to_remove)


# ---------------------------------------------------------------------------
# Resuming a paused channel
#
# While a channel is paused its scheduled posts wait. Whatever came due in that time is "held"; the
# person (or agent) resuming picks what happens to it. Posts still in the future keep their time.
# ---------------------------------------------------------------------------

#: What to do with held posts on resume. The first is the default everywhere (UI, REST, MCP).
HELD_POSTS_CHOICES = ("requeue", "publish_now", "skip")
DEFAULT_HELD_POSTS = HELD_POSTS_CHOICES[0]


def overdue_held_posts(social_account):
    """Scheduled posts on ``social_account`` whose time has passed, in the order they were due.

    A post parked on a retry backoff is the publisher's to retry, not held by the pause, so it is left out.
    """
    from apps.composer.models import PlatformPost

    return (
        PlatformPost.objects.filter(social_account=social_account, status=PlatformPost.Status.SCHEDULED, retry_count=0)
        .annotate(effective_at=Coalesce("scheduled_at", "post__scheduled_at"))
        .filter(effective_at__lte=timezone.now())
        .select_related("post")
        .order_by(F("effective_at").asc(), "post__created_at", "id")
    )


def resume_publishing(social_account, held_posts=DEFAULT_HELD_POSTS, *, by=None):
    """Resume a paused channel, handling the posts that came due while it was paused.

    ``held_posts``: ``requeue`` moves each into the channel's next open posting slots, keeping their order
    (any left without a slot, because the channel has none or the horizon is full, become drafts);
    ``publish_now`` sends them a minute apart, like the calendar's bulk publish; ``skip`` turns them into
    drafts. Nothing is deleted. The posts are settled before the pause is lifted, so the publisher never
    sees them as overdue. Returns ``{"requeued", "published_now", "skipped"}`` counts.
    """
    from apps.composer.services import sync_post_scheduled_at, transition_platform_post
    from apps.social_accounts.models import SocialAccount

    if held_posts not in HELD_POSTS_CHOICES:
        raise ValueError(f"held_posts must be one of: {', '.join(HELD_POSTS_CHOICES)}.")
    outcome = {"requeued": 0, "published_now": 0, "skipped": 0}
    with transaction.atomic():
        list(SocialAccount.objects.select_for_update().filter(id=social_account.id))
        social_account.refresh_from_db(fields=["paused_at", "paused_by"])
        if not social_account.is_paused:
            return outcome
        rows = list(overdue_held_posts(social_account))
        now = timezone.now()
        slots = next_available_slots(social_account, len(rows)) if held_posts == "requeue" else []
        for index, pp in enumerate(rows):
            if held_posts == "skip" or (held_posts == "requeue" and index >= len(slots)):
                if pp.can_transition_to("draft"):
                    transition_platform_post(pp, "draft")
                    outcome["skipped"] += 1
                continue
            when = slots[index] if held_posts == "requeue" else now + timedelta(minutes=index)
            pp.scheduled_at = when
            pp.save(update_fields=["scheduled_at", "updated_at"])
            QueueEntry.objects.filter(post=pp.post, queue__social_account=social_account).update(
                assigned_slot_datetime=when
            )
            sync_post_scheduled_at(pp.post)
            outcome["requeued" if held_posts == "requeue" else "published_now"] += 1
        social_account.set_paused(False, by=by)
    return outcome
