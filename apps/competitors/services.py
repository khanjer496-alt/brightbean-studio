"""Competitor benchmarking: adding profiles, daily snapshots, and the comparison tables.

Only networks whose provider sets ``supports_public_profile_metrics`` appear
here, and only through the customer's own connected account on that network.
Nothing on this page comes from scraping: a network without an official route
is listed as "not available" with the reason its provider gives.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.analytics import rollup
from apps.analytics.ranges import AnalyticsRange, local_today
from apps.billing.entitlements import PlanLimitError, check_can_add_competitor, get_entitlements
from apps.social_accounts.models import SocialAccount
from providers import PROVIDER_REGISTRY
from providers.exceptions import (
    PublicProfileNotFoundError,
    QuotaExceededError,
    RateLimitError,
    TokenExpiredError,
)
from providers.types import PublicProfileMetrics

from .models import Competitor, CompetitorSnapshot

logger = logging.getLogger(__name__)

# The chart shows you plus this many competitors (the chart palette has four series).
CHART_COMPETITORS = 3
_YOUTUBE_CHANNEL_ID = re.compile(r"UC[A-Za-z0-9_-]{22}")


class CompetitorError(Exception):
    """A refused or failed competitor action; ``str(exc)`` is written for the person who tried."""

    def __init__(self, message: str, *, code: str = "competitor_error", status: int = 422):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


# ---------------------------------------------------------------------------
# Networks
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Network:
    platform: str
    name: str
    supported: bool
    reason: str
    handle_hint: str


def network_for(platform: str) -> Network | None:
    cls = PROVIDER_REGISTRY.get(platform)
    if cls is None:
        return None
    return Network(
        platform=platform,
        name=cls({}).platform_name,
        supported=cls.supports_public_profile_metrics,
        reason="" if cls.supports_public_profile_metrics else cls.public_profile_unavailable_reason,
        handle_hint=cls.competitor_handle_hint,
    )


def supported_networks() -> list[Network]:
    nets = [network_for(p) for p in PROVIDER_REGISTRY]
    return sorted((n for n in nets if n and n.supported), key=lambda n: n.name)


def unsupported_networks(workspace) -> list[Network]:
    """Networks the workspace has connected that cannot be benchmarked, with the reason."""
    platforms = SocialAccount.objects.filter(workspace=workspace).values_list("platform", flat=True).distinct()
    nets = [network_for(p) for p in platforms]
    return sorted((n for n in nets if n and not n.supported), key=lambda n: n.name)


def canonical_handle(platform: str, handle: str) -> str:
    """The handle as stored: lowercase, except a YouTube channel id, which is case-sensitive."""
    cls = PROVIDER_REGISTRY[platform]
    clean = cls.normalize_competitor_handle(handle)
    if not clean or _YOUTUBE_CHANNEL_ID.fullmatch(clean):
        return clean
    return clean.lower()


# ---------------------------------------------------------------------------
# Reading a profile
# ---------------------------------------------------------------------------


def using_account(workspace, platform: str, allowed_ids=None) -> SocialAccount | None:
    """A connected account on ``platform`` whose access can read public profiles."""
    accounts = SocialAccount.objects.filter(
        workspace=workspace, platform=platform, connection_status=SocialAccount.ConnectionStatus.CONNECTED
    ).order_by("created_at")
    if allowed_ids is not None:
        accounts = accounts.filter(id__in=allowed_ids)
    return accounts.filter(analytics_needs_reconnect=False).first() or accounts.first()


def fetch_profile(platform: str, handle: str, account: SocialAccount) -> PublicProfileMetrics:
    """Read ``handle``'s public numbers as ``account``; failures become a ``CompetitorError``."""
    from apps.analytics.tasks import _analytics_provider_and_token

    network = network_for(platform)
    name = network.name if network else platform
    try:
        provider, access_token = _analytics_provider_and_token(account)
        return provider.public_profile_metrics(handle, account, access_token)
    except PublicProfileNotFoundError as exc:
        raise CompetitorError(str(exc), code="competitor_not_found", status=404) from exc
    except (RateLimitError, QuotaExceededError) as exc:
        raise CompetitorError(
            f"{name} is limiting requests right now. Try again later.", code="competitor_rate_limited", status=429
        ) from exc
    except TokenExpiredError as exc:
        raise CompetitorError(
            f"{name} rejected the connected account's access. Reconnect {account.account_name or name} and try again.",
            code="competitor_reconnect",
        ) from exc
    except Exception as exc:
        logger.warning("competitors: %s profile read failed for %s: %s", platform, handle, exc)
        raise CompetitorError(f"Couldn't read that {name} profile right now. Try again later.") from exc


