"""Workspace-level rollups: many accounts, one window, honest about gaps.

The single-account page reads one account's snapshots. This module answers the
questions that span accounts (overview, compare, exports, the agent API) with a
fixed number of queries however many accounts there are: one for the daily
account snapshots, one for the per-post fallback, one for post counts, and two
for posts and their latest stats.

Rules that keep the numbers truthful:

* A metric an account does not report is ``None`` ("not available"), never 0.
  A real zero needs a snapshot that said so.
* A day is only "reported" between an account's first and last snapshot for
  that metric; the days after the last one simply haven't arrived (YouTube
  lags 2-3 days) and before the first one the account wasn't being measured.
* A total is the sum over the accounts that report it, and says how many that
  was. A change vs. the previous period compares only the accounts that
  reported in BOTH periods, and only when the earlier period is at least as
  completely reported as the current one, so an account connected part-way
  through the previous period can't read as growth.
* Accounts on :data:`NATIVE_ONLY_PLATFORMS` contribute their reported counts
  but never feed a calculated figure (engagements, engagement rate).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date
from itertools import groupby
from typing import Any

from django.db.models import Count, Max
from django.db.models.functions import TruncDate

from apps.composer.models import PlatformPost
from apps.social_accounts.models import AnalyticsPlatformConfig, SocialAccount

from . import services
from .constants import NATIVE_ONLY_PLATFORMS
from .metrics import (
    ENGAGEMENT_PARTS,
    EXPOSURE_METRICS,
    GROWTH_METRICS,
    METRICS,
    PLATFORM_METRICS,
    post_metrics_for,
)
from .models import AccountInsightsSnapshot, PostInsightsSnapshot
from .ranges import AnalyticsRange

# (key, label, kind) in display order.
KPI_SPECS: tuple[tuple[str, str, str], ...] = (
    ("followers", "Followers", "count"),
    ("new_followers", "New followers", "count"),
    ("exposure", "Views & impressions", "count"),
    ("engagements", "Engagements", "count"),
    ("engagement_rate", "Engagement rate", "percent"),
    ("posts", "Posts published", "count"),
)
# Metrics the trend charts can plot, with the label the chart carries.
SERIES_LABELS: dict[str, str] = {
    "exposure": "Views & impressions",
    "engagements": "Engagements",
    "new_followers": "New followers",
}
DEFAULT_SERIES = "exposure"
TOP_POST_LIMIT = 10
TOP_POST_SORTS = ("exposure", "engagements")
POST_FORMATS = ("Text", "Image", "Video", "Carousel")


# ---------------------------------------------------------------------------
# Per-account data
# ---------------------------------------------------------------------------


class AccountData:
    """One account's daily values, ``metric -> {day: value}``."""

    def __init__(self, account: SocialAccount):
        self.account = account
        self.values: dict[str, dict[date, float]] = defaultdict(dict)

    def span(self, metrics: Iterable[str]) -> tuple[date, date] | None:
        days = [d for m in metrics for d in self.values.get(m, ())]
        return (min(days), max(days)) if days else None

    def has(self, metric: str) -> bool:
        return bool(self.values.get(metric))

    def total(self, metrics: Sequence[str], days: Sequence[date]) -> float | None:
        """Sum of ``metrics`` over ``days``; ``None`` when none reported any of them."""
        found = False
        total = 0.0
        for metric in metrics:
            series = self.values.get(metric)
            if not series:
                continue
            for day in days:
                if day in series:
                    found = True
                    total += series[day]
        return total if found else None

    def daily(self, metrics: Sequence[str], days: Sequence[date]) -> list[float | None]:
        """Per-day sums of ``metrics``; ``None`` outside the reported span."""
        span = self.span(metrics)
        if span is None:
            return [None] * len(days)
        out: list[float | None] = []
        for day in days:
            if not span[0] <= day <= span[1]:
                out.append(None)
                continue
            out.append(sum(self.values[m].get(day, 0.0) for m in metrics if m in self.values))
        return out


def exposure_metric(data: AccountData) -> str | None:
    return next((m for m in EXPOSURE_METRICS if data.has(m)), None)


