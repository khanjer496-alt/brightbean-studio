"""Public "Connect your AI" page: no login, so a reviewer or a prospective customer can read it first."""

from django.conf import settings
from django.http import Http404, HttpResponse, JsonResponse
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


@require_GET
def openai_apps_challenge(request):
    """The domain-verification token OpenAI issues when an app is submitted to the ChatGPT directory.

    Plain text and nothing else (OpenAI rejects JSON or a list). 404 until ``OPENAI_APPS_CHALLENGE`` is set.
    """
    token = settings.OPENAI_APPS_CHALLENGE.strip()
    if not token:
        raise Http404
    return HttpResponse(token, content_type="text/plain")


@require_GET
def mcp_server_card(request):
    """Static description of the MCP server for registries that scan it (Smithery reads this when it cannot sign in)."""
    from apps.mcp.protocol import SERVER_VERSION
    from apps.mcp.tools import all_tools

    return JsonResponse(
        {
            "serverInfo": {"name": settings.BRAND_NAME, "version": SERVER_VERSION},
            "authentication": {"required": True, "schemes": ["oauth2"]},
            "tools": [tool.to_mcp_dict() for tool in all_tools()],
        }
    )
