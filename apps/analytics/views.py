"""Analytics page views.

Three workspace-scoped pages share one date-range control (see ``ranges``):
the workspace overview, a side-by-side comparison of 2-4 accounts, and the
single-account page in the design's Classic layout. HTMX partials handle chart
metric chips, sort/filter/page changes on the All-posts table, and the
post-detail drawer fetch. CSV exports live in ``exports``.
"""

from __future__ import annotations

import logging
from uuid import UUID

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET

from apps.composer.models import PlatformPost
from apps.members.models import WorkspaceMembership
from apps.social_accounts.models import AnalyticsPlatformConfig, SocialAccount
from apps.workspaces.models import Workspace

from . import exports, rollup, services
from .metrics import PLATFORM_COLOR, PLATFORM_PRIMARY
from .ranges import PRESET_DAYS, AnalyticsRange, local_today, parse_range

logger = logging.getLogger(__name__)

COMPARE_MIN_ACCOUNTS = 2
COMPARE_MAX_ACCOUNTS = 4


@login_required
@require_GET
def analytics_status(request, workspace_id, account_id):
    workspace = _get_workspace(request, workspace_id)
    account = get_object_or_404(SocialAccount, pk=account_id, workspace=workspace, platform="instagram")
    return render(request, "analytics/_instagram_status.html", {"account": account, "workspace_id": workspace.id})


def _get_workspace(request, workspace_id):
    """The workspace, if the user is a member whose role may view analytics.

    Shared by every analytics view and the CSV exports, so "can see the page"
    and "can download its numbers" can't drift apart.
    """
    workspace = get_object_or_404(Workspace, id=workspace_id)
    membership = WorkspaceMembership.objects.filter(user=request.user, workspace=workspace).first()
    if membership is None:
        raise PermissionDenied("You are not a member of this workspace.")
    if not membership.effective_permissions.get("view_analytics", False):
        raise PermissionDenied("Permission denied: view_analytics")
    return workspace


def _analytics_accounts(workspace, enabled_platforms):
    """Every connected account in the workspace, annotated with why (if at all)
    analytics is unavailable for it.

    Deliberately unfiltered by platform: an account whose platform is switched
    off in ``AnalyticsPlatformConfig`` used to be dropped here, so it vanished
    from the account switcher with nothing anywhere saying why. It stays in the
    list now and carries its verdict, which the switcher and the page body both
    render.
    """
    accounts = list(
        SocialAccount.objects.filter(
            workspace=workspace,
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        ).order_by("platform", "account_name")
    )
    for account in accounts:
        verdict = services.analytics_availability(account.platform, enabled_platforms)
        account.analytics_verdict = verdict
        account.analytics_unavailable_reason = verdict.message if verdict else None
    return accounts


def _preferred_account(accounts):
    """The account to land on: the first with working analytics, else the first.

    Shared by the index redirect and the unknown-``account_id`` fallback so the
    two ways of arriving without a specific account agree. Falling back to
    ``accounts[0]`` rather than the no-accounts page means a workspace whose
    only account is unavailable still gets the page explaining why.
    """
    return next((a for a in accounts if a.analytics_verdict is None), accounts[0])


def _parse_days_filter(value: str | None) -> int | None:
    """Legacy All-posts date filter in old URLs: 7|30|90 days (else no day limit)."""
    try:
        n = int(value or "")
    except (TypeError, ValueError):
        return None
    return n if n in PRESET_DAYS else None


def _table_window(value: str | None, rng: AnalyticsRange) -> dict:
    """All-posts date scope: the page range by default, ``all`` for all time.

    Old links carried ``table_range=7|30|90``; they keep meaning "that many
    days back from now".
    """
    if value == "all":
        return {"days_filter": None, "bounds": None, "scope": "all"}
    legacy = _parse_days_filter(value)
    if legacy is not None:
        return {"days_filter": legacy, "bounds": None, "scope": str(legacy)}
    return {"days_filter": None, "bounds": rng.bounds(), "scope": "range"}


def _parse_page(value: str | None) -> int:
    """Coerce ``?page=`` into a positive int, defaulting to 1 on bad input.

    Without this, a request like ``?page=abc`` raises ValueError and 500s
    before ``all_posts_for()`` can clamp it.
    """
    try:
        n = int(value or "1")
    except (TypeError, ValueError):
        return 1
    return max(1, n)


def _no_accounts(request, workspace):
    return render(request, "analytics/no_accounts.html", {"workspace": workspace})


def _analytics_setup(request, workspace_id):
    """Workspace + connected accounts for a page, or a response to return instead."""
    workspace = _get_workspace(request, workspace_id)
    enabled = AnalyticsPlatformConfig.enabled_platforms()
    if not enabled:
        raise Http404("Analytics is disabled.")
    accounts = _analytics_accounts(workspace, enabled)
    return workspace, accounts


