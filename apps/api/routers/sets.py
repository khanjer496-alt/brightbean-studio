"""``/api/v1/sets/*`` — saved groups of accounts ("Client A: all channels").

Reading and using a set needs ``create_posts``; creating, changing and deleting one needs
``manage_social_accounts`` (the web settings rule). A key only sees the members of a set that are on its
allowlist. ``set_id`` is also accepted wherever a request lists accounts: ``/ai/caption``, ``/ai/adapt`` and
``/repurpose/jobs`` outputs; ``POST /sets/{set_id}/drafts`` writes one draft across the whole set.
"""

from __future__ import annotations

import uuid

from ninja import Query, Router

from apps.api import ops
from apps.api.routers.common import run_op
from apps.api.schemas import (
    AccountSetResponse,
    AccountSetsListResponse,
    CreateAccountSetRequest,
    CreateSetDraftRequest,
    DeletedResponse,
    PostResponse,
    UpdateAccountSetRequest,
)

router = Router(tags=["sets"])


@router.get("/", response=AccountSetsListResponse, summary="List saved account sets")
def list_sets(
    request,
    limit: int = Query(ops.LIMIT_DEFAULT, ge=1, le=ops.LIMIT_MAX),
    cursor: str | None = Query(None),
):
    return run_op(request, ops.list_account_sets, limit=limit, cursor=cursor, action="set.list", is_write=False)


@router.post("/", response={201: AccountSetResponse}, summary="Save a group of accounts")
def create_set(request, payload: CreateAccountSetRequest):
    result = run_op(
        request,
        ops.create_account_set,
        **payload.dict(),
        action="set.create",
        is_write=True,
        target_id=lambda r: r.id,
        status_code=201,
    )
    return 201, result


@router.get("/{set_id}", response=AccountSetResponse, summary="Read a set")
def get_set(request, set_id: uuid.UUID):
    return run_op(request, ops.get_account_set, set_id, action="set.read", is_write=False, target_id=set_id)


@router.patch("/{set_id}", response=AccountSetResponse, summary="Rename a set or change its accounts and defaults")
def update_set(request, set_id: uuid.UUID, payload: UpdateAccountSetRequest):
    return run_op(
        request,
        ops.update_account_set,
        set_id,
        **payload.dict(exclude_unset=True),
        action="set.update",
        is_write=True,
        target_id=set_id,
    )


@router.delete("/{set_id}", response=DeletedResponse, summary="Delete a set (its accounts stay connected)")
def delete_set(request, set_id: uuid.UUID):
    return run_op(request, ops.delete_account_set, set_id, action="set.delete", is_write=True, target_id=set_id)


@router.post(
    "/{set_id}/drafts",
    response={201: PostResponse},
    summary="Create one draft on every connected account of a set",
)
def create_set_draft(request, set_id: uuid.UUID, payload: CreateSetDraftRequest):
    post = run_op(
        request,
        ops.create_set_draft,
        set_id,
        **payload.dict(),
        action="set.draft",
        is_write=True,
        target_id=lambda r: r.id,
        status_code=201,
        idempotent=True,
    )
    return 201, post
