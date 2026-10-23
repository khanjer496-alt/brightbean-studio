"""``/api/v1/analytics/*`` — read-only channel and post analytics.

Designed for the agent polling workflow: an agent schedules a post via
``/api/v1/posts``, waits for it to publish, then iteratively calls these
endpoints to observe how it performs.

Both routes:

1. Enforce HTTP rate limits (per-key, per-workspace, global) up front.
2. Require the ``view_analytics`` workspace permission — analytics is a
   separately-scoped capability in the permission registry, so a key
   that wasn't granted it can't read performance data even for accounts
   it's otherwise allowlisted on.
3. Re-use the same allowlist guards (``_resolve_account`` /
   ``_get_workspace_post``) as ``/api/v1/posts``, so a partial-scope key
   can't read analytics for accounts it can't already see.
4. Delegate the actual response assembly to
   ``apps.analytics.api_builders`` so MCP and REST stay byte-equal.
5. Write a success audit log on the way out.

Failure paths produce ``analytics.read.{status_code}`` audit rows via the
centralised handler in ``apps/api/api.py``.
"""

from __future__ import annotations

import datetime as dt
import uuid

from ninja import Query, Router
from ninja.errors import HttpError

from apps.analytics.api_builders import (
    build_account_analytics,
    build_post_analytics,
    build_workspace_overview,
    connected_accounts,
)
from apps.analytics.ranges import api_range
from apps.analytics.rollup import TOP_POST_SORTS
from apps.api.limits import enforce_http_rate_limits
from apps.api.middleware import log_audit_entry
from apps.api.routers.posts import _get_workspace_post, _require_perm, _resolve_account
from apps.api.schemas import AccountAnalyticsResponse, PostAnalyticsResponse, WorkspaceOverviewResponse
from apps.billing.entitlements import require_feature
from apps.billing.plans import Feature

router = Router(tags=["analytics"])


@router.get(
    "/overview",
    response=WorkspaceOverviewResponse,
    summary="Read workspace-wide analytics over a date range",
)
def workspace_overview(
    request,
    days: int | None = Query(
        None,
        ge=1,
        le=90,
        description="Rolling window ending today in the workspace timezone (default 30). Ignored when start and end are set.",
    ),
    start: dt.date | None = Query(None, description="First day of a custom range (inclusive)."),
    end: dt.date | None = Query(None, description="Last day of a custom range (inclusive, not after today)."),
    compare: bool = Query(True, description="Include the previous equal-length period."),
    top: str = Query("exposure", description=f"Rank top posts by one of: {', '.join(TOP_POST_SORTS)}."),
):
    enforce_http_rate_limits(request, is_write=False)
    _require_perm(request, "view_analytics")
    if (start is None) != (end is None):
        raise HttpError(422, "start and end must be given together.")
    workspace = request.workspace
    require_feature(workspace.organization, Feature.ANALYTICS)
    accounts = connected_accounts(request.api_key)
    log_audit_entry(request, action="analytics.read.overview", target_id=workspace.id, status_code=200)
    return build_workspace_overview(
        workspace, accounts, api_range(workspace, days=days, start=start, end=end, compare=compare), top
    )


@router.get(
    "/accounts/{account_id}",
    response=AccountAnalyticsResponse,
    summary="Read channel analytics summary",
)
def account_analytics(
    request,
    account_id: uuid.UUID,
    days: int = Query(
        30,
        ge=7,
        le=90,
        description=(
            "Rolling window size in days. Must be between 7 and 90 inclusive. "
            "The ``derive`` helper needs 2× this many days of snapshots to "
            "compute the period-over-period delta, so the cap keeps DB scans bounded."
        ),
    ),
):
    enforce_http_rate_limits(request, is_write=False)
    _require_perm(request, "view_analytics")
    account = _resolve_account(request, account_id)
    log_audit_entry(
        request,
        action="analytics.read.account",
        target_id=account.id,
        status_code=200,
    )
    return build_account_analytics(account, days)


@router.get(
    "/posts/{post_id}",
    response=PostAnalyticsResponse,
    summary="Read post analytics with per-platform metrics",
)
def post_analytics(request, post_id: uuid.UUID):
    enforce_http_rate_limits(request, is_write=False)
    _require_perm(request, "view_analytics")
    post = _get_workspace_post(request, post_id)
    log_audit_entry(
        request,
        action="analytics.read.post",
        target_id=post.id,
        status_code=200,
    )
    return build_post_analytics(post)