def engagement_parts(data: AccountData) -> list[str]:
    if data.account.platform in NATIVE_ONLY_PLATFORMS:
        return []
    return [m for m in ENGAGEMENT_PARTS if data.has(m)]


def growth_metric(data: AccountData) -> str | None:
    return next((m for m in GROWTH_METRICS if data.has(m)), None)


def load_account_data(accounts: Sequence[SocialAccount], start: date, end: date) -> dict[Any, AccountData]:
    """Daily snapshots for every account over ``[start, end]`` in two queries.

    Mirrors the single-account bundle: account-level rows win, and the per-post
    estimate (:func:`services.sum_post_deltas`) fills in metrics the platform
    gives no account-level series for, or whose per-post data is fresher.
    """
    data = {a.id: AccountData(a) for a in accounts}
    platform_metrics = {a.id: set(PLATFORM_METRICS.get(a.platform, ())) for a in accounts}
    wanted = sorted(set().union(*platform_metrics.values())) if platform_metrics else []
    if not data or not wanted:
        return data

    account_rows = AccountInsightsSnapshot.objects.filter(
        social_account_id__in=list(data),
        metric_key__in=wanted,
        date__gte=start,
        date__lte=end,
    )
    captured = {
        (row["social_account_id"], row["metric_key"]): row["latest"]
        for row in account_rows.values("social_account_id", "metric_key").annotate(latest=Max("captured_at"))
    }
    for account_id, metric, day, value in account_rows.values_list("social_account_id", "metric_key", "date", "value"):
        if metric in platform_metrics[account_id]:
            data[account_id].values[metric][day] = value

    fallback_ids = [a.id for a in accounts if a.platform not in NATIVE_ONLY_PLATFORMS]
    fallback_metrics = [m for m in wanted if services.supports_post_fallback(m)]
    if fallback_ids and fallback_metrics:
        snapshots = PostInsightsSnapshot.objects.filter(
            platform_post__social_account_id__in=fallback_ids,
            metric_key__in=fallback_metrics,
            date__gte=start,
            date__lte=end,
        )
        # Freshness per (account, metric) comes from one aggregate, and the walk
        # reads the publish *date*: decoding two timestamps on every one of the
        # (possibly hundreds of thousands of) rows dominated the page time.
        fallback_captured = {
            (row["platform_post__social_account_id"], row["metric_key"]): row["latest"]
            for row in snapshots.values("platform_post__social_account_id", "metric_key").annotate(
                latest=Max("captured_at")
            )
        }
        post_rows = (
            snapshots.annotate(published_day=TruncDate("platform_post__published_at", tzinfo=UTC))
            .order_by("platform_post__social_account_id", "metric_key", "platform_post_id", "date")
            .values_list(
                "platform_post__social_account_id",
                "metric_key",
                "platform_post_id",
                "published_day",
                "date",
                "value",
            )
        )
        for (account_id, metric), group in groupby(post_rows.iterator(chunk_size=5000), key=lambda r: (r[0], r[1])):
            if metric not in platform_metrics[account_id]:
                continue
            daily, _, observed = services.sum_post_deltas(((*r[2:], None) for r in group), start, end)
            if not observed:
                continue
            if services.prefers_post_series(
                captured.get((account_id, metric)),
                fallback_captured.get((account_id, metric)),
                data[account_id].has(metric),
            ):
                for day in observed:
                    data[account_id].values[metric][day] = daily.get(day, 0.0)
    return data


# ---------------------------------------------------------------------------
# Windows, accounts, KPIs
# ---------------------------------------------------------------------------


@dataclass
class Window:
    """One account's figures for one period; ``None`` means not available."""

    exposure: float | None = None
    engagements: float | None = None
    new_followers: float | None = None
    posts: int = 0
    # Share of the window's days inside the account's reported span (0..1).
    coverage: float = 0.0

    @property
    def rate(self) -> float | None:
        if self.exposure is None or self.engagements is None or self.exposure <= 0:
            return None
        return self.engagements / self.exposure * 100


