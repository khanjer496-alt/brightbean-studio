"""Custom OAuth2 validator that restricts PKCE to S256 only (and requires it of every public client).

django-oauth-toolkit's default validator (and oauthlib under it) accepts both
``S256`` and ``plain`` for ``code_challenge_method``. RFC 7636 §4.2 marks
``plain`` as insecure unless the channel is fully secure end-to-end — which
defeats the entire point of PKCE for the kind of MCP-client scenarios we
support. The protected-resource metadata document advertises ``S256`` as the
only supported method (see ``apps.oauth_server.metadata``); this validator
enforces that contract at the authorize endpoint, blocking authorization
requests with a missing or ``plain`` method early — before any Grant row is
written.
"""

from __future__ import annotations

from oauth2_provider.oauth2_validators import OAuth2Validator
from oauthlib.oauth2.rfc6749 import errors as oauthlib_errors


class S256OnlyOAuth2Validator(OAuth2Validator):
    """OAuth2Validator subclass that requires ``code_challenge_method=S256``.

    Hooks ``is_pkce_required`` because it is the earliest validator method
    oauthlib invokes while ``request.code_challenge_method`` is still the
    raw client-supplied value (oauthlib later defaults a missing method to
    ``"plain"`` at ``authorization_code.py``; we reject before that lands).
    """

    def is_pkce_required(self, client_id, request):
        code_challenge = getattr(request, "code_challenge", None)
        method = getattr(request, "code_challenge_method", None)
        if code_challenge and method != "S256":
            raise oauthlib_errors.InvalidRequestError(
                description="code_challenge_method must be S256",
                request=request,
            )
        if not super().is_pkce_required(client_id, request):
            return False
        # A confidential client (registered by an admin, ``manage.py oauth_client``) proves itself with its secret
        # at the token endpoint, and some (Microsoft Power Platform connectors) send no PKCE. PKCE stays mandatory
        # for every public client, which is every client dynamic registration creates.
        client = getattr(request, "client", None)
        if client is None or getattr(client, "client_id", None) != client_id:
            from oauth2_provider.models import get_application_model

            client = get_application_model().objects.filter(client_id=client_id).first()
        return client is None or client.client_type != client.CLIENT_CONFIDENTIAL

    def validate_refresh_token(self, refresh_token, client, request, *args, **kwargs):
        """Refuse a refresh token whose connected app was disconnected or has expired.

        Disconnecting already revokes the app's refresh tokens; this is the second lock, and the one that makes an
        expiry set on the API keys page stop the client from minting new access tokens.
        """
        if not super().validate_refresh_token(refresh_token, client, request, *args, **kwargs):
            return False
        from apps.api_keys import capabilities as caps
        from apps.api_keys.models import ConnectedApp

        mine = ConnectedApp.objects.filter(user=request.user, application=client)
        original = self.get_original_scopes(refresh_token, request) or ""
        org_id = caps.org_from_scope(original if isinstance(original, str) else " ".join(original))
        if org_id is None:
            # From before connected apps: fine until the person connects this client through the consent screen.
            return not mine.exists()
        connection = mine.filter(organization_id=org_id).first()
        return connection is not None and connection.is_active

    def validate_scopes(self, client_id, scopes, client, request, *args, **kwargs):
        """Accept the ``org:`` scope the consent screen adds (and a refresh keeps).

        A client can't use this to pick an organization: the consent screen drops any ``org:`` scope from the
        request and adds the one for the person's own organization, and a refresh can only keep original scopes.
        """
        from apps.api_keys import capabilities as caps

        scopes = [s for s in scopes if not s.startswith(caps.ORG_SCOPE_PREFIX)]
        return super().validate_scopes(client_id, scopes, client, request, *args, **kwargs)
