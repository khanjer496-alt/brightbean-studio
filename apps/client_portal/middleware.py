"""Keeps a magic-link session inside the client portal."""

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect

# An allowlist: everything a portal-scoped session may reach. The portal itself
# (its views resolve the one workspace the link was for), sign-out, the terms
# acceptance and legal pages, and the token-authenticated approval links (they
# never use the session for anything but a "wrong person" check). Account
# settings, the email and password forms, 2FA setup, sign-in, the admin, the
# API and every workspace page are outside it on purpose: a magic link only
# proves control of a mailbox, and a forwarded one must not open any of them.
PORTAL_SESSION_PATH_PREFIXES = (
    "/portal/",
    "/legal/",
    "/health/",
    "/accounts/logout/",
    "/accounts/accept-terms/",
    "/approve-link/",
    "/integrations/slack-approvals/interactions/",
)

# Machine endpoints get a 403 rather than a redirect to an HTML page.
API_PATH_PREFIXES = ("/api/", "/oauth/")


def _asset_prefixes():
    """Static files and the public media subtrees, when this process serves them under a local path."""
    prefixes = []
    for url in (settings.STATIC_URL, settings.MEDIA_URL):
        url = str(url or "")
        if url.startswith("/") and not url.startswith("//") and url != "/":
            prefixes.append(url if url.endswith("/") else url + "/")
    return tuple(prefixes)


class PortalSessionMiddleware:
    """Reject a portal-scoped session outside the client portal.

    A magic link only proves control of a mailbox, so the session it starts
    (``is_portal_session``, see ``services.create_portal_session``) must not
    open the main app or the API. Users who have two-factor authentication
    never get such a session: they finish the 2FA challenge and receive an
    ordinary one. A GET elsewhere is sent to the portal without running the
    view; anything else is refused.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self.allowed = PORTAL_SESSION_PATH_PREFIXES + _asset_prefixes()

    def __call__(self, request):
        # path_info is what URL resolution matches against.
        if request.path_info.startswith(self.allowed):
            return self.get_response(request)
        # The flag is read off the session, which only loads once a cookie is sent.
        if request.session.get("is_portal_session") and request.user.is_authenticated:
            if request.method not in ("GET", "HEAD") or request.path_info.startswith(API_PATH_PREFIXES):
                raise PermissionDenied("This session is limited to the client portal.")
            return redirect("client_portal:dashboard")
        return self.get_response(request)
