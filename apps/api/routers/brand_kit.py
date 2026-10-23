"""``/api/v1/brand-kit`` — the workspace's logos, colours, fonts, copy and rules."""

from __future__ import annotations

from ninja import Router

from apps.api import ops
from apps.api.routers.common import run_op
from apps.api.schemas import BrandKitResponse, UpdateBrandKitRequest

router = Router(tags=["brand-kit"])


@router.get(
    "/",
    response=BrandKitResponse,
    summary="Read the brand kit",
    description="The workspace's own values, with blanks filled from the organization's. Needs `create_posts` or `manage_workspace_settings`.",
)
def get_brand_kit(request):
    return run_op(request, ops.get_brand_kit, action="brand_kit.read", is_write=False)


@router.patch(
    "/",
    response=BrandKitResponse,
    summary="Update the brand kit",
    description="Needs `manage_workspace_settings`. Fields you leave out are unchanged; lists replace.",
)
def update_brand_kit(request, payload: UpdateBrandKitRequest):
    return run_op(
        request,
        ops.update_brand_kit,
        **payload.dict(exclude_unset=True),
        action="brand_kit.update",
        is_write=True,
    )
