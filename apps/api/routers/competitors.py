"""``/api/v1/competitors/*`` — public competitor benchmarking.

Reading needs ``view_analytics``; adding and removing profiles needs
``manage_social_accounts``. Only networks that offer an official way to read
public profiles are available (``GET /competitors`` lists the rest with the
reason), and the plan caps how many each network may track. The comparison
shows only accounts on the key's allowlist.
"""

from __future__ import annotations

import datetime as dt
import uuid

from ninja import Query, Router

from apps.api import ops
from apps.api.routers.common import run_op
from apps.api.schemas import (
    AddCompetitorRequest,
    CompetitorComparisonResponse,
    CompetitorResponse,
    CompetitorsListResponse,
    DeletedResponse,
)

router = Router(tags=["competitors"])


@router.get("/", response=CompetitorsListResponse, summary="List tracked competitors and the networks available")
def list_competitors(request):
    return run_op(request, ops.list_competitors, action="competitors.list", is_write=False)


@router.get("/comparison", response=CompetitorComparisonResponse, summary="Compare your accounts with the competitors")
def comparison(
    request,
    days: int | None = Query(None, ge=1, le=366),
    start: dt.date | None = Query(None),
    end: dt.date | None = Query(None),
):
    return run_op(
        request,
        ops.get_competitor_comparison,
        days=days,
        start=start,
        end=end,
        action="competitors.comparison",
        is_write=False,
    )


@router.post("/", response={201: CompetitorResponse}, summary="Track a competitor")
def add_competitor(request, payload: AddCompetitorRequest):
    result = run_op(
        request,
        ops.add_competitor,
        payload,
        action="competitors.add",
        is_write=True,
        target_id=lambda r: r.id,
        status_code=201,
    )
    return 201, result


@router.delete("/{competitor_id}", response=DeletedResponse, summary="Stop tracking a competitor")
def remove_competitor(request, competitor_id: uuid.UUID):
    return run_op(
        request,
        ops.remove_competitor,
        competitor_id,
        action="competitors.remove",
        is_write=True,
        target_id=competitor_id,
    )
