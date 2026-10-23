"""``GET /api/v1/signatures`` — the workspace's shared signatures.

Personal signatures belong to a person, not to the workspace an API key acts
on, so they are not listed here.
"""

from __future__ import annotations

from ninja import Router

from apps.api.limits import enforce_http_rate_limits
from apps.api.middleware import log_audit_entry
from apps.api.schemas import SignaturesListResponse, SignatureSummary
from apps.composer.models import Signature

router = Router(tags=["signatures"])


@router.get(
    "/",
    response=SignaturesListResponse,
    summary="List the workspace's signatures",
)
def list_signatures(request):
    enforce_http_rate_limits(request, is_write=False)
    signatures = Signature.objects.for_workspace(request.workspace.id)  # type: ignore[attr-defined]  # set by ApiKeyAuth
    log_audit_entry(request, action="signatures.list", target_id=None, status_code=200)
    return SignaturesListResponse(
        signatures=[SignatureSummary(id=s.id, name=s.name, body=s.body, is_default=s.is_default) for s in signatures]
    )
