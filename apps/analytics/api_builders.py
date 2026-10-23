"""Compose analytics service output into the agent-API response schemas.

Both the REST router (``apps/api/routers/analytics.py``) and the MCP
handlers (``apps/mcp/handlers.py``) call into this module so the two
surfaces emit identical bodies. The REST router only adds HTTP-shaped
concerns (rate limits, audit logging); the actual model → schema work
lives here.

Routers in this codebase are pure dispatch, so MCP imports from this
module rather than from the router — mirroring how ``apps.mcp.handlers``
already pulls ``PostResponse`` straight from ``apps.api.schemas`` for
parity-safe serialization.
"""

from __future__ import annotations

from apps.api.schemas import (
    AccountAnalyticsResponse,
    DerivedMetricResponse,
    EngagementCardResponse,
    OverviewAccountResponse,
    OverviewDayResponse,
    OverviewMetricResponse,
    OverviewNetworkResponse,
    OverviewPostResponse,
    PlatformPostAnalyticsResponse,
    PostAnalyticsResponse,
    PostMetricTileResponse,
    WorkspaceOverviewResponse,
)
from apps.composer.models import Post
from apps.social_accounts.models import AnalyticsPlatformConfig, SocialAccount
from apps.workspaces.models import Workspace

from . import rollup
from .freshness import account_freshness, post_freshness
from .ranges import AnalyticsRange
from .services import (
    _label,
    account_analytics_bundle,
    engagement_card,
    follower_growth_metric,
    hero_cards,
    post_detail,
    unavailable_reason,
)


def build_account_analytics(account: SocialAccount, days: int) -> AccountAnalyticsResponse:
    """Assemble the per-channel analytics response.

    For platforms with no live analytics — either inherently
    (:data:`NO_ANALYTICS_PLATFORMS`) or because an admin disabled the
    platform in :class:`AnalyticsPlatformConfig` — returns an
    ``analytics_available: false`` envelope with empty metric arrays and
    no freshness ETA, and issues no snapshot queries. Otherwise composes
    hero metrics, engagement, follower growth and freshness from the
    existing service layer.
    """
    reason = unavailable_reason(account.platform)
    if reason is not None:
        return AccountAnalyticsResponse(
            account_id=account.id,
            platform=account.platform,
            account_name=account.account_name,
            connection_status=account.connection_status,
            days=days,
            analytics_available=False,
            unavailable_reason=reason,
            hero_metrics=[],
            engagement=None,
            follower_growth=None,
            captured_at=None,
            next_sync_eta=None,
        )

    # Single-pass snapshot fetch — feed the same bundle to
    # hero_cards / engagement_card / follower_growth so they don't each
    # re-issue the per-metric SELECTs. Also recovers ``captured_at`` from
    # the same scan, so ``account_freshness`` skips its ``Max`` aggregate.
    bundle = account_analytics_bundle(account, days)
    captured_at, next_sync_eta = account_freshness(
        account,
        last_captured_at=bundle["max_captured_at"],
        have_last_captured_at=True,
    )
    hero = [
        DerivedMetricResponse.from_derived(card["metric"], card["label"], card["derived"])
        for card in hero_cards(account, days, bundle=bundle)
        if card["derived"].available
    ]
    engagement_card_payload = engagement_card(account, days, bundle=bundle)
    engagement = None
    if engagement_card_payload is not None:
        rate = engagement_card_payload["rate"]
        engagement = EngagementCardResponse(
            rate=DerivedMetricResponse.from_derived("engagement", _label("engagement"), rate, calculated=True),
            parts=[
                DerivedMetricResponse.from_derived(part["metric"], part["label"], part["derived"])
                for part in engagement_card_payload["parts"]
            ],
            formula=engagement_card_payload["formula"],
        )
    growth_pair = follower_growth_metric(account, days, bundle=bundle)
    growth_response = None
    if growth_pair is not None:
        growth_key, growth = growth_pair
        growth_response = DerivedMetricResponse.from_derived(growth_key, _label(growth_key), growth)

    return AccountAnalyticsResponse(
        account_id=account.id,
        platform=account.platform,
        account_name=account.account_name,
        connection_status=account.connection_status,
        days=days,
        analytics_available=True,
        unavailable_reason=None,
        hero_metrics=hero,
        engagement=engagement,
        follower_growth=growth_response,
        captured_at=captured_at,
        next_sync_eta=next_sync_eta,
        reporting_period=bundle.get("reporting_period"),
    )


def build_post_analytics(post: Post) -> PostAnalyticsResponse:
    """Assemble per-platform analytics for every child of a Post.

    Each ``PlatformPost`` gets its own envelope so a mixed-platform post
    (e.g. one Threads child + one Bluesky child) reports
    ``analytics_available`` independently per platform.
    """
    # ``_get_workspace_post`` (REST) and ``_get_post_for_key`` (MCP) already
    # prefetch ``platform_posts__social_account``; calling ``.all()`` here
    # serves from that cache. ``.select_related(...)`` would create a fresh
    # queryset that bypasses the prefetch and fires an extra query.
    children = list(post.platform_posts.all())
    # Resolve the admin-configured enable list once, not per child, so a
    # mixed-platform post stays at a single config query.
    enabled_platforms = AnalyticsPlatformConfig.enabled_platforms()
    return PostAnalyticsResponse(
        post_id=post.id,
        workspace_id=post.workspace_id,
        title=post.title,
        caption=post.caption,
        platform_posts=[_build_platform_post_analytics(child, enabled_platforms) for child in children],
    )


