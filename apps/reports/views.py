"""Reports pages: the list, the editor with its live preview, run history and downloads.

Permissions: seeing reports needs ``view_analytics``; creating, editing and
running them needs the channel-management permission as well (owners and
managers). Everything is also gated on the plan's "branded reports" feature,
which a workspace that loses it keeps its definitions through but can't use.
"""

from __future__ import annotations

import base64
import binascii
import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.billing.entitlements import PlanLimitError, billing_enabled
from apps.members.models import WorkspaceMembership
from apps.workspaces.models import Workspace

from . import periods, services
from .builder import build_report_data, report_accounts
from .forms import ReportDefinitionForm
from .models import ReportDefinition, ReportRun

logger = logging.getLogger(__name__)


def _workspace(request, workspace_id, *, manage: bool = False) -> Workspace:
    workspace = get_object_or_404(Workspace, id=workspace_id)
    membership = WorkspaceMembership.objects.filter(user=request.user, workspace=workspace).first()
    if membership is None:
        raise PermissionDenied("You are not a member of this workspace.")
    if not services.can_view_reports(membership):
        raise PermissionDenied("Permission denied: view_analytics")
    if manage and not services.can_manage_reports(membership):
        raise PermissionDenied("Permission denied: managing reports")
    request.report_membership = membership
    return workspace


def _nav(workspace) -> dict:
    kwargs = {"workspace_id": workspace.id}
    return {
        "active": "reports",
        "overview": reverse("analytics:index", kwargs=kwargs),
        "accounts": reverse("analytics:accounts", kwargs=kwargs),
        "compare": reverse("analytics:compare", kwargs=kwargs),
        "reports": reverse("reports:list", kwargs=kwargs),
    }


def _page(request, workspace, template, context=None, status=200):
    membership = request.report_membership
    return render(
        request,
        template,
        {
            "workspace": workspace,
            "nav": _nav(workspace),
            "can_manage": services.can_manage_reports(membership),
            **(context or {}),
        },
        status=status,
    )


def _not_in_plan(request, workspace):
    return _page(
        request,
        workspace,
        "reports/not_included.html",
        {"billing_enabled": billing_enabled()},
        status=402,
    )


def _entitled(workspace) -> bool:
    return services.reports_enabled(workspace.organization)


def _definition(workspace, report_id) -> ReportDefinition:
    return get_object_or_404(
        ReportDefinition.objects.select_related("workspace__organization"), pk=report_id, workspace=workspace
    )


def _run(workspace, run_id) -> ReportRun:
    return get_object_or_404(
        ReportRun.objects.select_related("definition__workspace__organization"),
        pk=run_id,
        definition__workspace=workspace,
    )


# ---------------------------------------------------------------------------
# Definitions
# ---------------------------------------------------------------------------


@login_required
@require_GET
def report_list(request, workspace_id):
    workspace = _workspace(request, workspace_id)
    if not _entitled(workspace):
        return _not_in_plan(request, workspace)
    definitions = list(ReportDefinition.objects.filter(workspace=workspace).select_related("workspace__organization"))
    latest = {
        run.definition_id: run
        for run in ReportRun.objects.filter(definition__in=definitions)
        .order_by("definition_id", "-created_at")
        .distinct("definition_id")
    }
    now = timezone.now()
    rows = [{"definition": d, "latest": latest.get(d.id), "next_fire": periods.next_fire(d, now)} for d in definitions]
    return _page(
        request, workspace, "reports/list.html", {"rows": rows, "timezone_name": workspace.effective_timezone or "UTC"}
    )


def _form_page(request, workspace, form, definition):
    return _page(
        request,
        workspace,
        "reports/form.html",
        {"form": form, "definition": definition, "timezone_name": workspace.effective_timezone or "UTC"},
        status=400 if form.is_bound and form.errors else 200,
    )


def _save(request, workspace, form, previous):
    definition = form.save(commit=False)
    if definition.created_by_id is None:
        definition.created_by = request.user
    services.touch_schedule(definition, previous)
    definition.save()
    form.save_m2m()
    return definition


@login_required
@require_http_methods(["GET", "POST"])
def report_create(request, workspace_id):
    workspace = _workspace(request, workspace_id, manage=True)
    if not _entitled(workspace):
        return _not_in_plan(request, workspace)
    form = ReportDefinitionForm(request.POST or None, workspace=workspace)
    if request.method == "POST" and form.is_valid():
        definition = _save(request, workspace, form, None)
        return redirect("reports:history", workspace_id=workspace.id, report_id=definition.id)
    return _form_page(request, workspace, form, None)


@login_required
@require_http_methods(["GET", "POST"])
def report_edit(request, workspace_id, report_id):
    workspace = _workspace(request, workspace_id, manage=True)
    if not _entitled(workspace):
        return _not_in_plan(request, workspace)
    definition = _definition(workspace, report_id)
    previous = services.snapshot_schedule(definition)
    form = ReportDefinitionForm(request.POST or None, instance=definition, workspace=workspace)
    if request.method == "POST" and form.is_valid():
        _save(request, workspace, form, previous)
        return redirect("reports:history", workspace_id=workspace.id, report_id=definition.id)
    return _form_page(request, workspace, form, definition)


@login_required
@require_POST
def report_delete(request, workspace_id, report_id):
    workspace = _workspace(request, workspace_id, manage=True)
    _definition(workspace, report_id).delete()
    return redirect("reports:list", workspace_id=workspace.id)


