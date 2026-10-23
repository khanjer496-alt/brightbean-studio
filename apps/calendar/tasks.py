"""Background tasks for the Content Calendar (F-2.3)."""

import copy
import logging
import zoneinfo
from datetime import datetime, timedelta

from background_task import background
from dateutil.relativedelta import relativedelta
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.composer.models import PlatformPost, Post, PostMedia
from apps.composer.services import post_media_types, publish_blocker

from .models import RecurrenceRule

logger = logging.getLogger(__name__)

LOOKAHEAD_DAYS = 90

# Source rows whose future occurrences are still meant to publish on their own.
# A row the team pulled back to draft, put on hold, or sent for review or
# rejected has not committed its recurrences either, so those clone as drafts.
_RECURRING_COMMITTED_STATUSES = frozenset(
    {
        PlatformPost.Status.SCHEDULED,
        PlatformPost.Status.PUBLISHING,
        PlatformPost.Status.PUBLISHED,
        PlatformPost.Status.FAILED,
    }
)


# How often the recurring-post sweep runs; registered on a repeating schedule by
# apps.calendar.apps.CalendarConfig. Saving a series generates its copies at
# once, so this only has to keep the lookahead window topped up.
RECURRENCE_GENERATION_INTERVAL_SECONDS = 3600  # hourly

# Safety bound on the dates one rule may produce in a single pass. A daily
# series is walked from its first post, so a few years of it stay well inside.
_MAX_SERIES_DATES = 10_000


@background(schedule=0)
def run_recurring_post_generation():
    """Background entry point for :func:`generate_recurring_posts`."""
    generate_recurring_posts()


def generate_recurring_posts():
    """Generate individual Post records from active RecurrenceRules.

    Runs daily. For each active rule, computes recurrence dates from the
    source post's scheduled_at up to 90 days ahead. Creates clones of the
    source post for each date not yet generated.
    """
    rules = RecurrenceRule.objects.filter(is_active=True).select_related("post__workspace")
    now = timezone.now()
    generated_total = 0

    for rule in rules:
        generated_total += generate_occurrences(rule, now=now)

    logger.info("Generated %d recurring posts.", generated_total)
    return generated_total


def generate_occurrences(rule, *, now=None):
    """Create the missing copies of ``rule``'s post inside the lookahead window.

    Idempotent: a date that already has a copy is skipped, so the hourly sweep
    and a save of the series can both call it. Returns the number created.
    """
    now = now or timezone.now()
    # Hold the rule's row for the whole pass: the hourly sweep and a save of
    # the series can run at once, and each would otherwise see the same dates
    # missing and create them twice. A second caller waits, then finds them.
    with transaction.atomic():
        rule = RecurrenceRule.objects.select_for_update(of=("self",)).select_related("post__workspace").get(pk=rule.pk)
        return _generate_locked(rule, now)


def _generate_locked(rule, now):
    """``generate_occurrences`` once ``rule``'s row is locked."""
    source = rule.post
    if not rule.is_active or not source.scheduled_at:
        return 0
    source_pps = list(source.platform_posts.select_related("social_account"))
    # A series runs once its first post is committed to publishing. One still
    # in review keeps its rule and starts when it is approved and scheduled;
    # its copies then carry that approval (see _create_occurrence).
    if source_pps and not any(pp.status in _RECURRING_COMMITTED_STATUSES for pp in source_pps):
        return 0

    # Recurrence dates and times are computed in the post's workspace
    # timezone so the wall-clock time is preserved across DST boundaries
    # (a 09:00-local series stays at 09:00 local, not drifting by the DST
    # offset). ``scheduled_at`` is stored as UTC, so convert it back first.
    ws_tz = zoneinfo.ZoneInfo(source.workspace.effective_timezone or "UTC")
    local_start = source.scheduled_at.astimezone(ws_tz)
    base_date = local_start.date()
    base_time = local_start.time()
    today_local = now.astimezone(ws_tz).date()

    # Bound the lookahead horizon in the workspace's local calendar so it
    # lands on the same local day for every timezone, not the UTC date.
    cutoff = today_local + timedelta(days=LOOKAHEAD_DAYS)
    end = rule.end_date or cutoff
    if end > cutoff:
        end = cutoff

    dates = _compute_recurrence_dates(
        base_date,
        rule.frequency,
        rule.interval,
        end,
        weekdays=rule.weekdays,
        count=rule.count,
    )
    # Only future dates are ever created, so only they need the lookup below.
    dates = [d for d in dates if d > today_local]
    if not dates:
        rule.last_generated_at = now
        rule.save(update_fields=["last_generated_at"])
        return 0

    # Filter out dates already generated: copies linked to this post, plus
    # posts with the same caption, which is how copies made before the link
    # existed are recognised. Extract dates in the workspace zone so they line
    # up with the ws-local recurrence dates above rather than UTC calendar days.
    with timezone.override(ws_tz):
        existing_dates = set(
            Post.objects.filter(
                Q(recurrence_source=source) | Q(caption=source.caption),
                workspace=source.workspace,
                scheduled_at__date__in=dates,
            )
            .exclude(id=source.id)
            .values_list("scheduled_at__date", flat=True)
        )

    # Every clone carries the source's media, so one answer serves each
    # recurrence date and platform of this rule.
    media_types = post_media_types(source)
    source_media = list(source.media_attachments.all())
    generated = 0

    for d in dates:
        if d in existing_dates:
            continue

        scheduled_dt = datetime.combine(d, base_time).replace(tzinfo=ws_tz)
        # One copy at a time: a copy left without its platform posts would
        # block its date for good, since existing dates are skipped.
        with transaction.atomic():
            _create_occurrence(source, scheduled_dt, d, source_pps, source_media, media_types)
        generated += 1

    rule.last_generated_at = now
    rule.save(update_fields=["last_generated_at"])
    return generated


