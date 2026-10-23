"""Best time to post: which weekday and hour works best for one account.

The score comes from the account's own published posts. Each post is rated by
its engagement rate (engagements over the views, reach or impressions it got,
or over the account's followers when the network reports none of those), the
rating is weighted by recency (half-life :data:`HALF_LIFE_DAYS`) and dropped into
the weekday x hour cell of the workspace timezone it was published in. Hours
are local wall-clock, so a post at 09:00 before and after a DST change lands in
the same cell. Each cell's mean is pulled towards the account's overall mean by
:data:`PRIOR_WEIGHT` posts' worth, so a single lucky post can't crown a slot.

With fewer than :data:`MIN_POSTS` usable posts there is not enough to learn
from, and the scores come from the network's industry-average windows
(``SocialProvider.best_time_windows``) instead. ``basis`` records which, so the
UI can say "based on your N posts" or "based on industry defaults".

Results are cached in :class:`BestTimeSnapshot`; :func:`refresh_all` is the
daily periodic task.
"""

from __future__ import annotations

import logging
import zoneinfo
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.utils import timezone

from providers import best_time_windows_for

from .metrics import ENGAGEMENT_DENOMINATORS, ENGAGEMENT_PARTS
from .models import BestTimeSnapshot, PostInsightsSnapshot

logger = logging.getLogger(__name__)

# Fewer usable posts than this and a weekday x hour grid is mostly noise.
MIN_POSTS = 20
LOOKBACK_DAYS = 180
HALF_LIFE_DAYS = 60
# How many posts' worth of the account's overall mean each cell starts with.
PRIOR_WEIGHT = 1.0
MAX_POSTS = 1000
# A snapshot older than this is recomputed on read (the daily task normally
# keeps it fresher), so a new account doesn't wait a day for its heatmap.
MAX_AGE = timedelta(hours=36)

CELLS = 7 * 24
# The score a default window earns, best window first, then this much less each.
DEFAULT_TOP_SCORE = 100
DEFAULT_STEP = 12


def cell_index(weekday: int, hour: int) -> int:
    return weekday * 24 + hour


@dataclass(frozen=True)
class BestTime:
    weekday: int
    hour: int
    score: float
    posts: int

    @property
    def time_label(self) -> str:
        return f"{self.hour:02d}:00"


@dataclass(frozen=True)
class BestTimes:
    """A computed grid plus what it was based on."""

    basis: str
    sample_size: int
    timezone: str
    scores: list[float]
    counts: list[int]
    computed_at: datetime | None = None

    @property
    def is_default(self) -> bool:
        return self.basis == BestTimeSnapshot.Basis.DEFAULTS

    @property
    def basis_label(self) -> str:
        if self.is_default:
            return "Based on industry defaults"
        return f"Based on your {self.sample_size} posts"

    def top(self, count: int = 5) -> list[BestTime]:
        """The best ``count`` cells, spread out: at most two a day, never within an hour of each other."""
        ranked = sorted(
            (i for i in range(CELLS) if self.scores[i] > 0),
            key=lambda i: (-self.scores[i], i),
        )
        picked: list[BestTime] = []
        for i in ranked:
            weekday, hour = divmod(i, 24)
            same_day = [p for p in picked if p.weekday == weekday]
            if len(same_day) >= 2 or any(abs(p.hour - hour) <= 1 for p in same_day):
                continue
            picked.append(BestTime(weekday, hour, round(self.scores[i], 1), self.counts[i]))
            if len(picked) >= count:
                break
        return picked

    def rows(self) -> list[dict]:
        """The grid as 7 rows of 24 cells, each with a 0-4 heat ``level`` for the heatmap."""
        out = []
        for weekday in range(7):
            cells = []
            for hour in range(24):
                i = cell_index(weekday, hour)
                score = self.scores[i]
                cells.append(
                    {
                        "weekday": weekday,
                        "hour": hour,
                        "score": round(score),
                        "posts": self.counts[i],
                        "level": _level(score),
                    }
                )
            out.append({"weekday": weekday, "cells": cells})
        return out


