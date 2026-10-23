"""Response builders shared by the agent REST API and MCP, so both return the same bytes."""

from __future__ import annotations

from apps.api.schemas import ReportResponse, ReportRunResponse

from .models import ReportDefinition, ReportRun


def build_run(run: ReportRun) -> ReportRunResponse:
    ready = run.is_ready and run.data
    return ReportRunResponse(
        id=run.id,
        report_id=run.definition_id,
        status=run.status,
        trigger=run.trigger,
        period_start=run.period_start,
        period_end=run.period_end,
        created_at=run.created_at,
        finished_at=run.finished_at,
        error=run.error if run.status == ReportRun.Status.FAILED else "",
        emailed_to=len(run.emailed_to),
        summary=(run.data.get("ai_summary") or {}).get("text") if ready else None,
        metrics=[
            {"key": k["key"], "label": k["label"], "value": k["display"], "delta": k["delta"]}
            for k in run.data.get("kpis", [])
        ]
        if ready
        else [],
    )


def latest_run(definition: ReportDefinition) -> ReportRun | None:
    return definition.runs.order_by("-created_at").first()


def build_report(definition: ReportDefinition) -> ReportResponse:
    run = latest_run(definition)
    return ReportResponse(
        id=definition.id,
        name=definition.name,
        range_type=definition.range_type,
        sections=list(definition.sections),
        recipients=len(definition.recipients),
        frequency=definition.frequency,
        is_active=definition.is_active,
        visible_to_clients=definition.visible_to_clients,
        white_label=definition.white_label,
        latest_run=build_run(run) if run else None,
    )
