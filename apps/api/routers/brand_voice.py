"""``/api/v1/brand-voice`` — the workspace's brand voice, which every AI prompt carries."""

from __future__ import annotations

from ninja import Router

from apps.api import ops
from apps.api.routers.common import run_op
from apps.api.schemas import BrandVoiceResponse, UpdateBrandVoiceRequest

router = Router(tags=["brand-voice"])


@router.get(
    "/",
    response=BrandVoiceResponse,
    summary="Read the brand voice",
    description="Needs `create_posts` or `manage_workspace_settings`.",
)
def get_brand_voice(request):
    return run_op(request, ops.get_brand_voice, action="brand_voice.read", is_write=False)


@router.patch(
    "/",
    response=BrandVoiceResponse,
    summary="Update the brand voice",
    description="Needs `manage_workspace_settings`. Fields you leave out are unchanged; `examples` replaces the list.",
)
def update_brand_voice(request, payload: UpdateBrandVoiceRequest):
    return run_op(
        request,
        ops.update_brand_voice,
        **payload.dict(exclude_unset=True),
        action="brand_voice.update",
        is_write=True,
    )