def _build_platform_post_analytics(platform_post, enabled_platforms: list[str]) -> PlatformPostAnalyticsResponse:
    account = platform_post.social_account
    reason = unavailable_reason(account.platform, enabled_platforms)

    # Short-circuit unavailable-platform and draft/scheduled cases BEFORE
    # any snapshot queries — drafts have nothing to fetch, and unavailable
    # platforms never have snapshots in the first place.
    if reason is not None:
        return PlatformPostAnalyticsResponse(
            platform_post_id=platform_post.id,
            social_account_id=account.id,
            platform=account.platform,
            status=platform_post.status,
            published_at=platform_post.published_at,
            analytics_available=False,
            unavailable_reason=reason,
            metric_tiles=[],
            captured_at=None,
            next_sync_eta=None,
        )
    if not platform_post.published_at:
        return PlatformPostAnalyticsResponse(
            platform_post_id=platform_post.id,
            social_account_id=account.id,
            platform=account.platform,
            status=platform_post.status,
            published_at=None,
            analytics_available=True,
            unavailable_reason=None,
            metric_tiles=[],
            captured_at=None,
            next_sync_eta=None,
        )

    detail = post_detail(platform_post)
    captured_at, next_sync_eta = post_freshness(
        platform_post,
        last_captured_at=detail["captured_at"],
        have_last_captured_at=True,
    )
    tiles = [
        PostMetricTileResponse(
            key=tile["key"],
            label=tile["label"],
            kind=tile["kind"],
            value=tile["value"],
            series=list(tile["sparkline"]),
            is_primary=tile["is_primary"],
        )
        for tile in detail["metric_tiles"]
        if tile["value"] is not None
    ]
    return PlatformPostAnalyticsResponse(
        platform_post_id=platform_post.id,
        social_account_id=account.id,
        platform=account.platform,
        status=platform_post.status,
        published_at=platform_post.published_at,
        analytics_available=True,
        unavailable_reason=None,
        metric_tiles=tiles,
        captured_at=captured_at,
        next_sync_eta=next_sync_eta,
    )


def connected_accounts(api_key) -> list[SocialAccount]:
    """The key's allowlisted accounts that are connected, in display order.

    Goes through ``.all()`` because the OAuth actor's allowlist is a shim that
    only offers that.
    """
    connected = [
        a
        for a in api_key.social_accounts.all()
        # The allowlist is validated at issuance; re-checking the workspace keeps
        # an account later moved elsewhere out of this workspace's totals.
        if a.connection_status == SocialAccount.ConnectionStatus.CONNECTED and a.workspace_id == api_key.workspace_id
    ]
    return sorted(connected, key=lambda a: (a.platform, a.account_name))


_NETWORK_KEYS = ("followers", "new_followers", "exposure", "engagements", "engagement_rate", "posts")


def build_workspace_overview(
    workspace: Workspace, accounts: list[SocialAccount], rng: AnalyticsRange, top_sort: str = "exposure"
) -> WorkspaceOverviewResponse:
    """Assemble the workspace rollup for the agent API and MCP.

    ``accounts`` is whatever the caller is allowed to see (an API key's
    allowlist), so totals never include an account the key can't read.
    """
    data = rollup.overview(accounts, rng, top_sort=top_sort)
    trends = {key: rollup.trend(data["accounts"], rng, key)["datasets"][0]["values"] for key in rollup.SERIES_LABELS}
    return WorkspaceOverviewResponse(
        workspace_id=workspace.id,
        start=rng.start,
        end=rng.end,
        timezone=str(rng.tz),
        compare=rng.compare,
        previous_start=rng.previous_start if rng.compare else None,
        previous_end=rng.previous_end if rng.compare else None,
        metrics=[
            OverviewMetricResponse(
                key=k["key"],
                label=k["label"],
                kind=k["kind"],
                value=k["value"],
                previous=k["previous"],
                delta=k["delta"],
                accounts_reporting=k["reporting"],
                accounts_total=k["total"],
            )
            for k in data["kpis"]
        ],
        networks=[
            OverviewNetworkResponse(
                platform=n["platform"],
                name=n["name"],
                accounts=n["accounts"],
                metrics={key: n[key] for key in _NETWORK_KEYS},
            )
            for n in data["networks"]
        ],
        accounts=[
            OverviewAccountResponse(
                account_id=row.account.id,
                platform=row.account.platform,
                account_name=row.account.account_name,
                analytics_available=row.unavailable is None,
                unavailable_reason=row.unavailable,
                metrics={cell["key"]: cell["value"] for cell in rollup.account_cells(row, False)},
            )
            for row in data["accounts"]
        ],
        daily=[
            OverviewDayResponse(
                date=day,
                exposure=trends["exposure"][i],
                engagements=trends["engagements"][i],
                new_followers=trends["new_followers"][i],
            )
            for i, day in enumerate(rng.day_list())
        ],
        top_posts=[
            OverviewPostResponse(
                post_id=r["post"].post_id,
                platform_post_id=r["post"].id,
                social_account_id=r["account"].id,
                platform=r["account"].platform,
                caption=r["caption"],
                published_at=r["post"].published_at,
                format=r["format"],
                exposure=r["exposure"],
                engagements=r["engagements"],
                engagement_rate=r["rate"],
            )
            for r in data["top_posts"]
        ],
    )
