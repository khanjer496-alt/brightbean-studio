"""``/api/v1/ideas/*`` and ``/api/v1/idea-groups/*`` — the ideas kanban board.

Ideas are workspace content, not tied to an account, so the allowlist doesn't
apply. Everything here needs ``create_posts``, the permission the board needs
in the web app.
"""

from __future__ import annotations

import uuid

from ninja import Query, Router

from apps.api import ops
from apps.api.routers.common import run_op
from apps.api.schemas import (
    CreateIdeaGroupRequest,
    CreateIdeaRequest,
    DeletedResponse,
    IdeaGroupResponse,
    IdeaGroupsListResponse,
    IdeaResponse,
    IdeasListResponse,
    UpdateIdeaRequest,
)

router = Router(tags=["ideas"])
groups_router = Router(tags=["ideas"])


@router.get("/", response=IdeasListResponse, summary="List ideas")
def list_ideas(
    request,
    group_id: uuid.UUID | None = Query(None, description="Only ideas in this kanban column."),
    tag: str | None = Query(None),
    search: str | None = Query(None, description="Matches the title or description."),
    limit: int = Query(ops.LIMIT_DEFAULT, ge=1, le=ops.LIMIT_MAX),
    cursor: str | None = Query(None),
):
    return run_op(
        request,
        ops.list_ideas,
        group_id=group_id,
        tag=tag,
        search=search,
        limit=limit,
        cursor=cursor,
        action="ideas.list",
        is_write=False,
    )


@router.get("/{idea_id}", response=IdeaResponse, summary="Read an idea")
def get_idea(request, idea_id: uuid.UUID):
    return run_op(request, ops.get_idea, idea_id, action="ideas.read", is_write=False, target_id=idea_id)


@router.post("/", response={201: IdeaResponse}, summary="Create an idea")
def create_idea(request, payload: CreateIdeaRequest):
    idea = run_op(
        request,
        ops.create_idea,
        title=payload.title,
        description=payload.description,
        tags=payload.tags,
        group_id=payload.group_id,
        action="ideas.create",
        is_write=True,
        target_id=lambda i: i.id,
        status_code=201,
        idempotent=True,
    )
    return 201, idea


@router.patch(
    "/{idea_id}",
    response=IdeaResponse,
    summary="Update or move an idea",
    description="Send `group_id` and/or `position` to move it on the board.",
)
def update_idea(request, idea_id: uuid.UUID, payload: UpdateIdeaRequest):
    return run_op(
        request,
        ops.update_idea,
        idea_id,
        title=payload.title,
        description=payload.description,
        tags=payload.tags,
        group_id=payload.group_id,
        position=payload.position,
        action="ideas.update",
        is_write=True,
        target_id=idea_id,
    )


@router.delete("/{idea_id}", response=DeletedResponse, summary="Delete an idea")
def delete_idea(request, idea_id: uuid.UUID):
    return run_op(request, ops.delete_idea, idea_id, action="ideas.delete", is_write=True, target_id=idea_id)


@groups_router.get("/", response=IdeaGroupsListResponse, summary="List kanban columns")
def list_groups(request):
    return run_op(request, ops.list_idea_groups, action="idea_groups.list", is_write=False)


@groups_router.post("/", response={201: IdeaGroupResponse}, summary="Create a kanban column")
def create_group(request, payload: CreateIdeaGroupRequest):
    group = run_op(
        request,
        ops.create_idea_group,
        name=payload.name,
        action="idea_groups.create",
        is_write=True,
        target_id=lambda g: g.id,
        status_code=201,
        idempotent=True,
    )
    return 201, group


@groups_router.delete(
    "/{group_id}",
    response=DeletedResponse,
    summary="Delete a kanban column",
    description="The column must be empty (409 otherwise): move or delete its ideas first.",
)
def delete_group(request, group_id: uuid.UUID):
    return run_op(
        request, ops.delete_idea_group, group_id, action="idea_groups.delete", is_write=True, target_id=group_id
    )
