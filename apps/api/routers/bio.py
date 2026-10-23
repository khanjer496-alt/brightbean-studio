"""``/api/v1/bio/*`` — link-in-bio pages: read them, edit them and their links, read their counters.

Reading needs ``view_analytics``; editing needs ``manage_social_accounts`` (the
web editor's rule). Pages are created in the web app, where the plan's page
limit is explained. A key can only point a page's icons or latest-posts grid at
accounts on its allowlist.
"""

from __future__ import annotations

import uuid

from ninja import Query, Router

from apps.api import ops
from apps.api.routers.common import run_op
from apps.api.schemas import (
    BioPageResponse,
    BioPagesListResponse,
    BioStatsResponse,
    CreateBioLinkRequest,
    DeletedResponse,
    UpdateBioLinkRequest,
    UpdateBioPageRequest,
)

router = Router(tags=["bio"])


@router.get("/pages", response=BioPagesListResponse, summary="List link-in-bio pages")
def list_pages(
    request,
    limit: int = Query(ops.LIMIT_DEFAULT, ge=1, le=ops.LIMIT_MAX),
    cursor: str | None = Query(None),
):
    return run_op(request, ops.list_bio_pages, limit=limit, cursor=cursor, action="bio.list", is_write=False)


@router.get("/pages/{page_id}", response=BioPageResponse, summary="Read a page and its links")
def get_page(request, page_id: uuid.UUID):
    return run_op(request, ops.get_bio_page, page_id, action="bio.read", is_write=False, target_id=page_id)


@router.patch("/pages/{page_id}", response=BioPageResponse, summary="Update a page")
def update_page(request, page_id: uuid.UUID, payload: UpdateBioPageRequest):
    return run_op(
        request,
        ops.update_bio_page,
        page_id,
        **payload.dict(exclude_unset=True),
        action="bio.update",
        is_write=True,
        target_id=page_id,
    )


@router.post("/pages/{page_id}/links", response={201: BioPageResponse}, summary="Add a link; returns the page")
def add_link(request, page_id: uuid.UUID, payload: CreateBioLinkRequest):
    page = run_op(
        request,
        ops.add_bio_link,
        page_id,
        **payload.dict(),
        action="bio.link_add",
        is_write=True,
        target_id=page_id,
        status_code=201,
    )
    return 201, page


@router.patch("/pages/{page_id}/links/{link_id}", response=BioPageResponse, summary="Update or move a link")
def update_link(request, page_id: uuid.UUID, link_id: uuid.UUID, payload: UpdateBioLinkRequest):
    return run_op(
        request,
        ops.update_bio_link,
        page_id,
        link_id,
        **payload.dict(exclude_unset=True),
        action="bio.link_update",
        is_write=True,
        target_id=link_id,
    )


@router.delete("/pages/{page_id}/links/{link_id}", response=DeletedResponse, summary="Remove a link")
def delete_link(request, page_id: uuid.UUID, link_id: uuid.UUID):
    return run_op(
        request, ops.delete_bio_link, page_id, link_id, action="bio.link_delete", is_write=True, target_id=link_id
    )


@router.get("/pages/{page_id}/stats", response=BioStatsResponse, summary="Views and clicks per day and per link")
def get_stats(request, page_id: uuid.UUID, days: int = Query(30, ge=1, le=90)):
    return run_op(request, ops.get_bio_stats, page_id, days=days, action="bio.stats", is_write=False, target_id=page_id)
