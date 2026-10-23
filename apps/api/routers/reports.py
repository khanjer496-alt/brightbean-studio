"""``/api/v1/reports/*`` — list scheduled client reports, run one now, read its latest run.

Reading needs ``view_analytics``; running needs ``manage_social_accounts`` too
(the same rule as the web UI) and queues the build for a worker, answering 202
with the pending run. Reports are a plan feature (``branded_reports``). A
report covering any account outside the key's allowlist is invisible to it
(404), like a post.
"""

from __future__ import annotations

import uuid

from ninja import Query, Router

from apps.api import ops
from apps.api.routers.common import run_op
from apps.api.schemas import ReportRunResponse, ReportsListResponse

router = Router(tags=["reports"])


@router.get("/", response=ReportsListResponse, summary="List this workspace's reports")
def list_reports(
    request,
    limit: int = Query(ops.LIMIT_DEFAULT, ge=1, le=ops.LIMIT_MAX),
    cursor: str | None = Query(None),
):
    return run_op(request, ops.list_reports, limit=limit, cursor=cursor, action="reports.read.list", is_write=False)


@router.post("/{report_id}/run", response={202: ReportRunResponse}, summary="Build a report now")
def run_report(request, report_id: uuid.UUID, deliver: bool = False):
    run = run_op(
        request,
        ops.run_report,
        report_id,
        deliver=deliver,
        action="reports.run",
        is_write=True,
        target_id=report_id,
        status_code=202,
    )
    return 202, run


@router.get("/{report_id}/runs/latest", response=ReportRunResponse, summary="Read a report's most recent run")
def latest_run(request, report_id: uuid.UUID):
    return run_op(
        request,
        ops.get_latest_report_run,
        report_id,
        action="reports.read.latest",
        is_write=False,
        target_id=report_id,
    )
