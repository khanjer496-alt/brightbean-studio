"""Competitors page: the comparison against your own accounts, adding and removing profiles, CSV export.

Reading needs ``view_analytics`` (the same rule as every analytics page);
adding and removing profiles needs ``manage_social_accounts`` as well.
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from apps.analytics.ranges import parse_range
from apps.analytics.views import _get_workspace, _nav, _range_controls
from apps.billing.entitlements import billing_enabled, get_entitlements
from apps.members.models import WorkspaceMembership
from apps.social_accounts.models import SocialAccount

from . import exports, services
from .models import Competitor


def _can_manage(request, workspace) -> bool:
    membership = WorkspaceMembership.objects.filter(user=request.user, workspace=workspace).first()
    return bool(membership and membership.effective_permissions.get("manage_social_accounts", False))


def _connected_accounts(workspace) -> list[SocialAccount]:
    return list(
        SocialAccount.objects.filter(
            workspace=workspace, connection_status=SocialAccount.ConnectionStatus.CONNECTED
        ).order_by("platform", "account_name")
    )


def _export_menu(workspace, rng) -> list[dict]:
    qs = rng.querystring()
    return [
        {"label": label, "url": reverse("competitors:export", kwargs={"workspace_id": workspace.id, "kind": kind}) + qs}
        for kind, label in exports.EXPORT_KINDS.items()
    ]


@login_required
@require_GET
def competitors_index(request: HttpRequest, workspace_id) -> HttpResponse:
    workspace = _get_workspace(request, workspace_id)
    ent = get_entitlements(workspace.organization)
    rng = parse_range(request.GET, workspace)
    accounts = _connected_accounts(workspace)
    data = services.comparison(workspace, rng, accounts)
    networks = [g["network"] for g in data["groups"]]
    requested = request.GET.get("network")
    chosen = next((n for n in networks if n.platform == requested), networks[0] if networks else None)
    chart = services.trend(workspace, rng, chosen.platform, accounts) if chosen else None
    limit = ent.max_competitors_per_network
    connected = {a.platform for a in accounts}
    add_networks = [
        {
            "network": n,
            "connected": n.platform in connected,
            "used": Competitor.objects.filter(workspace=workspace, platform=n.platform).count(),
        }
        for n in services.supported_networks()
    ]
    extra = {"network": chosen.platform} if chosen else {}
    context = {
        "workspace": workspace,
        "range": rng,
        "range_qs": rng.querystring().removeprefix("?"),
        "controls": _range_controls(rng, extra, show_compare=False),
        "nav": _nav(workspace, rng, "competitors"),
        "exports": _export_menu(workspace, rng),
        "groups": data["groups"],
        "unsupported": data["unsupported"],
        "can_manage": _can_manage(request, workspace),
        "limit": limit,
        "plan_enabled": limit is None or limit > 0,
        "billing_enabled": billing_enabled(),
        "add_networks": add_networks,
        "network_options": [
            {
                "label": n.name,
                "url": rng.querystring(network=n.platform),
                "active": chosen is not None and n.platform == chosen.platform,
            }
            for n in networks
        ],
        "chosen": chosen,
        "trend": chart,
        "trend_summary": services.trend_summary(chart) if chart else "",
        "last_read": services.freshness_note(list(Competitor.objects.filter(workspace=workspace))),
    }
    return render(request, "competitors/index.html", context)


@login_required
@require_POST
def competitor_add(request: HttpRequest, workspace_id) -> HttpResponse:
    workspace = _get_workspace(request, workspace_id)
    if not _can_manage(request, workspace):
        raise PermissionDenied("Permission denied: manage_social_accounts")
    platform = request.POST.get("platform", "")
    try:
        competitor = services.add_competitor(workspace, platform, request.POST.get("handle", ""), request.user)
    except services.CompetitorError as exc:
        messages.error(request, exc.message)
    else:
        messages.success(request, f"Now tracking {competitor.label}. Numbers are refreshed every day.")
    return redirect("competitors:index", workspace_id=workspace.id)


@login_required
@require_POST
def competitor_remove(request: HttpRequest, workspace_id, competitor_id) -> HttpResponse:
    workspace = _get_workspace(request, workspace_id)
    if not _can_manage(request, workspace):
        raise PermissionDenied("Permission denied: manage_social_accounts")
    try:
        services.remove_competitor(workspace, competitor_id)
    except services.CompetitorError as exc:
        raise Http404(exc.message) from exc
    messages.success(request, "Competitor removed. Its history was deleted.")
    return redirect("competitors:index", workspace_id=workspace.id)


@login_required
@require_GET
def competitors_export(request: HttpRequest, workspace_id, kind: str) -> HttpResponse:
    workspace = _get_workspace(request, workspace_id)
    if kind not in exports.EXPORT_KINDS:
        raise Http404("Unknown export.")
    rng = parse_range(request.GET, workspace)
    return exports.csv_response(kind, workspace, _connected_accounts(workspace), rng)
