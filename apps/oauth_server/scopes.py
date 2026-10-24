"""OAuth scopes: ``mcp`` plus one ``cap:<capability>`` per tool-level capability.

The capability scopes are how a narrowed grant survives from the consent screen to the access token (and to the
refresh tokens that follow): the user's choice is stored on the grant as scopes, not in a table of our own.
"""

from __future__ import annotations

from django.conf import settings
from oauth2_provider.scopes import BaseScopes

from apps.api_keys import capabilities as caps


class CapabilityScopes(BaseScopes):
    def get_all_scopes(self):
        scopes = {"mcp": f"Call {settings.BRAND_NAME} MCP tools on your behalf"}
        scopes.update({f"{caps.SCOPE_PREFIX}{c.key}": c.label for c in caps.CAPABILITIES})
        return scopes

    def get_available_scopes(self, application=None, request=None, *args, **kwargs):
        return list(self.get_all_scopes())

    def get_default_scopes(self, application=None, request=None, *args, **kwargs):
        return ["mcp"]
