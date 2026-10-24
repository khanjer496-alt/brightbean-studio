"""URL routes for the OAuth Authorization Server (MCP connector flow).

``app_name`` is "oauth2_provider" so any internal ``reverse()`` performed by
django-oauth-toolkit resolves against these routes. Only the routes the MCP
flow needs are exposed — DOT's application-management UI is left unmounted.
"""

from csp.decorators import csp_update
from django.urls import path
from oauth2_provider import views as oauth2_views

from . import views

app_name = "oauth2_provider"

# Other clients redirect somewhere else: ChatGPT (chatgpt.com), Copilot Studio (the Power Platform consent host), and
# command-line or desktop clients on a loopback port (Claude Code, Cursor, VS Code).
# The consent form POSTs to /oauth/authorize/ ('self'), then 302-redirects to the
# client's redirect_uri (Claude: https://claude.ai|claude.com/api/mcp/auth_callback).
# Chromium enforces form-action across the whole redirect chain, so the redirect
# target must be allowlisted on the consent page or the flow dies silently there.
# csp_update appends to the global CSP_FORM_ACTION, scoping the relaxation here only.
OAUTH_CLIENT_FORM_ACTION = (
    "https://claude.ai https://claude.com https://chatgpt.com https://global.consent.azure-apim.net "
    "http://localhost:* http://127.0.0.1:* http://[::1]:*"
)
authorize_view = csp_update(FORM_ACTION=OAUTH_CLIENT_FORM_ACTION)(views.ConsentAuthorizationView.as_view())

urlpatterns = [
    path("authorize/", authorize_view, name="authorize"),
    path("token/", oauth2_views.TokenView.as_view(), name="token"),
    path("revoke_token/", oauth2_views.RevokeTokenView.as_view(), name="revoke-token"),
    path("register", views.RegisterView.as_view(), name="register"),
]
