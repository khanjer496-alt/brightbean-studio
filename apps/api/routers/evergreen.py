"""``/api/v1/evergreen/*`` — content that is re-posted into queues now and then.

Items belong to one social account, so only those on the key's allowlisted
accounts are visible. Everything needs ``create_posts``; pre-approving repeats
(skipping the approval workflow) also needs ``approve_posts``.
"""

from __future__ import annotations

import uuid

from ninja import Query, Router

from apps.api import ops
from apps.api.routers.common import run_op
from apps.api.schemas import (
    AddEvergreenRequest,
    DeletedResponse,
    EvergreenDetailResponse,
    EvergreenItemResponse,
    EvergreenListResponse,
    UpdateEvergreenRequest,
)

router = Router(tags=["evergreen"])


@router.get("/", response=EvergreenListResponse, summary="List evergreen items")
def list_evergreen(
    request,
    social_account_id: uuid.UUID | None = Query(None),
    category_id: uuid.UUID | None = Query(None),
    paused: bool | None = Query(None, description="Only paused (true) or only active (false) items."),
    limit: int = Query(ops.LIMIT_DEFAULT, ge=1, le=ops.LIMIT_MAX),
    cursor: str | None = Query(None),
):
    return run_op(
        request,
        ops.list_evergreen,
        social_account_id=social_account_id,
        category_id=category_id,
        paused=paused,
        limit=limit,
        cursor=cursor,
        action="evergreen.list",
        is_write=False,
    )


@router.get(
    "/{item_id}",
    response=EvergreenDetailResponse,
    summary="Read an evergreen item with its history",
    description="Every repeat, newest first, with its status and the engagement it earned.",
)
def get_evergreen(request, item_id: uuid.UUID):
    return run_op(request, ops.get_evergreen, item_id, action="evergreen.read", is_write=False, target_id=item_id)


@router.post(
    "/",
    response={201: EvergreenListResponse},
    summary="Add content to the evergreen library",
    description=(
        "Give exactly one of `post_id` (a published post; one item per published channel), `template_id` or "
        "`idea_id` (both need `social_account_id`). 409 when the post is already in the library for that channel."
    ),
)
def add_evergreen(request, payload: AddEvergreenRequest):
    result = run_op(
        request,
        ops.add_evergreen,
        payload,
        action="evergreen.create",
        is_write=True,
        target_id=lambda r: r.items[0].id if r.items else None,
        status_code=201,
        idempotent=True,
    )
    return 201, result


@router.patch(
    "/{item_id}",
    response=EvergreenItemResponse,
    summary="Update, pause or resume an evergreen item",
    description="Send only the fields to change. `is_paused: true` stops it being recycled until resumed.",
)
def update_evergreen(request, item_id: uuid.UUID, payload: UpdateEvergreenRequest):
    return run_op(
        request,
        ops.update_evergreen,
        item_id,
        payload.model_dump(exclude_unset=True),
        action="evergreen.update",
        is_write=True,
        target_id=item_id,
    )


@router.delete("/{item_id}", response=DeletedResponse, summary="Remove an evergreen item")
def remove_evergreen(request, item_id: uuid.UUID):
    return run_op(request, ops.remove_evergreen, item_id, action="evergreen.delete", is_write=True, target_id=item_id)
