"""``/api/v1/reports/*`` — list scheduled client reports, run one now, read its latest run.

Reading needs ``view_analytics``; running needs ``manage_social_accounts`` too
(the same rule as the web UI) and queues the build for a worker, answering 202
with the pending run. Reports are a plan feature (``branded_reports``).
"""

from __future__ import annotations

import uuid

from ninja import Router
from ninja.errors import HttpError

from apps.api.limits import enforce_http_rate_limits
from apps.api.middleware import log_audit_entry
from apps.api.routers.posts import _require_perm
from apps.api.schemas import ReportResponse, ReportRunResponse
from apps.billing.entitlements import require_feature
from apps.billing.plans import Feature
from apps.reports import api_builders, services
from apps.reports.models import ReportDefinition

router = Router(tags=["reports"])


def _definition(request, report_id: uuid.UUID) -> ReportDefinition:
    try:
        return ReportDefinition.objects.select_related("workspace__organization").get(
            pk=report_id, workspace=request.workspace
        )
    except ReportDefinition.DoesNotExist:
        raise HttpError(404, "Report not found.") from None


@router.get("/", response=list[ReportResponse], summary="List this workspace's reports")
def list_reports(request):
    enforce_http_rate_limits(request, is_write=False)
    _require_perm(request, "view_analytics")
    require_feature(request.workspace.organization, Feature.BRANDED_REPORTS)
    log_audit_entry(request, action="reports.read.list", target_id=request.workspace.id, status_code=200)
    return [api_builders.build_report(d) for d in ReportDefinition.objects.filter(workspace=request.workspace)]


@router.post("/{report_id}/run", response={202: ReportRunResponse}, summary="Build a report now")
def run_report(request, report_id: uuid.UUID, deliver: bool = False):
    enforce_http_rate_limits(request, is_write=True)
    _require_perm(request, "view_analytics")
    _require_perm(request, "manage_social_accounts")
    definition = _definition(request, report_id)
    require_feature(request.workspace.organization, Feature.BRANDED_REPORTS)
    run = services.queue_manual_run(definition, None if request.user.is_anonymous else request.user, deliver=deliver)
    log_audit_entry(request, action="reports.run", target_id=definition.id, status_code=202)
    return 202, api_builders.build_run(run)


@router.get("/{report_id}/runs/latest", response=ReportRunResponse, summary="Read a report's most recent run")
def latest_run(request, report_id: uuid.UUID):
    enforce_http_rate_limits(request, is_write=False)
    _require_perm(request, "view_analytics")
    require_feature(request.workspace.organization, Feature.BRANDED_REPORTS)
    definition = _definition(request, report_id)
    run = api_builders.latest_run(definition)
    if run is None:
        raise HttpError(404, "This report hasn't been run yet.")
    log_audit_entry(request, action="reports.read.latest", target_id=definition.id, status_code=200)
    return api_builders.build_run(run)