def _level(score: float) -> int:
    """0 = nothing to show, 1-4 = quartiles of the 0-100 score."""
    if score <= 0:
        return 0
    return 1 + min(3, int(score // 25))


def _workspace_tz(account) -> zoneinfo.ZoneInfo:
    try:
        return zoneinfo.ZoneInfo(account.workspace.effective_timezone or "UTC")
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return zoneinfo.ZoneInfo("UTC")


def _latest_metrics(post_ids) -> dict[object, dict[str, float]]:
    """The newest snapshot value of each metric, per platform post."""
    latest: dict[object, dict[str, tuple[object, float]]] = defaultdict(dict)
    rows = PostInsightsSnapshot.objects.filter(platform_post_id__in=post_ids).values_list(
        "platform_post_id", "metric_key", "date", "value"
    )
    for post_id, metric, day, value in rows:
        current = latest[post_id].get(metric)
        if current is None or day > current[0]:
            latest[post_id][metric] = (day, value)
    return {pid: {m: v for m, (_, v) in metrics.items()} for pid, metrics in latest.items()}


def post_engagement_rate(metrics: dict[str, float], followers: int) -> float | None:
    """Engagements per view (or reach, impression, play; else per follower), or ``None`` when unmeasurable."""
    engagements = sum(metrics.get(k, 0.0) for k in ENGAGEMENT_PARTS)
    denominator = next((metrics[d] for d in ENGAGEMENT_DENOMINATORS if metrics.get(d, 0) > 0), None)
    if denominator is None and followers > 0:
        denominator = followers
    if not denominator:
        return None
    return engagements / denominator


def score_posts(samples: list[tuple[datetime, float]], tz: zoneinfo.ZoneInfo, now: datetime):
    """Weekday x hour scores from ``(published_at, engagement_rate)`` samples.

    Returns ``(scores, counts)``, both 168 long. Cells with no post score 0.
    """
    weight_sum = [0.0] * CELLS
    weighted_rate = [0.0] * CELLS
    counts = [0] * CELLS
    total_weight = total_rate = 0.0
    for published_at, rate in samples:
        local = published_at.astimezone(tz)
        age_days = max((now - published_at).total_seconds() / 86400, 0.0)
        weight = 0.5 ** (age_days / HALF_LIFE_DAYS)
        i = cell_index(local.weekday(), local.hour)
        weight_sum[i] += weight
        weighted_rate[i] += weight * rate
        counts[i] += 1
        total_weight += weight
        total_rate += weight * rate
    overall = total_rate / total_weight if total_weight else 0.0
    shrunk = [
        (weighted_rate[i] + PRIOR_WEIGHT * overall) / (weight_sum[i] + PRIOR_WEIGHT) if counts[i] else 0.0
        for i in range(CELLS)
    ]
    best = max(shrunk, default=0.0)
    scores = [round(100 * v / best, 1) if best > 0 else 0.0 for v in shrunk]
    return scores, counts


def score_defaults(platform: str) -> list[float]:
    """Scores from the network's industry-average windows: best window 100, each next one less."""
    scores = [0.0] * CELLS
    for rank, window in enumerate(best_time_windows_for(platform)):
        score = max(DEFAULT_TOP_SCORE - rank * DEFAULT_STEP, 1)
        for day in window.days:
            for hour in window.hours:
                i = cell_index(day, hour)
                scores[i] = max(scores[i], float(score))
    return scores


def usable_samples(account, now: datetime) -> list[tuple[datetime, float]]:
    from apps.composer.models import PlatformPost

    posts = list(
        PlatformPost.objects.filter(
            social_account=account,
            status=PlatformPost.Status.PUBLISHED,
            published_at__isnull=False,
            published_at__gte=now - timedelta(days=LOOKBACK_DAYS),
            published_at__lte=now,
        )
        .order_by("-published_at")
        .values_list("id", "published_at")[:MAX_POSTS]
    )
    metrics = _latest_metrics([pid for pid, _ in posts])
    samples = []
    for pid, published_at in posts:
        rate = post_engagement_rate(metrics.get(pid, {}), account.follower_count)
        if rate is not None:
            samples.append((published_at, rate))
    return samples


def compute(account, now: datetime | None = None) -> BestTimes:
    """Work out ``account``'s grid from scratch (no caching)."""
    now = now or timezone.now()
    tz = _workspace_tz(account)
    samples = usable_samples(account, now)
    if len(samples) >= MIN_POSTS:
        scores, counts = score_posts(samples, tz, now)
        if any(scores):
            return BestTimes(BestTimeSnapshot.Basis.POSTS, len(samples), tz.key, scores, counts)
    return BestTimes(BestTimeSnapshot.Basis.DEFAULTS, 0, tz.key, score_defaults(account.platform), [0] * CELLS)


def refresh(account, now: datetime | None = None) -> BestTimes:
    """Recompute and store ``account``'s snapshot. Idempotent."""
    result = compute(account, now)
    snapshot, _ = BestTimeSnapshot.objects.update_or_create(
        social_account=account,
        defaults={
            "basis": result.basis,
            "sample_size": result.sample_size,
            "timezone": result.timezone,
            "scores": result.scores,
            "counts": result.counts,
        },
    )
    return BestTimes(
        result.basis, result.sample_size, result.timezone, result.scores, result.counts, snapshot.computed_at
    )


def get_best_times(account) -> BestTimes:
    """The cached grid, recomputed when missing, stale, or in another timezone than the workspace's."""
    snapshot = BestTimeSnapshot.objects.filter(social_account=account).first()
    tz = _workspace_tz(account).key
    if (
        snapshot is None
        or snapshot.timezone != tz
        or len(snapshot.scores) != CELLS
        or timezone.now() - snapshot.computed_at > MAX_AGE
    ):
        return refresh(account)
    return BestTimes(
        snapshot.basis, snapshot.sample_size, snapshot.timezone, snapshot.scores, snapshot.counts, snapshot.computed_at
    )


def refresh_all() -> int:
    """Recompute every connected account's snapshot; returns how many. One bad account never stops the rest."""
    from apps.social_accounts.models import SocialAccount

    accounts = SocialAccount.objects.filter(connection_status=SocialAccount.ConnectionStatus.CONNECTED).select_related(
        "workspace__organization"
    )
    done = 0
    for account in accounts.iterator():
        try:
            refresh(account)
            done += 1
        except Exception:
            logger.exception("Best-time refresh failed for account %s", account.pk)
    return done


def upcoming(best_times: BestTimes, count: int = 3, *, after: datetime | None = None, days: int = 7):
    """The next ``count`` best-time datetimes (aware, workspace timezone) after ``after``, soonest first."""
    after = after or timezone.now()
    tz = zoneinfo.ZoneInfo(best_times.timezone)
    local_after = after.astimezone(tz)
    top = best_times.top(count=max(count * 2, 6))
    found = []
    for offset in range(days + 1):
        day = (local_after + timedelta(days=offset)).date()
        for slot in top:
            if slot.weekday != day.weekday():
                continue
            when = datetime(day.year, day.month, day.day, slot.hour, tzinfo=tz)
            if when > after:
                found.append((when, slot))
    found.sort(key=lambda pair: pair[0])
    return found[:count]