def _range_controls(rng: AnalyticsRange, extra: dict | None = None, *, show_compare: bool = True) -> dict:
    """What ``_range_controls.html`` needs: preset links, the custom form, compare toggle."""
    extra = extra or {}
    hidden = [(k, v) for k, vals in extra.items() for v in (vals if isinstance(vals, (list, tuple)) else [vals])]
    if not rng.compare:
        hidden.append(("compare", "none"))
    return {
        "presets": [
            {
                "label": f"{days}D",
                "title": f"Last {days} days",
                "url": rng.querystring(extra, range=str(days), start=None, end=None),
                "active": rng.preset == str(days),
            }
            for days in PRESET_DAYS
        ],
        "custom_active": rng.is_custom,
        "start": rng.start.isoformat(),
        "end": rng.end.isoformat(),
        "max": local_today(rng.tz).isoformat(),
        "hidden": hidden,
        "compare": rng.compare,
        "no_compare": not rng.compare,
        "show_compare": show_compare,
        "compare_on_url": rng.querystring(extra, compare=None),
        "compare_off_url": rng.querystring(extra, compare="none"),
    }


def _nav(workspace, rng: AnalyticsRange, active: str, extra: dict | None = None) -> dict:
    qs = rng.querystring(extra)
    kwargs = {"workspace_id": workspace.id}
    return {
        "active": active,
        "overview": reverse("analytics:index", kwargs=kwargs) + qs,
        "accounts": reverse("analytics:accounts", kwargs=kwargs) + qs,
        "compare": reverse("analytics:compare", kwargs=kwargs) + qs,
    }


def _export_urls(workspace, rng: AnalyticsRange, extra: dict | None = None) -> list[dict]:
    qs = rng.querystring(extra)
    return [
        {"label": label, "url": reverse("analytics:export", kwargs={"workspace_id": workspace.id, "kind": kind}) + qs}
        for kind, label in exports.EXPORT_KINDS.items()
    ]


@login_required
def analytics_index(request: HttpRequest, workspace_id) -> HttpResponse:
    """Workspace overview: totals, trend, networks, top posts and formats."""
    workspace, accounts = _analytics_setup(request, workspace_id)
    if not accounts:
        return _no_accounts(request, workspace)
    rng = parse_range(request.GET, workspace)
    series = request.GET.get("metric", rollup.DEFAULT_SERIES)
    data = rollup.overview(accounts, rng, series=series, top_sort=request.GET.get("top", ""))
    chart_extra = {"metric": data["trend"]["key"], "top": data["top_sort"]}
    context = {
        "workspace": workspace,
        "range": rng,
        "range_qs": rng.querystring()[1:],
        "controls": _range_controls(rng, chart_extra),
        "nav": _nav(workspace, rng, "overview"),
        "exports": _export_urls(workspace, rng),
        "series_options": [
            {
                "key": key,
                "label": label,
                "url": rng.querystring({"top": data["top_sort"]}, metric=key),
                "active": key == data["trend"]["key"],
            }
            for key, label in rollup.SERIES_LABELS.items()
        ],
        "top_options": [
            {
                "key": key,
                "label": label,
                "url": rng.querystring({"metric": data["trend"]["key"]}, top=key),
                "active": key == data["top_sort"],
            }
            for key, label in (("exposure", "Most seen"), ("engagements", "Most engaged"))
        ],
        "trend_summary": summarize_trend(data["trend"]),
        "excluded": [r for r in data["accounts"] if r.unavailable],
        **data,
    }
    return render(request, "analytics/overview.html", context)


@login_required
def analytics_accounts(request: HttpRequest, workspace_id) -> HttpResponse:
    """Lands on the first connected account with analytics, keeping the range."""
    workspace, accounts = _analytics_setup(request, workspace_id)
    if not accounts:
        return _no_accounts(request, workspace)
    target = _preferred_account(accounts)
    url = reverse("analytics:account", kwargs={"workspace_id": workspace.id, "account_id": target.id})
    return redirect(f"{url}?{request.GET.urlencode()}" if request.GET else url)


def _picked_accounts(request, accounts):
    by_id = {str(a.id): a for a in accounts}
    picked = []
    for raw in request.GET.getlist("accounts"):
        try:
            account = by_id.get(str(UUID(raw)))
        except ValueError:
            continue
        if account is not None and account not in picked:
            picked.append(account)
    return picked[:COMPARE_MAX_ACCOUNTS]


