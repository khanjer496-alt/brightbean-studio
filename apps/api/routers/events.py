"""``/api/v1/events/*`` — custom calendar events (launches, campaigns) shown on the calendar."""

from __future__ import annotations

import datetime as dt
import uuid

from ninja import Query, Router

from apps.api import ops
from apps.api.routers.common import run_op
from apps.api.schemas import (
    CalendarEventResponse,
    CalendarEventsListResponse,
    CreateCalendarEventRequest,
    DeletedResponse,
)

router = Router(tags=["events"])


@router.get(
    "/",
    response=CalendarEventsListResponse,
    summary="List calendar events",
    description="Events overlapping `date_from`…`date_to` (both inclusive, optional), soonest first.",
)
def list_events(
    request,
    date_from: dt.date | None = Query(None),
    date_to: dt.date | None = Query(None),
    limit: int = Query(ops.LIMIT_DEFAULT, ge=1, le=ops.LIMIT_MAX),
    cursor: str | None = Query(None),
):
    return run_op(
        request,
        ops.list_events,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        cursor=cursor,
        action="events.list",
        is_write=False,
    )


@router.post("/", response={201: CalendarEventResponse}, summary="Create a calendar event")
def create_event(request, payload: CreateCalendarEventRequest):
    event = run_op(
        request,
        ops.create_event,
        **payload.dict(),
        action="events.create",
        is_write=True,
        target_id=lambda e: e.id,
        status_code=201,
    )
    return 201, event


@router.delete("/{event_id}", response=DeletedResponse, summary="Delete a calendar event")
def delete_event(request, event_id: uuid.UUID):
    return run_op(request, ops.delete_event, event_id, action="events.delete", is_write=True, target_id=event_id)