@dataclass
class AccountMetrics:
    account: SocialAccount
    unavailable: str | None
    followers: int | None
    exposure_label: str
    current: Window
    previous: Window
    data: AccountData | None = field(default=None, repr=False)

    @property
    def comparable(self) -> bool:
        """Whether a change vs. the previous period means something for this account.

        Not when the earlier period is less completely reported than the current
        one (the account was connected, or its history starts, part-way through
        it): the gap would read as growth. A current period that is the less
        complete one (YouTube's trailing days) only understates, so it stays.
        """
        return self.previous.coverage >= self.current.coverage

    def series(self, key: str, days: Sequence[date]) -> list[float | None]:
        """Daily values of ``key`` (see :data:`SERIES_LABELS`) over ``days``."""
        if self.data is None:
            return [None] * len(days)
        if key == "exposure":
            metric = exposure_metric(self.data)
            return self.data.daily([metric], days) if metric else [None] * len(days)
        if key == "engagements":
            parts = engagement_parts(self.data)
            return self.data.daily(parts, days) if parts else [None] * len(days)
        if key == "new_followers":
            return self._follower_gain_series(days)
        raise KeyError(key)

    def _follower_gain_series(self, days: Sequence[date]) -> list[float | None]:
        metric = growth_metric(self.data) if self.data else None
        if metric is None:
            return [None] * len(days)
        if metric != "followers":
            return self.data.daily([metric], days)
        # Lifetime totals: a day's gain is its total minus the previous total.
        totals = self.data.values[metric]
        ordered = sorted(totals)
        previous = dict(zip(ordered[1:], ordered, strict=False))
        return [max(totals[day] - totals[previous[day]], 0.0) if day in previous else None for day in days]


def _window(data: AccountData | None, days: Sequence[date], posts: int) -> Window:
    if data is None:
        return Window(posts=posts)
    metric = exposure_metric(data)
    parts = engagement_parts(data)
    growth = growth_metric(data)
    span = data.span(list(data.values))
    covered = sum(1 for d in days if span[0] <= d <= span[1]) if span else 0
    return Window(
        coverage=covered / len(days) if days else 0.0,
        exposure=data.total([metric], days) if metric else None,
        engagements=data.total(parts, days) if parts else None,
        new_followers=_follower_gain(data, growth, days) if growth else None,
        posts=posts,
    )


def _follower_gain(data: AccountData, metric: str, days: Sequence[date]) -> float | None:
    if metric != "followers":
        return data.total([metric], days)
    window = set(days)
    points = sorted((d, v) for d, v in data.values[metric].items() if d in window)
    return max(points[-1][1] - points[0][1], 0.0) if len(points) >= 2 else None


def _post_counts(accounts: Sequence[SocialAccount], rng: AnalyticsRange, start: date, end: date):
    lo, hi = rng.bounds(start, end)
    rows = (
        PlatformPost.objects.filter(
            social_account_id__in=[a.id for a in accounts],
            status=PlatformPost.Status.PUBLISHED,
            published_at__gte=lo,
            published_at__lt=hi,
        )
        .annotate(day=TruncDate("published_at", tzinfo=rng.tz))
        .values("social_account_id", "day")
        .annotate(n=Count("id"))
    )
    counts: dict[Any, dict[date, int]] = defaultdict(dict)
    for row in rows:
        counts[row["social_account_id"]][row["day"]] = row["n"]
    return counts


def daily_posts(accounts: Sequence[SocialAccount], rng: AnalyticsRange) -> dict[date, int]:
    """Posts published per local day across ``accounts`` within the range."""
    totals: dict[date, int] = defaultdict(int)
    for per_day in _post_counts(accounts, rng, rng.start, rng.end).values():
        for day, n in per_day.items():
            totals[day] += n
    return dict(totals)