# ---------------------------------------------------------------------------
# Adding, removing, snapshotting
# ---------------------------------------------------------------------------


def _store_snapshot(competitor: Competitor, metrics: PublicProfileMetrics, day: date) -> CompetitorSnapshot:
    snapshot, _ = CompetitorSnapshot.objects.update_or_create(
        competitor=competitor,
        date=day,
        defaults={
            "followers": metrics.followers,
            "posts_count": metrics.posts_count,
            "avg_engagement": metrics.avg_engagement,
            "posts_per_week": metrics.posts_per_week,
            "sampled_posts": metrics.sampled_posts,
        },
    )
    Competitor.objects.filter(pk=competitor.pk).update(
        last_synced_at=timezone.now(),
        last_error="",
        display_name=metrics.display_name or competitor.display_name,
        profile_url=metrics.profile_url or competitor.profile_url,
    )
    return snapshot


def add_competitor(workspace, platform: str, handle: str, user=None, *, allowed_account_ids=None) -> Competitor:
    """Track ``handle`` on ``platform``: plan limit, then a first read that proves the profile exists."""
    network = network_for(platform)
    if network is None or not network.supported:
        reason = network.reason if network else "Unknown network."
        raise CompetitorError(reason, code="competitor_unsupported")
    clean = canonical_handle(platform, handle)
    if not clean:
        raise CompetitorError(
            f"That doesn't look like a {network.name} handle. Try something like {network.handle_hint}.",
            code="competitor_bad_handle",
        )
    try:
        check_can_add_competitor(workspace, platform)
    except PlanLimitError as exc:
        raise CompetitorError(str(exc), code=exc.code, status=403) from exc
    if Competitor.objects.filter(workspace=workspace, platform=platform, handle=clean).exists():
        raise CompetitorError(
            f"You're already tracking {clean} on {network.name}.", code="competitor_duplicate", status=409
        )
    account = using_account(workspace, platform, allowed_account_ids)
    if account is None:
        raise CompetitorError(
            f"Connect a {network.name} account to this workspace first: it's what reads other profiles' public numbers.",
            code="competitor_no_account",
        )
    metrics = fetch_profile(platform, clean, account)
    try:
        with transaction.atomic():
            competitor = Competitor.objects.create(
                workspace=workspace,
                platform=platform,
                handle=clean,
                display_name=metrics.display_name,
                profile_url=metrics.profile_url,
                added_by=user if getattr(user, "is_authenticated", False) else None,
            )
            _store_snapshot(competitor, metrics, timezone.now().date())
    except IntegrityError as exc:
        raise CompetitorError(
            f"You're already tracking {clean} on {network.name}.", code="competitor_duplicate", status=409
        ) from exc
    competitor.refresh_from_db()
    return competitor


def remove_competitor(workspace, competitor_id) -> None:
    deleted, _ = Competitor.objects.filter(workspace=workspace, id=competitor_id).delete()
    if not deleted:
        raise CompetitorError("Competitor not found.", code="competitor_missing", status=404)


def snapshot_competitor(competitor: Competitor, account: SocialAccount, *, today: date | None = None) -> bool:
    """Read and store today's numbers; False (and ``last_error`` set) when the read failed."""
    try:
        metrics = fetch_profile(competitor.platform, competitor.handle, account)
    except CompetitorError as exc:
        Competitor.objects.filter(pk=competitor.pk).update(last_error=exc.message[:300])
        if exc.code in ("competitor_rate_limited",):
            raise
        return False
    _store_snapshot(competitor, metrics, today or timezone.now().date())
    return True


def snapshot_all(*, today: date | None = None, force: bool = False) -> dict[str, int]:
    """One snapshot per competitor per day. Idempotent: a competitor already read today is skipped.

    Competitors on a plan that no longer includes benchmarking, or in a workspace with no
    connected account on the network, are skipped, not deleted.
    """
    today = today or timezone.now().date()
    done = set(CompetitorSnapshot.objects.filter(date=today).values_list("competitor_id", flat=True))
    stats = {"read": 0, "skipped": 0, "failed": 0}
    blocked_platforms: set[tuple[Any, str]] = set()
    accounts: dict[tuple[Any, str], SocialAccount | None] = {}
    entitled: dict[Any, bool] = {}
    for competitor in Competitor.objects.select_related("workspace__organization").order_by("created_at"):
        if competitor.id in done and not force:
            stats["skipped"] += 1
            continue
        org = competitor.workspace.organization
        if org.pk not in entitled:
            limit = get_entitlements(org).max_competitors_per_network
            entitled[org.pk] = limit is None or limit > 0
        key = (competitor.workspace_id, competitor.platform)
        if not entitled[org.pk] or key in blocked_platforms:
            stats["skipped"] += 1
            continue
        if key not in accounts:
            accounts[key] = using_account(competitor.workspace, competitor.platform)
        account = accounts[key]
        if account is None:
            stats["skipped"] += 1
            continue
        try:
            ok = snapshot_competitor(competitor, account, today=today)
        except CompetitorError:
            blocked_platforms.add(key)
            stats["failed"] += 1
            continue
        stats["read" if ok else "failed"] += 1
    return stats


