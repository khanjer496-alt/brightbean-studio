"""``/api/v1/best-times/*`` — when an account's posts do best, and making those times its posting slots.

Reading is open to any key that can see the account. Applying changes the
account's posting slots, so it needs ``manage_social_accounts``, as in the web app.
"""

from __future__ import annotations

import uuid

from ninja import Query, Router

from apps.api import ops
from apps.api.routers.common import run_op
from apps.api.schemas import ApplyBestTimesRequest, ApplyBestTimesResponse, BestTimesResponse

router = Router(tags=["best-times"])


@router.get(
    "/",
    response=BestTimesResponse,
    summary="Get an account's best times to post",
    description=(
        "The account's top weekday and hour combinations (`count` 1–14, default 5) and the 7x24 score grid behind "
        "them, in the workspace timezone. `basis` says whether they were learned from the account's own posts or "
        "are industry defaults because it has too few posts with results."
    ),
)
def get_best_times(request, social_account_id: uuid.UUID = Query(...), count: int = Query(5, ge=1, le=14)):
    return run_op(
        request,
        ops.get_best_times,
        social_account_id,
        count,
        action="best_times.read",
        is_write=False,
        target_id=social_account_id,
    )


@router.post(
    "/apply",
    response=ApplyBestTimesResponse,
    summary="Make the best times the account's posting slots",
    description=(
        "`mode: replace` swaps the account's posting slots for the suggested times, `add` keeps them. "
        "`preview: true` reports the change without writing it. Posts already holding a time keep it. "
        "Needs `manage_social_accounts`."
    ),
)
def apply_best_times(request, payload: ApplyBestTimesRequest):
    return run_op(
        request,
        ops.apply_best_times,
        social_account_id=payload.social_account_id,
        count=payload.count,
        mode=payload.mode,
        preview=payload.preview,
        action="best_times.apply",
        is_write=not payload.preview,
        target_id=payload.social_account_id,
    )
