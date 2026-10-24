"""Per-token daily budgets: requests, AI calls and scheduled posts.

A budget belongs to a token (an ``ApiKey``, or the ``ConnectedApp`` behind an OAuth client) and a UTC day, like
the other call caps, so it resets at 00:00 UTC without a job. A key's requests are counted from
``ApiKeyUsageHourly``; everything else is spent here, one conditional ``UPDATE`` per reservation, so concurrent
requests can never spend past a budget: Postgres locks the row for the UPDATE and re-checks the condition once
the other writer commits.
"""

from __future__ import annotations

import datetime as dt

from django.db.models import F, Sum
from django.utils import timezone

from apps.api.usage import actor_for, day_start
from apps.api_keys.models import ApiKey, ApiKeyDailyUsage, ApiKeyUsageHourly, ConnectedApp


def utc_today(now: dt.datetime | None = None) -> dt.date:
    return (now or timezone.now()).astimezone(dt.UTC).date()


def _owner(token) -> tuple[str, dict] | None:
    """``(actor, row links)`` for a token that carries budgets; None for a person or a grant with no connection."""
    if isinstance(token, ApiKey):
        return f"key:{token.pk}", {"api_key_id": token.pk}
    connection = token if isinstance(token, ConnectedApp) else getattr(token, "connection", None)
    if connection is not None:
        return f"grant:{connection.pk}", {"connected_app_id": connection.pk}
    return None


def _reserve(token, field: str, limit: int | None, amount: int = 1) -> bool:
    """Add ``amount`` to today's ``field`` unless that would pass ``limit``; False when it would."""
    owner = _owner(token)
    if owner is None:
        return True
    actor, links = owner
    day = utc_today()
    ApiKeyDailyUsage.objects.get_or_create(actor=actor, day=day, defaults=links)
    rows = ApiKeyDailyUsage.objects.filter(actor=actor, day=day)
    if limit is not None:
        if amount > limit:
            return False
        rows = rows.filter(**{f"{field}__lte": limit - amount})
    return rows.update(**{field: F(field) + amount}) == 1


def reserve_request(token) -> bool:
    """Spend one call from an OAuth connection's request budget (a key's calls are counted per hour instead)."""
    if isinstance(token, ApiKey) or getattr(token, "daily_request_budget", None) is None:
        return True
    return _reserve(token, "requests", token.daily_request_budget)


def reserve_ai_call(token, amount: int = 1) -> bool:
    """Spend ``amount`` AI calls from today's budget; False when that would pass it."""
    return _reserve(token, "ai_calls", getattr(token, "daily_ai_budget", None), amount)


def reserve_scheduled_post(token) -> bool:
    """Spend one scheduled post from today's budget before the post is allowed to go out; False when it is spent.

    Called once per post the request would schedule, at the moment the autonomy gate allows it, so a request that
    schedules several posts counts every one of them and stops at the budget instead of overshooting it.
    """
    return _reserve(token, "posts_scheduled", getattr(token, "daily_post_budget", None))


def release_scheduled_posts(token, amount: int) -> None:
    """Give back posts reserved for a request that then scheduled none of them (another of its posts was refused)."""
    owner = _owner(token)
    if owner is None or amount <= 0:
        return
    ApiKeyDailyUsage.objects.filter(actor=owner[0], day=utc_today(), posts_scheduled__gte=amount).update(
        posts_scheduled=F("posts_scheduled") - amount
    )


def snapshots(tokens) -> dict:
    """Today's spend against each budget for many tokens in two queries: ``{pk: {kind: (used, limit or None)}}``."""
    tokens = list(tokens)
    owners = {t.pk: _owner(t) for t in tokens}
    rows = {
        r.actor: r
        for r in ApiKeyDailyUsage.objects.filter(actor__in=[o[0] for o in owners.values() if o], day=utc_today())
    }
    key_actors = [actor_for(t) for t in tokens if isinstance(t, ApiKey)]
    calls = (
        {
            row["actor"]: row["total"]
            for row in ApiKeyUsageHourly.objects.filter(actor__in=key_actors, hour_start__gte=day_start(timezone.now()))
            .exclude(status_code=429)
            .values("actor")
            .annotate(total=Sum("count"))
        }
        if key_actors
        else {}
    )
    out = {}
    for token in tokens:
        owner = owners[token.pk]
        row = rows.get(owner[0]) if owner else None
        used_requests = calls.get(actor_for(token), 0) if isinstance(token, ApiKey) else (row.requests if row else 0)
        out[token.pk] = {
            "requests": (used_requests, token.daily_request_budget),
            "ai": (row.ai_calls if row else 0, token.daily_ai_budget),
            "posts": (row.posts_scheduled if row else 0, token.daily_post_budget),
        }
    return out
