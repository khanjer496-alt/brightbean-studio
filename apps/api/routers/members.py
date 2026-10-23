"""``GET /api/v1/members`` — the workspace's team (read-only)."""

from __future__ import annotations

from ninja import Router

from apps.api import ops
from apps.api.routers.common import run_op
from apps.api.schemas import MembersListResponse

router = Router(tags=["members"])


@router.get(
    "/",
    response=MembersListResponse,
    summary="List workspace members",
    description=(
        "Who is on the workspace's team, with their role and the `@mention` handle that notifies them from a "
        "comment. Email addresses are not included."
    ),
)
def list_members(request):
    return run_op(request, ops.list_members, action="members.read", is_write=False)
