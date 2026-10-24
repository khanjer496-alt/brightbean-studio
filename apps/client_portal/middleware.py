"""Keeps a magic-link session inside the client portal."""

from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect

# What a portal-scoped session may reach: the portal itself, assets and media
# it renders, sign-out, the terms page, and the token-authenticated approval
# links (they never use the session for anything but a "wrong person" check).
PORTAL_SESSION_PATH_PREFIXES = (
    "/portal/",
    "/static/",
    "/media/",
    "/health/",
    "/legal/",
    "/accounts/logout/",
    "/accounts/accept-terms/",
    "/approve-link/",
    "/integrations/slack-approvals/interactions",
)

# Machine endpoints get a 403 rather than a redirect to an HTML page.
API_PATH_PREFIXES = ("/api/", "/oauth/")


class PortalSessionMiddleware:
    """Reject a portal-scoped session outside the client portal.

    A magic link only proves control of a mailbox, so the session it starts
    (``is_portal_session``, see ``services.create_portal_session``) must not
    open the main app or the API. Users who have two-factor authentication
    never get such a session: they finish the 2FA challenge and receive an
    ordinary one.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path.startswith(PORTAL_SESSION_PATH_PREFIXES):
            return self.get_response(request)
        # The flag is read off the session, which only loads once a cookie is sent.
        if request.session.get("is_portal_session") and request.user.is_authenticated:
            if request.method != "GET" or request.path.startswith(API_PATH_PREFIXES):
                raise PermissionDenied("This session is limited to the client portal.")
            return redirect("client_portal:dashboard")
        return self.get_response(request)
