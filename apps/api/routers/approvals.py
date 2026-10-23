"""``/api/v1/approvals/*`` — the review queue, decisions, and post comments.

Decisions are taken as the person behind the key, so they need that person's
own ``approve_posts`` permission as well as the key's. Client members only see
and write ``external`` comments, as in the web app.
"""

from __future__ import annotations

import uuid

from ninja import Query, Router

from apps.api import ops
from apps.api.routers.common import run_op
from apps.api.schemas import (
    ApprovalDecisionRequest,
    CommentResponse,
    CommentsListResponse,
    CreateCommentRequest,
    PostResponse,
    PostsListResponse,
)

router = Router(tags=["approvals"])


@router.get(
    "/",
    response=PostsListResponse,
    summary="List posts in the approval workflow",
    description=(
        "Defaults to posts waiting on a reviewer or the client (`pending`). Pass `status` for "
        "`pending_review`, `pending_client`, `approved`, `rejected`, `changes_requested`, `on_hold` or `all`. "
        "Needs `approve_posts` or `create_posts`."
    ),
)
def list_approvals(
    request,
    status: str | None = Query(None),
    social_account_id: uuid.UUID | None = Query(None),
    limit: int = Query(ops.LIMIT_DEFAULT, ge=1, le=ops.LIMIT_MAX),
    cursor: str | None = Query(None),
):
    return run_op(
        request,
        ops.list_approvals,
        status=status,
        social_account_id=social_account_id,
        limit=limit,
        cursor=cursor,
        action="approvals.list",
        is_write=False,
    )


def _decide(request, post_id: uuid.UUID, decision: str, payload: ApprovalDecisionRequest):
    return run_op(
        request,
        ops.decide_approval,
        post_id,
        decision,
        payload.comment,
        action=f"approvals.{decision}",
        is_write=True,
        target_id=post_id,
    )


@router.post(
    "/{post_id}/approve",
    response=PostResponse,
    summary="Approve a post",
    description=(
        "Needs `approve_posts`. In the two-stage workflow an internal approval moves the post on to the client. "
        "409 when no platform post of the post is waiting for this reviewer."
    ),
)
def approve(request, post_id: uuid.UUID, payload: ApprovalDecisionRequest):
    return _decide(request, post_id, "approve", payload)


@router.post(
    "/{post_id}/request-changes",
    response=PostResponse,
    summary="Send a post back for changes",
    description="Needs `approve_posts`. A `comment` saying what to change is required; the author is notified.",
)
def request_changes(request, post_id: uuid.UUID, payload: ApprovalDecisionRequest):
    return _decide(request, post_id, "request_changes", payload)


@router.post(
    "/{post_id}/reject",
    response=PostResponse,
    summary="Reject a post",
    description="Needs `approve_posts`. A `comment` giving the reason is required; the author is notified.",
)
def reject(request, post_id: uuid.UUID, payload: ApprovalDecisionRequest):
    return _decide(request, post_id, "reject", payload)


@router.get(
    "/{post_id}/comments",
    response=CommentsListResponse,
    summary="List a post's comments",
    description="Top-level comments with their replies. Needs `create_posts` or `approve_posts`.",
)
def list_comments(request, post_id: uuid.UUID):
    return run_op(
        request, ops.list_comments, post_id, action="approvals.comments.list", is_write=False, target_id=post_id
    )


@router.post(
    "/{post_id}/comments",
    response={201: CommentResponse},
    summary="Comment on a post",
    description="Needs `create_posts` or `approve_posts`. Client members can only write `external` comments.",
)
def add_comment(request, post_id: uuid.UUID, payload: CreateCommentRequest):
    comment = run_op(
        request,
        ops.add_comment,
        post_id,
        body=payload.body,
        visibility=payload.visibility,
        parent_id=payload.parent_id,
        action="approvals.comments.create",
        is_write=True,
        target_id=lambda c: c.id,
        status_code=201,
        idempotent=True,
    )
    return 201, comment
