"""Per-token daily budgets: AI calls and scheduled posts (requests are counted from ``ApiKeyUsageHourly``).

A budget belongs to a token and a UTC day, like the other call caps, so it resets at 00:00 UTC without a job.
OAuth callers act as a person and carry no budgets, so every function here treats them as unlimited.
"""

from __future__ import annotations

import datetime as dt

from django.db.models import F, Sum
from django.utils import timezone

from apps.api.usage import actor_for, day_start
from apps.api_keys.models import ApiKey, ApiKeyDailyUsage, ApiKeyUsageHourly


def utc_today(now: dt.datetime | None = None) -> dt.date:
    return (now or timezone.now()).astimezone(dt.UTC).date()


def _tracked(api_key) -> bool:
    return isinstance(api_key, ApiKey)


def _reserve(api_key: ApiKey, field: str, limit: int | None, amount: int = 1) -> bool:
    """Add ``amount`` to today's ``field`` unless that would pass ``limit``. One UPDATE, so concurrent calls can't overspend."""
    day = utc_today()
    ApiKeyDailyUsage.objects.get_or_create(api_key_id=api_key.pk, day=day)
    rows = ApiKeyDailyUsage.objects.filter(api_key_id=api_key.pk, day=day)
    if limit is not None:
        rows = rows.filter(**{f"{field}__lte": max(limit - amount, -1)})
    return rows.update(**{field: F(field) + amount}) == 1


def reserve_ai_call(api_key) -> bool:
    """Spend one AI call from today's budget; False when the budget is used up."""
    if not _tracked(api_key):
        return True
    return _reserve(api_key, "ai_calls", api_key.daily_ai_budget)


def record_scheduled_post(api_key) -> None:
    """Count a post this token got scheduled. Counted even without a budget, so the page can show it."""
    if _tracked(api_key):
        _reserve(api_key, "posts_scheduled", None)


def scheduled_today(api_key) -> int:
    if not _tracked(api_key):
        return 0
    row = ApiKeyDailyUsage.objects.filter(api_key_id=api_key.pk, day=utc_today()).first()
    return row.posts_scheduled if row else 0


def post_budget_spent(api_key) -> bool:
    budget = getattr(api_key, "daily_post_budget", None)
    return budget is not None and scheduled_today(api_key) >= budget


def snapshots(api_keys) -> dict:
    """Today's spend against each budget for many keys in two queries: ``{key_id: {kind: (used, limit or None)}}``."""
    keys = list(api_keys)
    today = utc_today()
    rows = {r.api_key_id: r for r in ApiKeyDailyUsage.objects.filter(api_key__in=keys, day=today)}
    actors = {actor_for(k): k.pk for k in keys}
    calls = {
        row["actor"]: row["total"]
        for row in ApiKeyUsageHourly.objects.filter(actor__in=actors, hour_start__gte=day_start(timezone.now()))
        .exclude(status_code=429)
        .values("actor")
        .annotate(total=Sum("count"))
    }
    out = {}
    for key in keys:
        row = rows.get(key.pk)
        out[key.pk] = {
            "requests": (calls.get(actor_for(key), 0), key.daily_request_budget),
            "ai": (row.ai_calls if row else 0, key.daily_ai_budget),
            "posts": (row.posts_scheduled if row else 0, key.daily_post_budget),
        }
    return out
