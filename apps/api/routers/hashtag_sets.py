"""``GET /api/v1/hashtag-sets`` — the workspace's saved hashtag groups."""

from __future__ import annotations

from ninja import Router

from apps.api.limits import enforce_http_rate_limits
from apps.api.middleware import log_audit_entry
from apps.api.schemas import HashtagSetsListResponse, HashtagSetSummary
from apps.composer.models import HashtagSet

router = Router(tags=["hashtag-sets"])


@router.get(
    "/",
    response=HashtagSetsListResponse,
    summary="List the workspace's hashtag sets",
)
def list_hashtag_sets(request):
    enforce_http_rate_limits(request, is_write=False)
    sets = HashtagSet.objects.for_workspace(request.workspace.id)  # type: ignore[attr-defined]  # set by ApiKeyAuth
    log_audit_entry(request, action="hashtag_sets.list", target_id=None, status_code=200)
    return HashtagSetsListResponse(
        hashtag_sets=[HashtagSetSummary(id=s.id, name=s.name, hashtags=s.hashtags) for s in sets]
    )
