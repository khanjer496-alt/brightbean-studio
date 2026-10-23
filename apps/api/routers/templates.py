"""``/api/v1/templates/*`` — reusable post templates.

A template names the accounts it targets. Like a post, a template aimed at any
account outside the key's allowlist is invisible to it (404); writes naming an
account outside the allowlist are refused with 403.
"""

from __future__ import annotations

import uuid

from ninja import Query, Router

from apps.api import ops
from apps.api.routers.common import run_op
from apps.api.schemas import (
    CreateTemplateRequest,
    DeletedResponse,
    TemplateResponse,
    TemplatesListResponse,
    UpdateTemplateRequest,
)

router = Router(tags=["templates"])


@router.get("/", response=TemplatesListResponse, summary="List post templates")
def list_templates(
    request,
    search: str | None = Query(None, description="Matches the name or description."),
    limit: int = Query(ops.LIMIT_DEFAULT, ge=1, le=ops.LIMIT_MAX),
    cursor: str | None = Query(None),
):
    return run_op(
        request, ops.list_templates, search=search, limit=limit, cursor=cursor, action="templates.list", is_write=False
    )


@router.get("/{template_id}", response=TemplateResponse, summary="Read a template")
def get_template(request, template_id: uuid.UUID):
    return run_op(
        request, ops.get_template, template_id, action="templates.read", is_write=False, target_id=template_id
    )


@router.post("/", response={201: TemplateResponse}, summary="Create a template")
def create_template(request, payload: CreateTemplateRequest):
    template = run_op(
        request,
        ops.create_template,
        **payload.dict(),
        action="templates.create",
        is_write=True,
        target_id=lambda t: t.id,
        status_code=201,
        idempotent=True,
    )
    return 201, template


@router.patch("/{template_id}", response=TemplateResponse, summary="Update a template")
def update_template(request, template_id: uuid.UUID, payload: UpdateTemplateRequest):
    return run_op(
        request,
        ops.update_template,
        template_id,
        **payload.dict(exclude_unset=True),
        action="templates.update",
        is_write=True,
        target_id=template_id,
    )


@router.delete("/{template_id}", response=DeletedResponse, summary="Delete a template")
def delete_template(request, template_id: uuid.UUID):
    return run_op(
        request, ops.delete_template, template_id, action="templates.delete", is_write=True, target_id=template_id
    )
