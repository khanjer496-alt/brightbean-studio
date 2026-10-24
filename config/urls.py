import logging
import re
from urllib.parse import urlsplit

from allauth.account.decorators import secure_admin_login
from django.conf import settings
from django.contrib import admin
from django.shortcuts import redirect
from django.templatetags.static import static
from django.urls import include, path, re_path
from django.views.static import serve

from apps.accounts.views import health_check
from apps.api.api import api as agent_api
from apps.oauth_server import views as oauth_views

logger = logging.getLogger(__name__)

# Staff sign in through allauth like everyone else, so the admin cannot be used
# to skip the two-factor step: Django's own admin login form never asks for it.
admin.site.login = secure_admin_login(admin.site.login)


def favicon(request):
    """Browsers and feed readers ask for /favicon.ico whatever the page links; send them to the brand icon.

    Resolved per request (not at import) so a hashed static name never has to exist when the URLconf loads,
    and temporary because that name changes with every deploy.
    """
    return redirect(static(settings.BRAND_FAVICON_ICO_STATIC))


urlpatterns = [
    path("admin/", admin.site.urls),
    path("health/", health_check, name="health_check"),
    path("favicon.ico", favicon),
    path("accounts/", include("apps.accounts.urls")),
    path("accounts/", include("allauth.urls")),
    path("organizations/", include("apps.organizations.urls")),
    # Org-level Agent API key management (Phase 4 UI). Mounted at
    # /organizations/api-keys/ so the page sits alongside General,
    # Workspaces, Team Members in the settings sidebar.
    path("organizations/api-keys/", include("apps.api_keys.urls")),
    # Plan & billing. Always routed so reverse() works on every deployment;
    # the views 404 unless settings.BILLING_ENABLED.
    path("organizations/billing/", include("apps.billing.urls")),
    path("workspaces/", include("apps.workspaces.urls")),
    path("members/", include("apps.members.urls")),
    path("settings/", include("apps.settings_manager.urls")),
    path("social-accounts/", include("apps.social_accounts.urls")),
    # Content Pipeline (Stream A)
    path("workspace/<uuid:workspace_id>/", include("apps.composer.urls")),
    path("workspace/<uuid:workspace_id>/calendar/", include("apps.calendar.urls")),
    path("workspace/<uuid:workspace_id>/inbox/", include("apps.inbox.urls")),
    # Client reports, competitors and AI visibility live under Analytics; listed first so their prefixes never reach the account route.
    path("workspace/<uuid:workspace_id>/analytics/reports/", include("apps.reports.urls")),
    path("workspace/<uuid:workspace_id>/analytics/competitors/", include("apps.competitors.urls")),
    path("workspace/<uuid:workspace_id>/analytics/ai-visibility/", include("apps.ai_visibility.urls")),
    path("workspace/<uuid:workspace_id>/analytics/", include("apps.analytics.urls")),
    path("workspace/<uuid:workspace_id>/bio/", include("apps.linkinbio.urls")),
    # AI writing assistant: composer actions, and the org's own provider key.
    path("workspace/<uuid:workspace_id>/ai/", include("apps.ai_writer.urls")),
    # Repurposing: one source into threads, posts, carousels, scripts…
    path("workspace/<uuid:workspace_id>/repurpose/", include("apps.repurpose.urls")),
    # Autonomy: the agent activity trail, the "going out soon" tray and the dial.
    path("workspace/<uuid:workspace_id>/activity/", include("apps.autonomy.urls")),
    path("workspace/<uuid:workspace_id>/automations/", include("apps.plugs.urls")),
    # Outbound webhooks: endpoints and delivery log (workspace settings).
    path("workspace/<uuid:workspace_id>/webhooks/", include("apps.webhooks.urls")),
    # Shareable report links: signed token, no login.
    path("reports/share/", include("apps.reports.public_urls")),
    # Public link-in-bio pages: no login, nothing stored about the visitor.
    path("l/", include("apps.linkinbio.public_urls")),
    path("webhooks/", include("apps.inbox.webhook_urls")),
    path("webhooks/", include("apps.common.webhook_urls")),
    path("webhooks/", include("apps.billing.webhook_urls")),
    # Agent API (Phase 2) — programmatic access for external AI agents.
    # Authenticated via scoped bearer tokens issued from the Organization
    # → API Keys page. OpenAPI docs at /api/v1/docs. ``agent_api.urls``
    # is Ninja's (patterns, app_namespace, instance_namespace) tuple,
    # which Django's path() handles natively.
    path("api/v1/", agent_api.urls),
    # OAuth 2.1 Authorization Server for the MCP connector flow (native
    # Claude Desktop login). django-oauth-toolkit serves /oauth/authorize/
    # + /oauth/token/ + /oauth/revoke_token/; apps.oauth_server adds DCR
    # (/oauth/register) and the discovery documents below.
    path("oauth/", include("apps.oauth_server.urls")),
    path(
        ".well-known/oauth-authorization-server",
        oauth_views.authorization_server_metadata_view,
        name="oauth-authorization-server-metadata",
    ),
    path(
        ".well-known/oauth-protected-resource",
        oauth_views.protected_resource_metadata_view,
        name="oauth-protected-resource-metadata",
    ),
    # RFC 9728 inserts the well-known segment before the resource's path, so
    # the metadata for the /api/v1/mcp resource is discovered at this
    # path-scoped URL — what the WWW-Authenticate challenge points clients at.
    path(
        ".well-known/oauth-protected-resource/api/v1/mcp",
        oauth_views.protected_resource_metadata_view,
        name="oauth-protected-resource-metadata-mcp",
    ),
    # Approval Workflow (Stream F)
    path("workspace/<uuid:workspace_id>/", include("apps.approvals.urls")),
    # One-tap decisions from approval emails (signed links; confirmation page, POST decides)
    path("approve-link/", include("apps.approvals.urls_links")),
    # Slack approval buttons (signature-verified) and the install callback
    path("integrations/slack-approvals/", include("apps.approvals.urls_slack")),
    # Client Portal Admin (workspace settings)
    path("workspace/<uuid:workspace_id>/settings/clients/", include("apps.client_portal.urls_admin")),
    # Media Library
    path("workspace/<uuid:workspace_id>/media/", include("apps.media_library.urls")),
    # Client Portal (Stream F)
    path("portal/", include("apps.client_portal.urls")),
    path("legal/", include("apps.legal.urls")),
    path("notifications/", include("apps.notifications.urls")),
    path("onboarding/", include("apps.onboarding.urls")),
    path("organizations/media/", include("apps.media_library.urls_org")),
    path("organizations/ai/", include("apps.ai_writer.urls_org")),
    path("", include("apps.accounts.urls_root")),
]