@login_required
def analytics_compare(request: HttpRequest, workspace_id) -> HttpResponse:
    """2-4 accounts side by side, plus an overlaid trend."""
    workspace, accounts = _analytics_setup(request, workspace_id)
    if not accounts:
        return _no_accounts(request, workspace)
    rng = parse_range(request.GET, workspace)
    picked = _picked_accounts(request, accounts)
    series = request.GET.get("metric")
    series = series if series in rollup.SERIES_LABELS else rollup.DEFAULT_SERIES
    extra = {"accounts": [str(a.id) for a in picked]}
    context = {
        "workspace": workspace,
        "range": rng,
        "range_qs": rng.querystring().removeprefix("?"),
        "controls": _range_controls(rng, {**extra, "metric": series}),
        "nav": _nav(workspace, rng, "compare", extra),
        "accounts": accounts,
        "picked": picked,
        "picked_ids": {a.id for a in picked},
        "min_accounts": COMPARE_MIN_ACCOUNTS,
        "max_accounts": COMPARE_MAX_ACCOUNTS,
        "ready": len(picked) >= COMPARE_MIN_ACCOUNTS,
        "series_options": [
            {"key": key, "label": label, "url": rng.querystring(extra, metric=key), "active": key == series}
            for key, label in rollup.SERIES_LABELS.items()
        ],
    }
    if context["ready"]:
        rows = rollup.collect(picked, rng)
        trend = rollup.account_trend(rows, rng, series)
        context.update(
            rows=rows,
            matrix=rollup.compare_matrix(rows, rng.compare),
            trend=trend,
            trend_summary=summarize_trend(trend),
            exports=_export_urls(workspace, rng, extra),
        )
    return render(request, "analytics/compare.html", context)


@login_required
def analytics_export(request: HttpRequest, workspace_id, kind: str) -> HttpResponse:
    """Streamed CSV of the overview, per-account figures or the posts table."""
    workspace, accounts = _analytics_setup(request, workspace_id)
    if kind not in exports.EXPORT_KINDS:
        raise Http404("Unknown export.")
    rng = parse_range(request.GET, workspace)
    picked = _picked_accounts(request, accounts)
    return exports.csv_response(kind, workspace, picked or accounts, rng)


