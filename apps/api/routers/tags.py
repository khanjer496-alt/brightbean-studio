"""``/api/v1/tags/*`` — the workspace's tag list.

Tags label posts and ideas by name. Renaming or deleting a tag updates the
posts and ideas that carry it.
"""

from __future__ import annotations

import uuid

from ninja import Query, Router

from apps.api import ops
from apps.api.routers.common import run_op
from apps.api.schemas import DeletedResponse, TagNameRequest, TagResponse, TagsListResponse

router = Router(tags=["tags"])


@router.get("/", response=TagsListResponse, summary="List tags")
def list_tags(
    request,
    search: str | None = Query(None, description="Substring of the tag name."),
    limit: int = Query(ops.LIMIT_DEFAULT, ge=1, le=ops.LIMIT_MAX),
    cursor: str | None = Query(None),
):
    return run_op(request, ops.list_tags, search=search, limit=limit, cursor=cursor, action="tags.list", is_write=False)


@router.post(
    "/",
    response={201: TagResponse},
    summary="Create a tag",
    description="Returns the existing tag when one with that name exists.",
)
def create_tag(request, payload: TagNameRequest):
    tag = run_op(
        request,
        ops.create_tag,
        name=payload.name,
        action="tags.create",
        is_write=True,
        target_id=lambda t: t.id,
        status_code=201,
        idempotent=True,
    )
    return 201, tag


@router.patch("/{tag_id}", response=TagResponse, summary="Rename a tag", description="409 if the name is taken.")
def rename_tag(request, tag_id: uuid.UUID, payload: TagNameRequest):
    return run_op(
        request, ops.rename_tag, tag_id, name=payload.name, action="tags.update", is_write=True, target_id=tag_id
    )


@router.delete(
    "/{tag_id}",
    response=DeletedResponse,
    summary="Delete a tag",
    description="Also removes the tag from the posts and ideas that carry it.",
)
def delete_tag(request, tag_id: uuid.UUID):
    return run_op(request, ops.delete_tag, tag_id, action="tags.delete", is_write=True, target_id=tag_id)
