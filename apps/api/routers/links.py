"""``/api/v1/links/*`` — short links and their click counts.

Making a link needs ``create_posts``; reading links and counts needs ``view_analytics``. A link tied to an
account is only visible to a key that has that account on its allowlist. The link service is the workspace's
own choice (built in, or its Dub, Short.io or Kutt account); callers never see or send a provider key.
"""

from __future__ import annotations

import uuid

from ninja import Query, Router

from apps.api import ops
from apps.api.routers.common import run_op
from apps.api.schemas import CreateShortLinkRequest, LinkStatsResponse, ShortLinkResponse, ShortLinksListResponse

router = Router(tags=["links"])


@router.post("/", response={201: ShortLinkResponse}, summary="Make a short link")
def create_link(request, payload: CreateShortLinkRequest):
    link = run_op(
        request,
        ops.create_short_link,
        **payload.dict(),
        action="link.create",
        is_write=True,
        target_id=lambda result: result.id,
        status_code=201,
        idempotent=True,
    )
    return 201, link


@router.get("/", response=ShortLinksListResponse, summary="List short links with their last 30 days of clicks")
def list_links(
    request,
    post_id: uuid.UUID | None = Query(None),
    social_account_id: uuid.UUID | None = Query(None),
    limit: int = Query(ops.LIMIT_DEFAULT, ge=1, le=ops.LIMIT_MAX),
    cursor: str | None = Query(None),
):
    return run_op(
        request,
        ops.list_short_links,
        post_id=post_id,
        social_account_id=social_account_id,
        limit=limit,
        cursor=cursor,
        action="link.list",
        is_write=False,
    )


@router.get("/stats", response=LinkStatsResponse, summary="Clicks per day, link, post and account")
def link_stats(
    request,
    days: int = Query(30, ge=1, le=90),
    post_id: uuid.UUID | None = Query(None),
    social_account_id: uuid.UUID | None = Query(None),
    link_id: uuid.UUID | None = Query(None),
):
    return run_op(
        request,
        ops.get_link_stats,
        days=days,
        post_id=post_id,
        social_account_id=social_account_id,
        link_id=link_id,
        action="link.stats",
        is_write=False,
    )
