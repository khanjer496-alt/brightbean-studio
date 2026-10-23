"""Which days a report covers, and when its schedule next fires.

Everything is read in the workspace's timezone, the same one the analytics
pages use (``apps.analytics.ranges``), so "last month" for a workspace in
Auckland starts at Auckland's midnight, whatever the server's clock says.

Rolling ranges end *yesterday*: a report is a finished document, and today's
partial day would read as a dip.
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta

from apps.analytics.ranges import CUSTOM, AnalyticsRange, local_today, workspace_tzinfo

from .models import ReportDefinition

RANGE_DAYS = {
    ReportDefinition.RangeType.LAST_7: 7,
    ReportDefinition.RangeType.LAST_30: 30,
}


def resolve_range(definition: ReportDefinition, *, now: datetime | None = None) -> AnalyticsRange:
    """The inclusive date range the definition covers when run at ``now``."""
    tz = workspace_tzinfo(definition.workspace)
    today = local_today(tz, now=now)
    kind = definition.range_type
    if kind in RANGE_DAYS:
        days = RANGE_DAYS[kind]
        end = today - timedelta(days=1)
        return AnalyticsRange(end - timedelta(days=days - 1), end, tz, str(days), definition.compare)
    if kind == ReportDefinition.RangeType.LAST_MONTH:
        end = today.replace(day=1) - timedelta(days=1)
        return AnalyticsRange(end.replace(day=1), end, tz, CUSTOM, definition.compare)
    return AnalyticsRange(definition.custom_start, definition.custom_end, tz, CUSTOM, definition.compare)


def range_for_dates(definition: ReportDefinition, start: date, end: date) -> AnalyticsRange:
    """A stored run's range, rebuilt from its dates (the workspace tz may have changed since)."""
    return AnalyticsRange(start, end, workspace_tzinfo(definition.workspace), CUSTOM, definition.compare)


def _slot(definition: ReportDefinition, tz, day: date) -> tuple[str, datetime]:
    """The schedule slot that contains local ``day``: its key and when it fires."""
    if definition.frequency == ReportDefinition.Frequency.WEEKLY:
        week_start = day - timedelta(days=day.weekday())
        fire_day = week_start + timedelta(days=definition.weekday)
        iso_year, iso_week, _ = week_start.isocalendar()
        key = f"W{iso_year}-{iso_week:02d}"
    else:
        # The 31st fires on the 30th in a 30-day month and on the 28th or 29th in February.
        last = calendar.monthrange(day.year, day.month)[1]
        fire_day = day.replace(day=min(definition.day_of_month, last))
        key = f"M{day.year}-{day.month:02d}"
    return key, datetime.combine(fire_day, definition.send_time, tzinfo=tz)


def due_slot(definition: ReportDefinition, now: datetime) -> tuple[str, datetime] | None:
    """``(schedule_key, fire_time)`` if the definition's current slot has come, else ``None``.

    There is one slot per ISO week (weekly) or calendar month (monthly). A slot
    that was missed (the worker was down at 09:00) is still due later in the
    same week or month, and runs once: the key is the slot, not the moment.
    Slots before ``schedule_anchor`` are skipped.
    """
    if not definition.is_scheduled:
        return None
    tz = workspace_tzinfo(definition.workspace)
    key, fire_at = _slot(definition, tz, now.astimezone(tz).date())
    if now < fire_at:
        return None
    if definition.schedule_anchor and fire_at < definition.schedule_anchor:
        return None
    return key, fire_at


def next_fire(definition: ReportDefinition, now: datetime) -> datetime | None:
    """When the schedule fires next after ``now`` (for display), or ``None`` if it isn't scheduled."""
    if not definition.is_scheduled:
        return None
    tz = workspace_tzinfo(definition.workspace)
    day = now.astimezone(tz).date()
    for _ in range(3):
        _, fire_at = _slot(definition, tz, day)
        if fire_at > now:
            return fire_at
        # Jump into the next week or month.
        day = (
            day + timedelta(days=7)
            if definition.frequency == ReportDefinition.Frequency.WEEKLY
            else (day.replace(day=28) + timedelta(days=5)).replace(day=1)
        )
    return None