@login_required
def analytics_account(request: HttpRequest, workspace_id, account_id) -> HttpResponse:
    """Main analytics page for one connected SocialAccount."""
    workspace, accounts = _analytics_setup(request, workspace_id)
    if not accounts:
        return _no_accounts(request, workspace)

    account = next((a for a in accounts if str(a.id) == str(account_id)), None)
    if account is None:
        return redirect("analytics:account", workspace_id=workspace.id, account_id=_preferred_account(accounts).id)

    rng = parse_range(request.GET, workspace)
    days = rng.days
    primary_color = "var(--primary)"  # locked in chat — design's locked accent
    window = _table_window(request.GET.get("table_range"), rng)

    has_any_post = PlatformPost.objects.filter(
        social_account=account,
        status=PlatformPost.Status.PUBLISHED,
    ).exists()

    # Empty / "freshly connected" state: no published posts AND no snapshot rows.
    from .models import AccountInsightsSnapshot

    has_snapshots = AccountInsightsSnapshot.objects.filter(social_account=account).exists()
    youtube_bundle = (
        services.account_analytics_bundle(account, days, rng.end) if account.platform == "youtube" else None
    )
    if youtube_bundle is not None:
        has_snapshots = has_snapshots or bool(youtube_bundle["native_current"])
    is_fresh = not has_any_post and not has_snapshots
    # Every gate comes from the one shared predicate, which also reports which
    # of them fired — only the admin-disabled one has a fix to offer (enable,
    # then reconnect), so the template needs the cause, not just the message.
    verdict = account.analytics_verdict
    analytics_unavailable = verdict is not None

    context: dict = {
        "workspace": workspace,
        "active_account": account,
        "accounts": accounts,
        "days": days,
        "range": rng,
        "range_qs": rng.querystring().removeprefix("?"),
        "controls": _range_controls(rng, show_compare=False),
        "nav": _nav(workspace, rng, "accounts"),
        "table_scope": window["scope"],
        "primary_color": primary_color,
        "platform_color": PLATFORM_COLOR.get(account.platform, "var(--primary)"),
        # Copy for the "calculated by BrightBean" labels on derived metrics.
        "platform_name": services.platform_name(account),
        "content_noun": services.content_noun(account),
        "is_fresh": is_fresh,
        "reporting_period": youtube_bundle["reporting_period"] if youtube_bundle is not None else None,
        # An unavailable platform gets its own reconnect instructions inside
        # the empty state, so suppressing the scope banner here keeps one set
        # of instructions on screen instead of two competing ones.
        "analytics_needs_reconnect": account.analytics_needs_reconnect and not analytics_unavailable,
        "analytics_unavailable": analytics_unavailable,
        "analytics_unavailable_notice": verdict.message if verdict else "",
        "analytics_disabled_by_admin": bool(verdict and verdict.cause == services.CAUSE_DISABLED),
    }

    if analytics_unavailable:
        table = services.all_posts_for(
            account,
            days_filter=window["days_filter"],
            bounds=window["bounds"],
            sort_key="date",
            sort_dir=request.GET.get("dir", "desc"),
            type_filter=request.GET.get("type", "all"),
            page=_parse_page(request.GET.get("page")),
        )
        # Strip metric columns — they'd all be 0 and contradict the
        # "analytics aren't available" notice above the table.
        table["metric_labels"] = []
        context["table"] = table
        if request.headers.get("HX-Request") and request.GET.get("partial") == "table":
            return render(request, "analytics/_post_table.html", context)
        if request.headers.get("HX-Request") and request.GET.get("partial") == "page":
            return render(request, "analytics/_page.html", context)
        return render(request, "analytics/index.html", context)

    if is_fresh:
        if request.headers.get("HX-Request") and request.GET.get("partial") == "page":
            return render(request, "analytics/_page.html", context)
        return render(request, "analytics/index.html", context)

    # Compute the bundle once and thread it into every consumer — the
    # post-level fallback inside the bundle is the dominant cost, and calling
    # these helpers without ``bundle`` re-runs it 3-4 times per render.
    bundle = youtube_bundle if youtube_bundle is not None else services.account_analytics_bundle(account, days, rng.end)
    follower_g = services.follower_growth(account, days, bundle=bundle, end=rng.end)
    hero_cards = services.hero_cards(account, days, bundle=bundle)
    engagement = services.engagement_card(account, days, bundle=bundle)
    chart = services.hero_chart_data(
        account,
        days,
        metric=request.GET.get("chart_metric"),
        bundle=bundle,
        end=rng.end,
    )
    table = services.all_posts_for(
        account,
        days_filter=window["days_filter"],
        bounds=window["bounds"],
        sort_key=request.GET.get("sort") or PLATFORM_PRIMARY.get(account.platform),
        sort_dir=request.GET.get("dir", "desc"),
        type_filter=request.GET.get("type", "all"),
        page=_parse_page(request.GET.get("page")),
    )

    context.update(
        {
            "follower_growth": follower_g,
            "hero_cards": hero_cards,
            "engagement": engagement,
            "calculated_note": services.calculated_metrics_note(
                account, cards=hero_cards, engagement=engagement, chart=chart, growth=follower_g
            ),
            "chart": chart,
            "reporting_period": bundle.get("reporting_period"),
            # ``null`` for days not reported yet: Chart.js draws those as a gap.
            "chart_data": {
                "labels": chart["labels"],
                "datasets": [
                    {
                        "label": chart["label"],
                        "values": [None if v is None else round(v, 4) for v in chart["derived"].series],
                    }
                ],
                "kind": chart["derived"].kind,
            },
            "chart_summary": summarize_trend(
                {
                    "label": chart["label"],
                    "labels": chart["labels"],
                    "datasets": [{"label": chart["label"], "values": chart["derived"].series}],
                }
            ),
            "table": table,
        }
    )

    if request.headers.get("HX-Request") and request.GET.get("partial") == "chart":
        return render(request, "analytics/_hero_chart.html", context)
    if request.headers.get("HX-Request") and request.GET.get("partial") == "table":
        return render(request, "analytics/_post_table.html", context)
    if request.headers.get("HX-Request") and request.GET.get("partial") == "page":
        # Full-page swap when account switcher is clicked via HTMX
        return render(request, "analytics/_page.html", context)

    return render(request, "analytics/index.html", context)


def summarize_trend(trend: dict) -> str:
    """One-sentence text equivalent of a trend chart, for assistive tech and the page."""
    labels = trend["labels"]
    parts = []
    for dataset in trend["datasets"]:
        known = [(v, d) for v, d in zip(dataset["values"], labels, strict=True) if v is not None]
        if not known:
            parts.append(f"{dataset['label']}: no data reported")
            continue
        total = sum(v for v, _ in known)
        peak_value, peak_day = max(known, key=lambda item: item[0])
        parts.append(f"{dataset['label']}: {total:,.0f} in total, peaking at {peak_value:,.0f} on {peak_day}")
    return f"{trend['label']}, {labels[0]} to {labels[-1]}. " + "; ".join(parts) + "."


@login_required
def post_detail(request: HttpRequest, workspace_id, post_id) -> HttpResponse:
    """HTMX-loaded payload for the slide-over post-detail drawer."""
    workspace = _get_workspace(request, workspace_id)
    post = get_object_or_404(
        PlatformPost.objects.select_related("social_account", "post").prefetch_related(
            "post__media_attachments__media_asset"
        ),
        id=post_id,
        social_account__workspace=workspace,
    )
    context = services.post_detail(post)
    context.update({"workspace": workspace})
    return render(request, "analytics/_post_detail.html", context)
