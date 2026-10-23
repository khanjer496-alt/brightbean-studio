"""Date ranges for the analytics pages, resolved in the workspace's timezone.

One parser feeds every page, export and API call so the same querystring means
the same window everywhere:

* ``range=7|30|90`` (presets) or ``range=custom`` with ``start=`` / ``end=``
  (ISO dates, both inclusive);
* ``compare=previous`` (default) or ``compare=none``.

"Today" is the workspace's local date, not the server's: a workspace in
Auckland looking at "last 7 days" at 09:00 local must not lose a day to UTC.
Snapshot rows are keyed by the date the platform reported, so they are compared
against these local dates as-is; post timestamps are real instants and are
bounded by local midnights (:meth:`AnalyticsRange.bounds`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.utils import timezone

PRESET_DAYS = (7, 30, 90)
DEFAULT_DAYS = 30
MAX_CUSTOM_DAYS = 366
COMPARE_PREVIOUS = "previous"
COMPARE_NONE = "none"
CUSTOM = "custom"


def workspace_tzinfo(workspace) -> tzinfo:
    """The workspace's timezone, falling back to UTC for a blank or unknown name."""
    try:
        return ZoneInfo(workspace.effective_timezone or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def local_today(tz: tzinfo, *, now: datetime | None = None) -> date:
    return (now or timezone.now()).astimezone(tz).date()


@dataclass(frozen=True)
class AnalyticsRange:
    start: date
    end: date  # inclusive
    tz: tzinfo
    preset: str  # "7" | "30" | "90" | "custom"
    compare: bool

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    @property
    def is_custom(self) -> bool:
        return self.preset == CUSTOM

    @property
    def previous_start(self) -> date:
        return self.start - timedelta(days=self.days)

    @property
    def previous_end(self) -> date:
        return self.start - timedelta(days=1)

    def previous(self) -> AnalyticsRange:
        """The equal-length window that ends the day before this one starts."""
        return AnalyticsRange(self.previous_start, self.previous_end, self.tz, CUSTOM, False)

    def day_list(self, start: date | None = None, end: date | None = None) -> list[date]:
        first, last = start or self.start, end or self.end
        return [first + timedelta(days=i) for i in range((last - first).days + 1)]

    def bounds(self, start: date | None = None, end: date | None = None) -> tuple[datetime, datetime]:
        """Half-open ``[from, to)`` instants covering whole local days."""
        first, last = start or self.start, end or self.end
        return (
            datetime.combine(first, time.min, tzinfo=self.tz),
            datetime.combine(last + timedelta(days=1), time.min, tzinfo=self.tz),
        )

    @property
    def label(self) -> str:
        if self.is_custom:
            return f"{self.start:%b %-d, %Y} – {self.end:%b %-d, %Y}"
        return f"Last {self.days} days"

    def params(self) -> dict[str, str]:
        """The querystring that reproduces this range exactly."""
        out = {"range": self.preset}
        if self.is_custom:
            out.update(start=self.start.isoformat(), end=self.end.isoformat())
        if not self.compare:
            out["compare"] = COMPARE_NONE
        return out

    def querystring(self, extra: dict | None = None, **overrides: str | None) -> str:
        """``?range=…`` for links; ``overrides`` replace keys (``None`` drops one)."""
        merged: dict = {**self.params(), **(extra or {})}
        for key, value in overrides.items():
            if value is None:
                merged.pop(key, None)
            else:
                merged[key] = value
        pairs: list[tuple[str, str]] = []
        for key, value in merged.items():
            if isinstance(value, (list, tuple)):
                pairs.extend((key, str(v)) for v in value)
            else:
                pairs.append((key, str(value)))
        return "?" + urlencode(pairs)


def _parse_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def parse_range(params, workspace, *, now: datetime | None = None) -> AnalyticsRange:
    """Build the range from request params; anything unusable falls back to 30 days.

    A custom range is clamped rather than rejected: a future ``end`` becomes
    today, a reversed pair is swapped, and a span past ``MAX_CUSTOM_DAYS`` keeps
    its end and loses the earliest days, so a hand-edited URL still renders.
    """
    tz = workspace_tzinfo(workspace)
    today = local_today(tz, now=now)
    compare = params.get("compare") != COMPARE_NONE
    raw = params.get("range")
    if raw == CUSTOM:
        start, end = _parse_date(params.get("start")), _parse_date(params.get("end"))
        if start and end:
            if start > end:
                start, end = end, start
            end = min(end, today)
            start = max(min(start, end), end - timedelta(days=MAX_CUSTOM_DAYS - 1))
            return AnalyticsRange(start, end, tz, CUSTOM, compare)
    try:
        days = int(raw or DEFAULT_DAYS)
    except (TypeError, ValueError):
        days = DEFAULT_DAYS
    if days not in PRESET_DAYS:
        days = DEFAULT_DAYS
    return AnalyticsRange(today - timedelta(days=days - 1), today, tz, str(days), compare)


def api_range(workspace, *, days: int | None, start: date | None, end: date | None, compare: bool) -> AnalyticsRange:
    """Range for the agent API: a custom ``start``..``end`` pair, else the last ``days`` (default 30)."""
    params: dict[str, str] = {} if compare else {"compare": COMPARE_NONE}
    if start and end:
        params.update(range=CUSTOM, start=start.isoformat(), end=end.isoformat())
        return parse_range(params, workspace)
    tz = workspace_tzinfo(workspace)
    days = days or DEFAULT_DAYS
    today = local_today(tz)
    return AnalyticsRange(
        today - timedelta(days=days - 1), today, tz, str(days) if days in PRESET_DAYS else CUSTOM, compare
    )