def prune_snapshots(days: int = 800) -> int:
    deleted, _ = CompetitorSnapshot.objects.filter(date__lt=timezone.now().date() - timedelta(days=days)).delete()
    return deleted


# ---------------------------------------------------------------------------
# Math
# ---------------------------------------------------------------------------


def growth_pct(start: float | None, end: float | None) -> float | None:
    """Percent change from ``start`` to ``end``; None when either is unknown or the start is not positive."""
    if start is None or end is None or start <= 0:
        return None
    return round((end - start) / start * 100, 2)


def _window_growth(points: Sequence[tuple[date, float | None]], rng: AnalyticsRange) -> float | None:
    """Follower growth across the range from dated points: baseline is the last point on or before the
    start (else the first inside the range), end is the last point inside it. Needs two distinct dates."""
    known = sorted((d, v) for d, v in points if v is not None and d <= rng.end)
    before = [p for p in known if p[0] <= rng.start]
    inside = [p for p in known if rng.start < p[0] <= rng.end]
    baseline = before[-1] if before else (inside[0] if inside else None)
    last = inside[-1] if inside else (before[-1] if before else None)
    if baseline is None or last is None or baseline[0] == last[0]:
        return None
    return growth_pct(baseline[1], last[1])


def _posts_per_week(count: int, rng: AnalyticsRange) -> float:
    return round(count / (rng.days / 7), 2)


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


def _own_row(row: rollup.AccountMetrics, rng: AnalyticsRange) -> dict[str, Any]:
    account = row.account
    gain = row.current.new_followers
    followers = row.followers
    growth = growth_pct(followers - gain, followers) if followers is not None and gain is not None else None
    posts, engagements = row.current.posts, row.current.engagements
    return {
        "kind": "you",
        "id": str(account.id),
        "label": account.account_name or account.account_handle,
        "handle": account.account_handle,
        "profile_url": "",
        "followers": followers,
        "growth": growth,
        "posts_per_week": _posts_per_week(posts, rng),
        "avg_engagement": round(engagements / posts, 2) if engagements is not None and posts else None,
        "last_synced": None,
        "error": row.unavailable or "",
    }


def _competitor_row(competitor: Competitor, snapshots: list[CompetitorSnapshot], rng: AnalyticsRange) -> dict[str, Any]:
    latest = snapshots[-1] if snapshots else None
    return {
        "kind": "competitor",
        "id": str(competitor.id),
        "label": competitor.label,
        "handle": competitor.handle,
        "profile_url": competitor.profile_url,
        "followers": latest.followers if latest else None,
        "growth": _window_growth([(s.date, s.followers) for s in snapshots], rng),
        "posts_per_week": latest.posts_per_week if latest else None,
        "avg_engagement": latest.avg_engagement if latest else None,
        "last_synced": latest.date if latest else None,
        "error": competitor.last_error,
    }


def _snapshots_by_competitor(
    competitors: Sequence[Competitor], rng: AnalyticsRange
) -> dict[Any, list[CompetitorSnapshot]]:
    """Each competitor's snapshots up to the range's end, with one baseline point before its start."""
    grouped: dict[Any, list[CompetitorSnapshot]] = defaultdict(list)
    ids = [c.id for c in competitors]
    # The baseline reads at most 14 days back; older than that, "growth" would span a gap in tracking.
    for snap in CompetitorSnapshot.objects.filter(
        competitor_id__in=ids, date__gte=rng.start - timedelta(days=14), date__lte=rng.end
    ).order_by("date"):
        grouped[snap.competitor_id].append(snap)
    return grouped


def comparison(workspace, rng: AnalyticsRange, accounts: Sequence[SocialAccount]) -> dict[str, Any]:
    """Per network: your accounts' figures beside the competitors', for the range."""
    competitors = list(Competitor.objects.filter(workspace=workspace))
    snapshots = _snapshots_by_competitor(competitors, rng)
    supported = {n.platform for n in supported_networks()}
    own = rollup.collect([a for a in accounts if a.platform in supported], rng)
    groups: list[dict[str, Any]] = []
    for network in supported_networks():
        mine = [_own_row(r, rng) for r in own if r.account.platform == network.platform]
        theirs = [
            _competitor_row(c, snapshots.get(c.id, []), rng) for c in competitors if c.platform == network.platform
        ]
        theirs.sort(key=lambda r: (r["followers"] is None, -(r["followers"] or 0)))
        if mine or theirs:
            groups.append(
                {
                    "network": network,
                    "you": mine,
                    "competitors": theirs,
                    "can_add": using_account(workspace, network.platform) is not None,
                }
            )
    return {"groups": groups, "unsupported": unsupported_networks(workspace)}


