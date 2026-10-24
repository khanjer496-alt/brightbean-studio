"""AI visibility page: set up the brand and prompts, read the latest report, save post ideas.

Reading needs ``view_analytics``. Editing the setup and running a report needs
``manage_workspace_settings`` (it spends the organization's AI allowance), and
saving an idea needs ``create_posts``, like the Ideas board itself.
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from apps.ai_writer.client import AI_PROVIDERS, AIError, resolve_ai
from apps.analytics.ranges import parse_range
from apps.analytics.views import _get_workspace, _nav
from apps.billing.entitlements import PlanLimitError, billing_enabled
from apps.members.models import WorkspaceMembership

from . import services
from .engines import enabled_engines
from .models import VisibilityProfile, VisibilityRun


def _can(request, workspace, permission: str) -> bool:
    membership = WorkspaceMembership.objects.filter(user=request.user, workspace=workspace).first()
    return bool(membership and membership.effective_permissions.get(permission, False))


def _require(request, workspace, permission: str) -> None:
    if not _can(request, workspace, permission):
        raise PermissionDenied(f"Permission denied: {permission}")


def _back(workspace):
    return redirect("ai_visibility:index", workspace_id=workspace.id)


@login_required
@require_GET
def visibility_index(request: HttpRequest, workspace_id) -> HttpResponse:
    workspace = _get_workspace(request, workspace_id)
    rng = parse_range(request.GET, workspace)
    organization = workspace.organization
    enabled = services.feature_enabled(organization)
    profile = VisibilityProfile.objects.filter(workspace=workspace).first() if enabled else None
    run = services.latest_run(profile) if profile else None
    chart = services.trend_chart(profile) if profile else None
    pending = services.pending_run(profile) if profile else None
    last_failed = (
        profile.runs.filter(status="failed").order_by("-created_at").first()
        if profile and run is None and pending is None
        else None
    )
    context = {
        "workspace": workspace,
        "nav": _nav(workspace, rng, "ai_visibility"),
        "enabled": enabled,
        "billing_enabled": billing_enabled(),
        "profile": profile,
        "prompt_limit": services.prompt_limit(workspace),
        "prompt_text": "\n".join(profile.prompts.values_list("text", flat=True)) if profile else "",
        "aliases_text": ", ".join(profile.aliases) if profile else "",
        "report": services.report_for(run) if run else None,
        "trend": chart,
        "trend_summary": (
            f"Mention rate over {len(chart['labels'])} report{'s' if len(chart['labels']) != 1 else ''}, "
            f"latest {chart['datasets'][0]['values'][-1]:.0f}%."
            if chart
            else ""
        ),
        "pending": pending,
        "last_failed": last_failed,
        "ai_configured": resolve_ai(organization) is not None,
        "can_edit": _can(request, workspace, "manage_workspace_settings"),
        "can_save_ideas": _can(request, workspace, "create_posts"),
        "honesty_note": services.HONESTY_NOTE,
        "engine_labels": sorted(e.label for e in enabled_engines(organization)),
        "provider_labels": {name: cls.label for name, cls in AI_PROVIDERS.items()},
    }
    return render(request, "ai_visibility/index.html", context)


@login_required
@require_POST
def visibility_save(request: HttpRequest, workspace_id) -> HttpResponse:
    workspace = _get_workspace(request, workspace_id)
    _require(request, workspace, "manage_workspace_settings")
    try:
        services.save_profile(
            workspace,
            brand_name=request.POST.get("brand_name", ""),
            website=request.POST.get("website", ""),
            aliases=request.POST.get("aliases", "").split(","),
            prompts=request.POST.get("prompts", "").splitlines(),
            is_active=request.POST.get("is_active") == "on",
        )
    except (PlanLimitError, services.VisibilityError) as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Saved. The next report uses these prompts.")
    return _back(workspace)


@login_required
@require_POST
def visibility_run(request: HttpRequest, workspace_id) -> HttpResponse:
    workspace = _get_workspace(request, workspace_id)
    _require(request, workspace, "manage_workspace_settings")
    profile = get_object_or_404(VisibilityProfile, workspace=workspace)
    try:
        services.run_now(profile, trigger=VisibilityRun.Trigger.MANUAL, user=request.user)
    except (AIError, PlanLimitError, services.VisibilityError) as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Report started. It takes a minute or two; reload this page to see it.")
    return _back(workspace)


@login_required
@require_POST
def visibility_save_idea(request: HttpRequest, workspace_id, run_id) -> HttpResponse:
    workspace = _get_workspace(request, workspace_id)
    _require(request, workspace, "create_posts")
    run = get_object_or_404(VisibilityRun, pk=run_id, profile__workspace=workspace)
    try:
        index = int(request.POST.get("index", ""))
    except ValueError as exc:
        raise Http404("Unknown idea.") from exc
    try:
        services.save_idea(run, index, request.user)
    except services.VisibilityError as exc:
        messages.error(request, exc.message)
    else:
        messages.success(request, "Saved to your Ideas board.")
    return redirect(reverse("ai_visibility:index", kwargs={"workspace_id": workspace.id}) + "#ideas")
