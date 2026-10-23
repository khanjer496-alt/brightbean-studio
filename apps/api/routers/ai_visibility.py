"""``/api/v1/ai-visibility`` — the latest "how AI describes your brand" report.

Read-only: needs ``view_analytics`` and a plan with the AI visibility report.
The report is set up, run and its ideas saved in the web app, because running
one spends the organization's AI allowance.
"""

from __future__ import annotations

from ninja import Router

from apps.api import ops
from apps.api.routers.common import run_op
from apps.api.schemas import AIVisibilityResponse

router = Router(tags=["ai-visibility"])


@router.get("/", response=AIVisibilityResponse, summary="Read the latest AI visibility report")
def get_report(request):
    return run_op(request, ops.get_ai_visibility, action="ai_visibility.read", is_write=False)