@login_required
@require_POST
def report_preview(request, workspace_id):
    """Render the report as the form currently describes it, without saving or calling the AI provider."""
    workspace = _workspace(request, workspace_id, manage=True)
    if not _entitled(workspace):
        return HttpResponse(status=402)
    form = ReportDefinitionForm(request.POST, workspace=workspace)
    if not form.is_valid():
        return render(request, "reports/_preview_errors.html", {"form": form})
    definition = form.save(commit=False)
    accounts = list(form.cleaned_data["social_accounts"]) or report_accounts(ReportDefinition(workspace=workspace))
    rng = periods.resolve_range(definition)
    data = build_report_data(definition, rng, with_ai=False, accounts=accounts)
    return render(request, "reports/_report.html", {"data": data})


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


@login_required
@require_GET
def report_history(request, workspace_id, report_id):
    workspace = _workspace(request, workspace_id)
    if not _entitled(workspace):
        return _not_in_plan(request, workspace)
    definition = _definition(workspace, report_id)
    runs = list(definition.runs.all()[:50])
    context = {
        "definition": definition,
        "runs": runs,
        "polling": any(r.status in ("pending", "running") for r in runs),
        "link_days": settings.REPORT_LINK_MAX_AGE_DAYS,
        "timezone_name": workspace.effective_timezone or "UTC",
    }
    if request.headers.get("HX-Request") and request.headers.get("HX-Target") == "report-runs":
        return render(
            request,
            "reports/_runs.html",
            {"workspace": workspace, "can_manage": services.can_manage_reports(request.report_membership), **context},
        )
    return _page(request, workspace, "reports/history.html", context)


@login_required
@require_POST
def report_run(request, workspace_id, report_id):
    workspace = _workspace(request, workspace_id, manage=True)
    definition = _definition(workspace, report_id)
    try:
        services.queue_manual_run(definition, request.user, deliver=request.POST.get("deliver") == "on")
    except PlanLimitError as exc:
        return HttpResponse(str(exc), status=402)
    except services.RunLimitError as exc:
        messages.error(request, str(exc))
    return redirect("reports:history", workspace_id=workspace.id, report_id=definition.id)


@login_required
@require_POST
def report_revoke_links(request, workspace_id, report_id):
    """Turn off every shareable link to this report, including the ones already emailed."""
    workspace = _workspace(request, workspace_id, manage=True)
    definition = _definition(workspace, report_id)
    services.revoke_links(definition)
    messages.success(request, "Every shared link to this report has stopped working. New links work as usual.")
    return redirect("reports:history", workspace_id=workspace.id, report_id=definition.id)


@login_required
@require_GET
def run_detail(request, workspace_id, run_id):
    workspace = _workspace(request, workspace_id)
    if not _entitled(workspace):
        return _not_in_plan(request, workspace)
    run = _run(workspace, run_id)
    if not run.is_ready:
        return redirect("reports:history", workspace_id=workspace.id, report_id=run.definition_id)
    return _page(request, workspace, "reports/run_detail.html", {"run": run, "data": run.data})


@login_required
@require_GET
def run_pdf(request, workspace_id, run_id):
    workspace = _workspace(request, workspace_id)
    if not _entitled(workspace):
        raise PermissionDenied("Reports aren't included in this plan.")
    return pdf_response(_run(workspace, run_id))


@login_required
@require_POST
def run_share(request, workspace_id, run_id):
    workspace = _workspace(request, workspace_id, manage=True)
    if not _entitled(workspace):
        return HttpResponse(status=402)
    run = _run(workspace, run_id)
    if not run.is_ready:
        raise Http404("This report isn't ready yet.")
    return render(
        request,
        "reports/_share.html",
        {"url": services.share_url(run), "days": settings.REPORT_LINK_MAX_AGE_DAYS, "run": run},
    )


def pdf_response(run: ReportRun) -> FileResponse:
    if not run.pdf_file:
        raise Http404("This report has no PDF yet.")
    name = f"{slugify(run.definition.name) or 'report'}-{run.period_start}-{run.period_end}.pdf"
    response = FileResponse(run.pdf_file.open("rb"), as_attachment=True, filename=name, content_type="application/pdf")
    response["Cache-Control"] = "private, no-store"
    return response


# ---------------------------------------------------------------------------
# Shareable links (no login)
# ---------------------------------------------------------------------------


def _private(response):
    """Headers for a page that carries a client's results and is reachable by link alone."""
    response["Cache-Control"] = "private, no-store"
    response["X-Robots-Tag"] = "noindex, nofollow"
    response["Referrer-Policy"] = "no-referrer"
    return response


def _shared_run(token) -> ReportRun | None:
    run = services.resolve_token(token)
    if run is None or not services.reports_enabled(run.definition.workspace.organization):
        return None
    return run


@never_cache
@require_GET
def public_report(request, token):
    run = _shared_run(token)
    if run is None:
        return _private(render(request, "reports/link_expired.html", status=410))
    return _private(render(request, "reports/public.html", {"run": run, "data": run.data, "token": token}))


@never_cache
@require_GET
def public_pdf(request, token):
    run = _shared_run(token)
    if run is None:
        return _private(render(request, "reports/link_expired.html", status=410))
    return _private(pdf_response(run))


@never_cache
@require_GET
def public_logo(request, token):
    """The snapshot's logo for the emailed copy (mail clients block ``data:`` images)."""
    run = _shared_run(token)
    logo = (run.data.get("branding") or {}).get("logo", "") if run else ""
    prefix = "data:image/png;base64,"
    if not logo.startswith(prefix):
        raise Http404("No logo.")
    try:
        body = base64.b64decode(logo[len(prefix) :], validate=True)
    except (binascii.Error, ValueError):
        raise Http404("No logo.") from None
    response = HttpResponse(body, content_type="image/png")
    response["X-Content-Type-Options"] = "nosniff"
    return _private(response)