def _create_occurrence(source, scheduled_dt, d, source_pps, source_media, media_types):
    """Clone ``source`` to ``scheduled_dt`` with its platform posts and media."""
    # Clone the post
    new_post = Post.objects.create(
        workspace=source.workspace,
        author=source.author,
        title=source.title,
        caption=source.caption,
        first_comment=source.first_comment,
        internal_notes=source.internal_notes,
        tags=source.tags,
        category=source.category,
        scheduled_at=scheduled_dt,
        recurrence_source=source,
    )

    # Clone platform posts in bulk, preserving per-platform offsets
    if source_pps:
        new_pps = []
        for pp in source_pps:
            # The source was checked when it was scheduled, but its media
            # or board can have gone since. A clone that can only fail is
            # left as a draft at its date: still on the calendar to fix,
            # but not handed to the publisher.
            blocked = publish_blocker(pp.social_account, media_types=media_types, platform_extra=pp.platform_extra)
            if blocked:
                logger.warning(
                    "Recurrence of post %s on %s left as a draft for %s: %s",
                    source.id,
                    d,
                    pp.social_account_id,
                    blocked,
                )
            # Preserve the offset between source PP's scheduled_at and
            # source post's scheduled_at, so per-platform time deltas
            # carry into each recurrence.
            pp_scheduled = None
            if pp.scheduled_at and source.scheduled_at:
                delta = pp.scheduled_at - source.scheduled_at
                pp_scheduled = scheduled_dt + delta
            new_pps.append(
                PlatformPost(
                    post=new_post,
                    social_account=pp.social_account,
                    platform_specific_title=pp.platform_specific_title,
                    platform_specific_caption=pp.platform_specific_caption,
                    platform_specific_first_comment=pp.platform_specific_first_comment,
                    platform_specific_media=pp.platform_specific_media,
                    # Carry per-platform settings (e.g. TikTok privacy level,
                    # comment/duet/stitch toggles, disclosure flags) into each
                    # recurrence. Without this the publisher falls back to
                    # provider defaults and loses the creator's choices.
                    platform_extra=copy.deepcopy(pp.platform_extra) if pp.platform_extra else {},
                    scheduled_at=pp_scheduled,
                    status=("scheduled" if pp.status in _RECURRING_COMMITTED_STATUSES and not blocked else "draft"),
                    # A copy is the approved content at another date, so it
                    # carries the first post's approval proof. The publisher
                    # recomputes the content fingerprint before sending, so a
                    # copy that has drifted from what was approved still stops.
                    approval_completed_at=pp.approval_completed_at,
                    approval_fingerprint=pp.approval_fingerprint,
                )
            )
        PlatformPost.objects.bulk_create(new_pps)

        # Sync Post.scheduled_at to min of children.
        from apps.composer.services import sync_post_scheduled_at

        sync_post_scheduled_at(new_post)

    # Clone media attachments in bulk
    if source_media:
        PostMedia.objects.bulk_create(
            [
                PostMedia(
                    post=new_post,
                    media_asset=pm.media_asset,
                    position=pm.position,
                    alt_text=pm.alt_text,
                    platform_overrides=pm.platform_overrides,
                )
                for pm in source_media
            ]
        )


def _compute_recurrence_dates(base_date, frequency, interval, end_date, *, weekdays=(), count=None):
    """Compute the recurrence dates after ``base_date`` up to ``end_date``.

    The series is always walked from ``base_date`` so ``count`` — the total
    posts in the series, the first included — means the same on every run.
    Weekly rules post on each of ``weekdays`` (0=Monday) in every
    ``interval``-th week, counted from the week of ``base_date``; without
    weekdays they repeat ``base_date``'s weekday. Monthly dates are measured
    from ``base_date`` each time, so a series on the 31st lands on the last
    day of a short month and returns to the 31st after it.
    """
    interval = max(int(interval or 1), 1)
    limit = _MAX_SERIES_DATES if count is None else min(_MAX_SERIES_DATES, max(count - 1, 0))
    dates = []
    if frequency == "weekly" and weekdays:
        week_start = base_date - timedelta(days=base_date.weekday())
        days = sorted({int(w) for w in weekdays})
        step = 0
        while len(dates) < limit and week_start <= end_date:
            for wd in days:
                d = week_start + timedelta(days=wd)
                if d <= base_date:
                    continue
                if d > end_date or len(dates) >= limit:
                    break
                dates.append(d)
            step += 1
            week_start = week_start + timedelta(weeks=interval)
            if step > _MAX_SERIES_DATES:
                break
        return dates

    for n in range(1, _MAX_SERIES_DATES + 1):
        if len(dates) >= limit:
            break
        if frequency == "daily":
            current = base_date + timedelta(days=interval * n)
        elif frequency == "weekly":
            current = base_date + timedelta(weeks=interval * n)
        elif frequency == "monthly":
            current = base_date + relativedelta(months=interval * n)
        else:
            break

        if current > end_date:
            break

        dates.append(current)

    return dates