# ---------------------------------------------------------------------------
# Intelligence integration — mounted only when env vars are set.
# Two prefixes, DIFFERENT namespaces:
#   /orgs/<uuid:org_id>/intelligence/*  — org-scoped surfaces under
#                                          namespace ``intelligence`` (playground,
#                                          subscribe, checkout, tools, …)
#   /intelligence/*                     — non-org-scoped under namespace
#                                          ``intelligence_global`` (Stripe success
#                                          URL + user-scoped finalizing)
#
# Both used to share the namespace ``intelligence`` which caused only the
# first include's names to be reachable via ``reverse()`` — activate,
# finalizing, finalizing-status (in the second include) were orphaned and
# any redirect/url-tag to them blew up with NoReverseMatch.
# ---------------------------------------------------------------------------
if settings.INTELLIGENCE_ENABLED:
    from apps.intelligence import urls as intelligence_urls

    urlpatterns += [
        path(
            "orgs/<uuid:org_id>/intelligence/",
            include((intelligence_urls.org_scoped_patterns, "intelligence")),
        ),
        path(
            "intelligence/",
            include((intelligence_urls.user_scoped_patterns, "intelligence_global")),
        ),
    ]

# ---------------------------------------------------------------------------
# User-uploaded media.
#
# Gated on SERVE_MEDIA, not DEBUG: under config.settings.production with
# STORAGE_BACKEND=local the uploads on disk have no other route, so every
# {{ asset.file.url }} 404s *and* the absolute URLs apps/publisher/engine.py
# hands to Instagram, Threads, Facebook, Pinterest and Google Business (which
# fetch media server-side, with no byte-upload fallback) are unfetchable.
#
# django.conf.urls.static.static() can't express this — it returns [] whenever
# DEBUG is False, which is exactly the case that needs the route.
# ---------------------------------------------------------------------------


# Subtrees of MEDIA_ROOT reachable without authentication. An allowlist, not a
# filter: a new upload_to prefix is private-by-default until it is named here,
# rather than silently world-readable the day it is added.
#
# media_library/ (assets, thumbs/, versions/) has to be public — the platforms
# listed above fetch those URLs server-side as anonymous clients. avatars/ and
# workspaces/icons/ are page chrome in templates/base.html, carry nothing
# confidential, and would cost a permission query per page behind a view.
#
# comment_attachments/ is deliberately absent: PostComment has an "internal"
# visibility that apps/client_portal/views.py filters out for portal clients,
# so a public URL there would hand back precisely what that filter withholds.
# apps.approvals.views.comment_attachment serves those behind the workspace
# membership check instead.
PUBLIC_MEDIA_PREFIXES = (
    "media_library/",
    "avatars/",
    "workspaces/icons/",
)


def media_urlpatterns():
    """Route the public subtrees of ``MEDIA_ROOT`` when this process serves media itself."""
    media_url = settings.MEDIA_URL
    document_root = settings.MEDIA_ROOT

    if urlsplit(media_url).netloc:
        # MEDIA_URL points at another host (CDN, object storage). There is
        # nothing local to route, and a pattern built from an absolute URL
        # would compile to a regex no request can ever match. The static()
        # helper this replaced short-circuited on netloc for the same reason.
        return []

    prefix = media_url.lstrip("/")
    if not prefix:
        # An empty prefix compiles to ^(?P<path>.*)$ — a catch-all appended
        # after every real route, serving whatever it matches out of the
        # process CWD. With STORAGE_BACKEND=s3, MEDIA_URL is never assigned and
        # Django normalises the empty default to "/", so this guard is what
        # keeps a misconfigured deployment from handing out the repo it runs
        # from (GET /.env, /requirements.txt, ...).
        return []

    if not document_root:
        # Refusing to build the route is right — serve() would resolve every
        # path against the process CWD — but a silent 404 for every upload is
        # exactly the failure this module exists to stop, so say so out loud.
        logger.warning(
            "SERVE_MEDIA is on but MEDIA_ROOT is empty: %s is not routed, so every "
            "upload will 404 and publishing to the platforms that fetch attachment "
            "URLs server-side will fail.",
            media_url,
        )
        return []

    return [
        re_path(
            rf"^{re.escape(prefix)}(?P<path>{re.escape(subtree)}.*)$",
            serve,
            {"document_root": document_root},
        )
        for subtree in PUBLIC_MEDIA_PREFIXES
    ]


if settings.SERVE_MEDIA:
    urlpatterns += media_urlpatterns()
