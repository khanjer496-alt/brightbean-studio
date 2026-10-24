"""``/api/v1/plugs/*`` — engagement automations on published posts.

"When this post reaches N likes, reposts or views within T hours: repost it, reply under it, or have another
account in the workspace repost or quote it." Thin adapters over the shared operations in :mod:`apps.api.ops`,
the same ones the MCP tools call. Reading needs ``create_posts``; changing rules needs ``publish_directly``. A
rule that fires still goes through the autonomy policy, so it may be held for a person or blocked. Every account
named must be on the key's allowlist.
"""

from __future__ import annotations

import uuid

from ninja import Query, Router

from apps.api import ops
from apps.api.routers.common import run_op
from apps.api.schemas import (
    CreatePlugRuleRequest,
    DeletedResponse,
    PlugRuleResponse,
    PlugRulesListResponse,
    UpdatePlugRuleRequest,
)

router = Router(tags=["plugs"])


@router.get("/", response=PlugRulesListResponse, summary="List engagement rules and what each network supports")
def list_rules(
    request,
    social_account_id: uuid.UUID | None = Query(None),
    limit: int = Query(ops.LIMIT_DEFAULT, ge=1, le=ops.LIMIT_MAX),
    cursor: str | None = Query(None),
):
    return run_op(
        request,
        ops.list_plug_rules,
        social_account_id=social_account_id,
        limit=limit,
        cursor=cursor,
        action="plugs.list",
        is_write=False,
    )


@router.get("/{rule_id}", response=PlugRuleResponse, summary="Read a rule and how it has gone")
def get_rule(request, rule_id: uuid.UUID):
    return run_op(request, ops.get_plug_rule, rule_id, action="plugs.read", is_write=False, target_id=rule_id)


@router.post("/", response={201: PlugRuleResponse}, summary="Add an engagement rule")
def create_rule(request, payload: CreatePlugRuleRequest):
    rule = run_op(
        request,
        ops.create_plug_rule,
        payload,
        action="plugs.create",
        is_write=True,
        target_id=lambda r: r.id,
        status_code=201,
    )
    return 201, rule


@router.patch("/{rule_id}", response=PlugRuleResponse, summary="Change a rule or switch it on or off")
def update_rule(request, rule_id: uuid.UUID, payload: UpdatePlugRuleRequest):
    return run_op(
        request, ops.update_plug_rule, rule_id, payload, action="plugs.update", is_write=True, target_id=rule_id
    )


@router.delete("/{rule_id}", response=DeletedResponse, summary="Remove a rule")
def delete_rule(request, rule_id: uuid.UUID):
    return run_op(request, ops.delete_plug_rule, rule_id, action="plugs.delete", is_write=True, target_id=rule_id)
