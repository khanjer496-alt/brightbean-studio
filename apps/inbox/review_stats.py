"""Review analytics: average rating, its weekly trend, response rate and time per location.

Computed from the reviews the inbox holds, over the same window the analytics
pages use. A review counts in the window it was *created* in (the platform's
``create_time``), and counts as answered when it carries a public reply, whether
the team wrote it here or someone wrote it on the platform.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from statistics import median

from providers import PROVIDER_REGISTRY

from .models import InboxMessage


def review_accounts(accounts):
    """The accounts of platforms that have customer reviews."""
    return [a for a in accounts if getattr(PROVIDER_REGISTRY.get(a.platform), "supports_reviews", False)]


def _parse(value):
    try:
        return datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def _review_rows(accounts, start_at, end_at):
    """``(message, created_at)`` for reviews of ``accounts`` created in ``[start_at, end_at)``."""
    rows = []
    qs = InboxMessage.objects.filter(
        social_account__in=accounts, message_type=InboxMessage.MessageType.REVIEW, rating__isnull=False
    ).filter(received_at__gte=start_at)
    for message in qs:
        created = _parse((message.extra or {}).get("create_time")) or message.received_at
        if start_at <= created < end_at:
            rows.append((message, created))
    return rows


def summarize(rows) -> dict:
    """Count, mean rating, response rate and median response time of ``(message, created_at)`` rows."""
    count = len(rows)
    if not count:
        return {"count": 0, "average": None, "response_rate": None, "median_response_hours": None}
    average = sum(m.rating for m, _ in rows) / count
    answered = [(m, c) for m, c in rows if m.review_reply]
    hours = []
    for message, created in answered:
        replied = _parse(message.review_reply.get("update_time"))
        if replied is not None and replied >= created:
            hours.append((replied - created).total_seconds() / 3600)
    return {
        "count": count,
        "average": round(average, 2),
        "response_rate": round(100 * len(answered) / count),
        "median_response_hours": round(median(hours), 1) if hours else None,
    }


def weekly_average(rows, tz) -> list[dict]:
    """Mean rating per week (weeks start Monday, in ``tz``), oldest first; weeks without reviews are left out."""
    weeks: dict[date, list[int]] = {}
    for message, created in rows:
        day = created.astimezone(tz).date()
        weeks.setdefault(day - timedelta(days=day.weekday()), []).append(message.rating)
    return [{"week": week, "average": round(sum(r) / len(r), 2), "count": len(r)} for week, r in sorted(weeks.items())]


def review_overview(accounts, rng) -> dict | None:
    """Per-location review stats for the window ``rng``, with the previous window's average; None without review accounts."""
    accounts = review_accounts(accounts)
    if not accounts:
        return None
    start_at, end_at = rng.bounds()
    prev_start, prev_end = rng.bounds(rng.previous_start, rng.previous_end)
    current = _review_rows(accounts, prev_start, end_at)
    locations = []
    for account in accounts:
        mine = [(m, c) for m, c in current if m.social_account_id == account.id]
        now = summarize([(m, c) for m, c in mine if start_at <= c < end_at])
        before = summarize([(m, c) for m, c in mine if prev_start <= c < prev_end])
        delta = None
        if now["average"] is not None and before["average"] is not None:
            delta = round(now["average"] - before["average"], 2)
        locations.append({"account": account, **now, "previous_average": before["average"], "delta": delta})
    window = [(m, c) for m, c in current if start_at <= c < end_at]
    return {"locations": locations, "total": summarize(window), "weekly": weekly_average(window, rng.tz)}
