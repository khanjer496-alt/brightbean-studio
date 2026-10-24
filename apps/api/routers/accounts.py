"""``/api/v1/accounts`` — list the connected accounts this key may target, and pause or resume publishing to one."""

from __future__ import annotations

import uuid
from typing import Literal

from ninja import Query, Router

from apps.api import ops
from apps.api.limits import enforce_http_rate_limits
from apps.api.middleware import log_audit_entry
from apps.api.routers.common import run_op
from apps.api.schemas import AccountPauseResponse, AccountsListResponse, AccountSummary

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
    response=AccountPauseResponse,
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
    response=AccountPauseResponse,
    summary="Resume publishing to an account; choose what happens to the posts held while it was paused",
    description=(
        "`held_posts` decides what happens to scheduled posts whose time passed while the account was paused: "
        "`requeue` (the default) moves them into the account's next open posting slots in their original order; "
        "`publish_now` sends them a minute apart; `skip` turns them into drafts. Posts still in the future keep "
        "their time, and nothing is deleted. **Behaviour change:** callers that omit `held_posts` used to have "
        "every held post go out at once and now get `requeue`; send `publish_now` for the old behaviour. The "
        "response counts what was `requeued`, `published_now` and `skipped`."
    ),
)
def resume_account(
    request,
    account_id: uuid.UUID,
    held_posts: Literal["requeue", "publish_now", "skip"] = Query(
        "requeue", description="What to do with posts whose time passed while the account was paused."
    ),
):
    return run_op(
        request,
        ops.set_account_paused,
        account_id,
        paused=False,
        held_posts=held_posts,
        action="accounts.resume",
        is_write=True,
        target_id=account_id,
    )
