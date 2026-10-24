"""``GET /api/v1/usage`` — the organization's plan, its limits and what is used (read-only)."""

from __future__ import annotations

from ninja import Router

from apps.api import ops
from apps.api.routers.common import run_op
from apps.api.schemas import UsageResponse

router = Router(tags=["usage"])


@router.get(
    "/",
    response=UsageResponse,
    summary="Read plan limits and usage",
    description=(
        "Channels, workspaces, team members, scheduled posts this month and API & MCP calls today, each with its "
        "limit (`null` = unlimited). Self-hosted installs report `billing_enabled: false` and no limits."
    ),
)
def get_usage(request):
    return run_op(request, ops.get_usage, action="usage.read", is_write=False)
