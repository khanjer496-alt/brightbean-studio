"""``/api/v1/autonomy`` — the workspace's autonomy policy and the audit trail of what agents did."""

from __future__ import annotations

import datetime as dt
import uuid

from ninja import Query, Router

from apps.api import ops
from apps.api.routers.common import run_op
from apps.api.schemas import AgentActionsListResponse, AutonomyPolicyResponse, UpdateAutonomyPolicyRequest

router = Router(tags=["autonomy"])


@router.get(
    "/",
    response=AutonomyPolicyResponse,
    summary="Read the autonomy policy",
    description=(
        "What agents may do on their own in this workspace: a level (`off`, `draft_only`, `auto_guardrails`, "
        "`full_auto`) for drafting, publishing, replying and recycling, per-account overrides, the guardrails, the "
        "undo window and whether crisis pause is on. Any key can read it. Writes that the policy holds come back "
        "with an `autonomy` notice saying why."
    ),
)
def get_policy(request):
    return run_op(request, ops.get_autonomy_policy, action="autonomy.read", is_write=False)


@router.patch(
    "/",
    response=AutonomyPolicyResponse,
    summary="Tighten the autonomy policy",
    description=(
        "Needs `manage_workspace_settings`. A key can only make the policy stricter (lower a level, turn crisis pause "
        "on, add blocked phrases); loosening it is for a person in Settings."
    ),
)
def update_policy(request, payload: UpdateAutonomyPolicyRequest):
    return run_op(
        request,
        ops.update_autonomy_policy,
        **payload.dict(exclude_unset=True),
        action="autonomy.update",
        is_write=True,
    )


@router.get(
    "/actions",
    response=AgentActionsListResponse,
    summary="List agent actions",
    description=(
        "The audit trail, newest first: who acted (person, agent or automation), what, the policy's decision "
        "(`executed`, `held`, `blocked`) and the guardrail results behind it. Needs `create_posts` or "
        "`manage_workspace_settings`. Paginated."
    ),
)
def list_actions(
    request,
    actor_type: str | None = Query(None, description="`human`, `agent` or `system`."),
    action_type: str | None = Query(
        None, description="`draft_content`, `publish_posts`, `reply`, `recycle_evergreen`."
    ),
    decision: str | None = Query(None, description="`executed`, `held` or `blocked`."),
    social_account_id: uuid.UUID | None = Query(None),
    since: dt.datetime | None = Query(None),
    until: dt.datetime | None = Query(None),
    limit: int = Query(ops.LIMIT_DEFAULT, ge=1, le=ops.LIMIT_MAX),
    cursor: str | None = Query(None),
):
    return run_op(
        request,
        ops.list_agent_actions,
        actor_type=actor_type,
        action_type=action_type,
        decision=decision,
        social_account_id=social_account_id,
        since=since,
        until=until,
        limit=limit,
        cursor=cursor,
        action="autonomy.actions.list",
        is_write=False,
    )