def collect(accounts: Sequence[SocialAccount], rng: AnalyticsRange) -> list[AccountMetrics]:
    """Per-account figures for the range and (when comparing) the period before it."""
    enabled = AnalyticsPlatformConfig.enabled_platforms()
    verdicts = {a.id: services.analytics_availability(a.platform, enabled) for a in accounts}
    usable = [a for a in accounts if verdicts[a.id] is None]
    fetch_start = rng.previous_start if rng.compare else rng.start
    data = load_account_data(usable, fetch_start, rng.end)
    counts = _post_counts(accounts, rng, fetch_start, rng.end)

    current_days, previous_days = rng.day_list(), (rng.day_list(rng.previous_start, rng.previous_end))
    rows = []
    for account in accounts:
        account_data = data.get(account.id)
        per_day = counts.get(account.id, {})
        current = _window(account_data, current_days, sum(per_day.get(d, 0) for d in current_days))
        previous = (
            _window(account_data, previous_days, sum(per_day.get(d, 0) for d in previous_days))
            if rng.compare
            else Window()
        )
        metric = exposure_metric(account_data) if account_data else None
        rows.append(
            AccountMetrics(
                account=account,
                unavailable=verdicts[account.id].message if verdicts[account.id] else None,
                followers=account.follower_count if account.follower_count > 0 else None,
                exposure_label=METRICS[metric]["label"] if metric else "",
                current=current,
                previous=previous,
                data=account_data,
            )
        )
    return rows


