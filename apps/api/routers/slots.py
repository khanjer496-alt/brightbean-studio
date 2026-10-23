"""``/api/v1/slots/*`` — an account's weekly posting slots and its next free times.

Slots are tied to a social account, so every route honours the key's account
allowlist. Changing them needs ``manage_social_accounts``, as in the web app.
"""

from __future__ import annotations

import uuid

from ninja import Query, Router

from apps.api import ops
from apps.api.routers.common import run_op
from apps.api.schemas import (
    CreatePostingSlotRequest,
    DeletedResponse,
    NextFreeSlotsResponse,
    PostingSlotResponse,
    PostingSlotsListResponse,
    UpdatePostingSlotRequest,
)

router = Router(tags=["slots"])


@router.get("/", response=PostingSlotsListResponse, summary="List posting slots")
def list_slots(request, social_account_id: uuid.UUID | None = Query(None)):
    return run_op(request, ops.list_slots, social_account_id, action="slots.list", is_write=False)


@router.get(
    "/next-free",
    response=NextFreeSlotsResponse,
    summary="Find an account's next free posting slots",
    description="Upcoming posting slots no scheduled post holds yet, soonest first (`count` 1–20, default 1).",
)
def next_free(request, social_account_id: uuid.UUID = Query(...), count: int = Query(1, ge=1, le=20)):
    return run_op(
        request,
        ops.next_free_slots,
        social_account_id,
        count,
        action="slots.next_free",
        is_write=False,
        target_id=social_account_id,
    )


@router.post(
    "/",
    response={201: PostingSlotResponse},
    summary="Create a posting slot",
    description="Needs `manage_social_accounts`. 409 when the account already has a slot at that day and time.",
)
def create_slot(request, payload: CreatePostingSlotRequest):
    slot = run_op(
        request,
        ops.create_slot,
        social_account_id=payload.social_account_id,
        day_of_week=payload.day_of_week,
        time=payload.time,
        action="slots.create",
        is_write=True,
        target_id=lambda s: s.id,
        status_code=201,
    )
    return 201, slot


@router.patch(
    "/{slot_id}",
    response=PostingSlotResponse,
    summary="Update a posting slot",
    description="Change its time or switch it on or off. Needs `manage_social_accounts`.",
)
def update_slot(request, slot_id: uuid.UUID, payload: UpdatePostingSlotRequest):
    return run_op(
        request,
        ops.update_slot,
        slot_id,
        time=payload.time,
        is_active=payload.is_active,
        action="slots.update",
        is_write=True,
        target_id=slot_id,
    )


@router.delete("/{slot_id}", response=DeletedResponse, summary="Delete a posting slot")
def delete_slot(request, slot_id: uuid.UUID):
    return run_op(request, ops.delete_slot, slot_id, action="slots.delete", is_write=True, target_id=slot_id)
