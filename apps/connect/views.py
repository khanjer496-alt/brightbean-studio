"""Public "Connect your AI" page: no login, so a reviewer or a prospective customer can read it first."""

from django.conf import settings
from django.shortcuts import render
from django.views.decorators.http import require_GET

from . import platforms as catalog


@require_GET
def index(request):
    items = catalog.platforms()
    groups: dict[str, list[catalog.Platform]] = {}
    for item in items:
        groups.setdefault(item.group, []).append(item)
    return render(
        request,
        "connect/index.html",
        {
            "legal_page": "connect",
            "legal_name": settings.BRAND_LEGAL_NAME or settings.LEGAL_ENTITY_NAME,
            "mcp_url": catalog.mcp_url(),
            "groups": groups,
            "docs_url": f"{settings.MCP_PUBLIC_BASE_URL}/api/v1/docs",
        },
    )