def _sum(values: Iterable[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return sum(present) if present else None


def _pct(current: float, previous: float) -> float | None:
    return round((current - previous) / previous * 100, 1) if previous else None


def _matched_delta(rows: Sequence[AccountMetrics], getter) -> float | None:
    """% change over the accounts that reported in both periods."""
    pairs = [(getter(r.current), getter(r.previous)) for r in rows if r.comparable]
    pairs = [(c, p) for c, p in pairs if c is not None and p is not None]
    return _pct(sum(c for c, _ in pairs), sum(p for _, p in pairs)) if pairs else None


def _combined_rate(windows: Iterable[Window]) -> float | None:
    both = [w for w in windows if w.rate is not None]
    exposure = sum(w.exposure for w in both)
    return sum(w.engagements for w in both) / exposure * 100 if exposure else None


def _matched_rate_delta(rows: Sequence[AccountMetrics]) -> float | None:
    both = [r for r in rows if r.comparable and r.current.rate is not None and r.previous.rate is not None]
    if not both:
        return None
    return _pct(_combined_rate(r.current for r in both) or 0.0, _combined_rate(r.previous for r in both) or 0.0)


def kpis(rows: Sequence[AccountMetrics], compare: bool) -> list[dict[str, Any]]:
    """Headline figures across ``rows``, each with how many accounts reported."""
    out = []
    for key, label, kind in KPI_SPECS:
        if key == "followers":
            values = [r.followers for r in rows]
            value, previous, delta = _sum(values), None, None
        elif key == "posts":
            values = [r.current.posts for r in rows]
            value = float(sum(values))
            previous = float(sum(r.previous.posts for r in rows)) if compare else None
            # Post counts come from our own records, complete in both periods.
            pairs = [(r.current.posts, r.previous.posts) for r in rows]
            delta = _pct(sum(c for c, _ in pairs), sum(p for _, p in pairs)) if compare else None
        elif key == "engagement_rate":
            values = [r.current.rate for r in rows]
            value = _combined_rate(r.current for r in rows)
            previous = _combined_rate(r.previous for r in rows) if compare else None
            delta = _matched_rate_delta(rows) if compare else None
        else:
            values = [getattr(r.current, key) for r in rows]
            value = _sum(values)
            previous = _sum(getattr(r.previous, key) for r in rows) if compare else None
            delta = _matched_delta(rows, lambda w, k=key: getattr(w, k)) if compare else None
        out.append(
            {
                "key": key,
                "label": label,
                "kind": kind,
                "value": value,
                "previous": previous,
                "delta": delta,
                "reporting": sum(1 for v in values if v is not None),
                "total": len(rows),
            }
        )
    return out


def account_cells(row: AccountMetrics, compare: bool) -> list[dict[str, Any]]:
    """One account's headline figures in :data:`KPI_SPECS` order, with change vs. before."""
    cells = []
    for key, label, kind in KPI_SPECS:
        if key == "followers":
            value, previous = row.followers, None
        elif key == "engagement_rate":
            value, previous = row.current.rate, row.previous.rate
        else:
            value, previous = getattr(row.current, key), getattr(row.previous, key)
        has_delta = (
            compare
            and key != "followers"
            and (key == "posts" or row.comparable)
            and value is not None
            and previous is not None
        )
        cells.append(
            {
                "key": key,
                "label": label,
                "kind": kind,
                "value": value,
                "delta": _pct(value, previous) if has_delta else None,
            }
        )
    return cells


def compare_matrix(rows: Sequence[AccountMetrics], compare: bool) -> list[dict[str, Any]]:
    """Metrics down, accounts across: ``[{label, kind, cells: [{value, delta}]}]``."""
    per_account = [account_cells(r, compare) for r in rows]
    return [
        {"key": key, "label": label, "kind": kind, "cells": [cells[i] for cells in per_account]}
        for i, (key, label, kind) in enumerate(KPI_SPECS)
    ]


def network_breakdown(rows: Sequence[AccountMetrics]) -> list[dict[str, Any]]:
    """One line per network, largest audience reached first."""
    grouped: dict[str, list[AccountMetrics]] = defaultdict(list)
    for row in rows:
        grouped[services.platform_name(row.account)].append(row)
    out = []
    for name, members in grouped.items():
        out.append(
            {
                "name": name,
                "platform": members[0].account.platform,
                "accounts": len(members),
                "followers": _sum(m.followers for m in members),
                "exposure": _sum(m.current.exposure for m in members),
                "engagements": _sum(m.current.engagements for m in members),
                "engagement_rate": _combined_rate(m.current for m in members),
                "new_followers": _sum(m.current.new_followers for m in members),
                "posts": sum(m.current.posts for m in members),
                "unavailable": all(m.unavailable for m in members),
            }
        )
    out.sort(key=lambda r: (r["exposure"] is None, -(r["exposure"] or 0), r["name"]))
    peak = max((r["exposure"] or 0 for r in out), default=0)
    for r in out:
        r["share"] = round((r["exposure"] or 0) / peak * 100) if peak else 0
    return out


def trend(rows: Sequence[AccountMetrics], rng: AnalyticsRange, key: str) -> dict[str, Any]:
    """Workspace-wide daily series for ``key``, plus the previous period aligned by day."""
    days, previous_days = rng.day_list(), rng.day_list(rng.previous_start, rng.previous_end)

    def combined(window_days: Sequence[date]) -> list[float | None]:
        per_account = [r.series(key, window_days) for r in rows]
        return [_sum(day_values) for day_values in zip(*per_account, strict=True)] if per_account else []

    return {
        "key": key,
        "label": SERIES_LABELS[key],
        "kind": "count",
        "labels": [d.isoformat() for d in days],
        "datasets": [
            {"label": "This period", "values": combined(days)},
            *(
                [
                    {
                        "label": "Previous period",
                        "values": combined(previous_days),
                        "muted": True,
                        # The x axis carries this period's dates; tooltips name the earlier day.
                        "dates": [d.isoformat() for d in previous_days],
                    }
                ]
                if rng.compare
                else []
            ),
        ],
    }


def account_trend(rows: Sequence[AccountMetrics], rng: AnalyticsRange, key: str) -> dict[str, Any]:
    """One dataset per account over the range, for the compare page's overlay."""
    days = rng.day_list()
    return {
        "key": key,
        "label": SERIES_LABELS[key],
        "kind": "count",
        "labels": [d.isoformat() for d in days],
        "datasets": [{"label": r.account.account_name, "values": r.series(key, days)} for r in rows],
    }


# ---------------------------------------------------------------------------
# Posts
# ---------------------------------------------------------------------------


def post_format(post: PlatformPost) -> str:
    """Text / Image / Video / Carousel, from the post's attachments."""
    attachments = list(post.post.media_attachments.all())
    if not attachments:
        return "Text"
    if len(attachments) > 1:
        return "Carousel"
    return "Video" if attachments[0].media_asset.is_video else "Image"


def posts_queryset(accounts: Sequence[SocialAccount], rng: AnalyticsRange):
    lo, hi = rng.bounds()
    return (
        PlatformPost.objects.filter(
            social_account_id__in=[a.id for a in accounts],
            status=PlatformPost.Status.PUBLISHED,
            published_at__gte=lo,
            published_at__lt=hi,
        )
        .select_related("post")
        # Every post in the range is loaded (formats average all of them), so
        # only the columns the rows read: Post is a wide model.
        .only(
            "id",
            "post_id",
            "social_account_id",
            "status",
            "published_at",
            "platform_specific_caption",
            "post__id",
            "post__caption",
        )
        .order_by("-published_at", "id")
    )


def post_row(post: PlatformPost, stats: dict[str, float], tz) -> dict[str, Any]:
    """One post with the same exposure / engagement rules as the account rollup."""
    platform = post.social_account.platform
    metric = next((m for m in EXPOSURE_METRICS if m in stats), None)
    parts = [] if platform in NATIVE_ONLY_PLATFORMS else [m for m in ENGAGEMENT_PARTS if m in stats]
    exposure = stats[metric] if metric else None
    engagements = sum(stats[m] for m in parts) if parts else None
    rate = engagements / exposure * 100 if exposure and engagements is not None else None
    return {
        "post": post,
        "account": post.social_account,
        "caption": (post.platform_specific_caption or post.post.caption or "").strip(),
        "published_at": post.published_at.astimezone(tz),
        "format": post_format(post),
        "exposure": exposure,
        "exposure_label": METRICS[metric]["label"] if metric else "",
        "engagements": engagements,
        "rate": rate,
        "stats": stats,
    }


def post_rows(posts: Sequence[PlatformPost], accounts: Sequence[SocialAccount], tz) -> list[dict[str, Any]]:
    """Rows for :func:`posts_queryset` results, reusing the already-loaded ``accounts``.

    Joining ``social_account`` per post would build (and decrypt the tokens of)
    one account instance per post; there are only ever a handful of accounts.
    """
    by_id = {a.id: a for a in accounts}
    for p in posts:
        p.social_account = by_id[p.social_account_id]
    metrics = sorted({m for p in posts for m in post_metrics_for(p.social_account.platform)})
    stats = services._latest_post_stats([p.id for p in posts], metrics)
    return [post_row(p, stats.get(p.id, {}), tz) for p in posts]


def top_posts(rows: Sequence[dict[str, Any]], sort: str, limit: int = TOP_POST_LIMIT) -> list[dict[str, Any]]:
    """Best posts by ``sort``; a post with no such figure isn't ranked at all."""
    sort = sort if sort in TOP_POST_SORTS else TOP_POST_SORTS[0]
    ranked = [r for r in rows if r[sort] is not None]
    ranked.sort(key=lambda r: r[sort], reverse=True)
    return ranked[:limit]


def format_performance(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Average results per post format, best average engagement first."""
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["format"]].append(row)
    out = []
    for name in POST_FORMATS:
        members = grouped.get(name)
        if not members:
            continue
        exposures = [m["exposure"] for m in members if m["exposure"] is not None]
        engagements = [m["engagements"] for m in members if m["engagements"] is not None]
        out.append(
            {
                "format": name,
                "posts": len(members),
                "avg_exposure": sum(exposures) / len(exposures) if exposures else None,
                "avg_engagements": sum(engagements) / len(engagements) if engagements else None,
                "reporting": len(engagements),
            }
        )
    out.sort(key=lambda r: (r["avg_engagements"] is None, -(r["avg_engagements"] or 0)))
    return out


def overview(
    accounts: Sequence[SocialAccount],
    rng: AnalyticsRange,
    *,
    series: str = DEFAULT_SERIES,
    top_sort: str = TOP_POST_SORTS[0],
) -> dict[str, Any]:
    """Everything the overview page, the export and the API need, in one shot."""
    rows = collect(accounts, rng)
    posts = list(posts_queryset(accounts, rng).prefetch_related("post__media_attachments__media_asset"))
    post_data = post_rows(posts, accounts, rng.tz)
    series = series if series in SERIES_LABELS else DEFAULT_SERIES
    return {
        "range": rng,
        "accounts": rows,
        "kpis": kpis(rows, rng.compare),
        "networks": network_breakdown(rows),
        "trend": trend(rows, rng, series),
        "top_posts": top_posts(post_data, top_sort),
        "top_sort": top_sort if top_sort in TOP_POST_SORTS else TOP_POST_SORTS[0],
        "formats": format_performance(post_data),
        "post_count": len(post_data),
    }
