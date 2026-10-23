"""``/api/v1/categories/*`` — content categories posts and queues can be filed under."""

from __future__ import annotations

import uuid

from ninja import Router

from apps.api import ops
from apps.api.routers.common import run_op
from apps.api.schemas import (
    CategoriesListResponse,
    CategoryResponse,
    CreateCategoryRequest,
    DeletedResponse,
    UpdateCategoryRequest,
)

router = Router(tags=["categories"])


@router.get("/", response=CategoriesListResponse, summary="List content categories")
def list_categories(request):
    return run_op(request, ops.list_categories, action="categories.list", is_write=False)


@router.post("/", response={201: CategoryResponse}, summary="Create a content category")
def create_category(request, payload: CreateCategoryRequest):
    category = run_op(
        request,
        ops.create_category,
        name=payload.name,
        color=payload.color,
        action="categories.create",
        is_write=True,
        target_id=lambda c: c.id,
        status_code=201,
        idempotent=True,
    )
    return 201, category


@router.patch("/{category_id}", response=CategoryResponse, summary="Update a content category")
def update_category(request, category_id: uuid.UUID, payload: UpdateCategoryRequest):
    return run_op(
        request,
        ops.update_category,
        category_id,
        **payload.dict(exclude_unset=True),
        action="categories.update",
        is_write=True,
        target_id=category_id,
    )


@router.delete(
    "/{category_id}",
    response=DeletedResponse,
    summary="Delete a content category",
    description="Posts that used it are left uncategorised.",
)
def delete_category(request, category_id: uuid.UUID):
    return run_op(
        request, ops.delete_category, category_id, action="categories.delete", is_write=True, target_id=category_id
    )