def trend(workspace, rng: AnalyticsRange, platform: str, accounts: Sequence[SocialAccount]) -> dict[str, Any] | None:
    """Follower growth since the start of the range: all your accounts on the network, and up to three competitors.

    Each line is indexed to its own first reading, so a 500-follower profile and a 500,000-follower one
    can be compared by pace. None when no line has two readings.
    """
    days = rng.day_list()
    iso = [d.isoformat() for d in days]
    datasets: list[dict[str, Any]] = []

    def indexed(by_day: dict[date, float]) -> list[float | None]:
        known = [by_day[d] for d in days if d in by_day and by_day[d] is not None]
        if len(known) < 2 or known[0] <= 0:
            return [None] * len(days)
        base = known[0]
        return [round((by_day[d] - base) / base * 100, 2) if d in by_day else None for d in days]

    mine = [r for r in rollup.collect([a for a in accounts if a.platform == platform], rng) if r.data]
    combined: dict[date, float] = defaultdict(float)
    counts: dict[date, int] = defaultdict(int)
    for row in mine:
        for day, value in row.data.values.get("followers", {}).items():
            combined[day] += value
            counts[day] += 1
    combined = {d: v for d, v in combined.items() if counts[d] == len(mine) and rng.start <= d <= rng.end}
    your_values = indexed(combined)
    if any(v is not None for v in your_values):
        datasets.append({"label": "You", "values": your_values})

    competitors = list(Competitor.objects.filter(workspace=workspace, platform=platform))
    snapshots = _snapshots_by_competitor(competitors, rng)
    ranked = sorted(competitors, key=lambda c: -(snapshots[c.id][-1].followers or 0) if snapshots.get(c.id) else 0)
    for competitor in ranked[:CHART_COMPETITORS]:
        by_day = {s.date: s.followers for s in snapshots.get(competitor.id, []) if s.followers is not None}
        values = indexed({d: v for d, v in by_day.items() if rng.start <= d <= rng.end})
        if any(v is not None for v in values):
            datasets.append({"label": competitor.label, "values": values})
    if not datasets:
        return None
    return {
        "key": "followers_growth",
        "label": "Follower growth",
        "kind": "percent",
        "labels": iso,
        "datasets": datasets,
    }


def trend_summary(chart: dict[str, Any]) -> str:
    parts = []
    for dataset in chart["datasets"]:
        known = [v for v in dataset["values"] if v is not None]
        parts.append(f"{dataset['label']}: {known[-1]:+.1f}%" if known else f"{dataset['label']}: no data")
    return (
        f"{chart['label']} since {chart['labels'][0]}, indexed to each profile's first reading. "
        + "; ".join(parts)
        + "."
    )


def overview_section(workspace, rng: AnalyticsRange, accounts: Sequence[SocialAccount]) -> dict[str, Any]:
    """The short competitors block on the analytics overview: one line per network that has any."""
    comp = comparison(workspace, rng, accounts)
    rows = []
    for group in comp["groups"]:
        if not group["competitors"]:
            continue
        you = group["you"]
        your_followers = sum(r["followers"] or 0 for r in you) if you else None
        top = next((r for r in group["competitors"] if r["followers"] is not None), None)
        growths = [r["growth"] for r in group["competitors"] if r["growth"] is not None]
        rows.append(
            {
                "network": group["network"],
                "your_followers": your_followers,
                "competitors": len(group["competitors"]),
                "top": top,
                "avg_growth": round(sum(growths) / len(growths), 2) if growths else None,
                "your_growth": next((r["growth"] for r in you if r["growth"] is not None), None)
                if len(you) == 1
                else None,
            }
        )
    return {
        "rows": rows,
        "tracking": Competitor.objects.filter(workspace=workspace).exists(),
        "enabled": (get_entitlements(workspace.organization).max_competitors_per_network or 0) != 0,
    }


def freshness_note(competitors: Sequence[Competitor]) -> datetime | None:
    stamps = [c.last_synced_at for c in competitors if c.last_synced_at]
    return max(stamps) if stamps else None


def today_for(workspace) -> date:
    from apps.analytics.ranges import workspace_tzinfo

    return local_today(workspace_tzinfo(workspace))
