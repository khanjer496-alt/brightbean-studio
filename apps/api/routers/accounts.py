"""``/api/v1/accounts`` — list the connected accounts this key may target, and pause or resume publishing to one."""

from __future__ import annotations

import uuid

from ninja import Router

from apps.api import ops
from apps.api.limits import enforce_http_rate_limits
from apps.api.middleware import log_audit_entry
from apps.api.routers.common import run_op
from apps.api.schemas import AccountsListResponse, AccountSummary

router = Router(tags=["accounts"])


@router.get(
    "/",
    response=AccountsListResponse,
    summary="List the SocialAccounts this API key is allowed to act on",
)
def list_accounts(request):
    enforce_http_rate_limits(request, is_write=False)
    api_key = request.api_key
    accounts = [AccountSummary.from_social_account(sa) for sa in api_key.social_accounts.all()]
    log_audit_entry(request, action="accounts.list", target_id=None, status_code=200)
    return AccountsListResponse(accounts=accounts)


@router.post(
    "/{account_id}/pause",
    response=AccountSummary,
    summary="Pause publishing to an account: its scheduled posts are held until it is resumed",
)
def pause_account(request, account_id: uuid.UUID):
    return run_op(
        request,
        ops.set_account_paused,
        account_id,
        paused=True,
        action="accounts.pause",
        is_write=True,
        target_id=account_id,
    )


@router.post(
    "/{account_id}/resume",
    response=AccountSummary,
    summary="Resume publishing to an account: held posts go out, overdue ones first",
)
def resume_account(request, account_id: uuid.UUID):
    return run_op(
        request,
        ops.set_account_paused,
        account_id,
        paused=False,
        action="accounts.resume",
        is_write=True,
        target_id=account_id,
    )
