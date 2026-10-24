"""OAuth Authorization Server endpoints for the MCP connector flow.

django-oauth-toolkit supplies /oauth/authorize/ and /oauth/token/. This module
adds the two pieces it does not ship:
  - RFC 7591 Dynamic Client Registration (POST /oauth/register)
  - RFC 8414 / RFC 9728 discovery metadata documents

DCR is intentionally open — any MCP client may register. A registered client
still cannot obtain a token without a BrightBean Studio user completing login
and consent, so the exposure is bounded; registration is rate-limited only to
blunt row-spam abuse.
"""

from __future__ import annotations

import json
import logging
import time
from urllib.parse import urlparse

from django.core.cache import cache
from django.http import HttpResponseBadRequest, JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET
from oauth2_provider.models import get_application_model
from oauth2_provider.views import AuthorizationView

from apps.api_keys import capabilities as caps
from apps.api_keys import services

from .metadata import authorization_server_metadata, protected_resource_metadata

LOG = logging.getLogger(__name__)

_MAX_REDIRECT_URIS = 5
_SUPPORTED_GRANTS = {"authorization_code", "refresh_token"}
# Per-IP cap is the primary defense — one abuser shouldn't be able to block
# legitimate MCP client onboarding. The much-higher server-wide cap stays as
# a backstop against distributed spray that the per-IP limit alone wouldn't
# catch.
_DCR_PER_IP_LIMIT = 10
_DCR_GLOBAL_LIMIT = 1000
_DCR_RATE_WINDOW = 60 * 60  # seconds
_DCR_PER_IP_KEY_PREFIX = "oauth_dcr_ip:"
_DCR_GLOBAL_KEY = "oauth_dcr_registrations_global"


def _dcr_error(error: str, description: str, status: int = 400) -> JsonResponse:
    """RFC 7591 section 3.2.2 error response."""
    return JsonResponse(
        {"error": error, "error_description": description},
        status=status,
    )


def _client_ip(request) -> str:
    """Return the client IP used for rate-limiting.

    Uses ``REMOTE_ADDR`` directly. Behind a proxy this is the proxy address —
    accurate per-client attribution would require a vetted ``X-Forwarded-For``
    parsing chain, and we deliberately don't trust an arbitrary forwarded
    header here. The global backstop catches mass spray.
    """
    return request.META.get("REMOTE_ADDR", "unknown") or "unknown"


def _incr_counter(key: str) -> int:
    """Atomic increment with TTL bootstrap."""
    try:
        return cache.incr(key)
    except ValueError:
        cache.set(key, 1, _DCR_RATE_WINDOW)
        return 1


def _dcr_rate_limited(request) -> bool:
    """Best-effort per-IP cap with a server-wide backstop to blunt row-spam.

    Once an IP is over its per-IP cap we short-circuit *before* touching the
    global counter. Otherwise a single abuser would still burn through the
    global budget on every blocked request and would eventually 429 every
    legitimate client — the exact scenario the per-IP design exists to
    prevent.
    """
    per_ip_count = _incr_counter(f"{_DCR_PER_IP_KEY_PREFIX}{_client_ip(request)}")
    if per_ip_count > _DCR_PER_IP_LIMIT:
        return True
    global_count = _incr_counter(_DCR_GLOBAL_KEY)
    return global_count > _DCR_GLOBAL_LIMIT


_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _is_https_uri(value) -> bool:
    """A redirect URI a client may register: absolute https, or http on the loopback interface (RFC 8252 §7.3).

    Command-line and desktop clients (Claude Code, Cursor, VS Code) receive the code on a loopback port they pick at
    run time; django-oauth-toolkit matches a registered loopback URI on any port.
    """
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    if parsed.scheme == "https":
        return bool(parsed.netloc)
    return parsed.scheme == "http" and parsed.hostname in _LOOPBACK_HOSTS and not parsed.fragment


@method_decorator(csrf_exempt, name="dispatch")
class RegisterView(View):
    """RFC 7591 Dynamic Client Registration.

    Registers a public, authorization-code OAuth client (PKCE — no secret).
    """

    def post(self, request, *args, **kwargs):
        if _dcr_rate_limited(request):
            return _dcr_error(
                "temporarily_unavailable",
                "Registration rate limit reached. Retry later.",
                status=429,
            )

        try:
            body = json.loads(request.body.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return _dcr_error("invalid_client_metadata", "Request body must be JSON.")
        if not isinstance(body, dict):
            return _dcr_error(
                "invalid_client_metadata",
                "Request body must be a JSON object.",
            )

        redirect_uris = body.get("redirect_uris")
        if not isinstance(redirect_uris, list) or not redirect_uris:
            return _dcr_error("invalid_redirect_uri", "redirect_uris is required.")
        if len(redirect_uris) > _MAX_REDIRECT_URIS:
            return _dcr_error("invalid_redirect_uri", "Too many redirect_uris.")
        if not all(_is_https_uri(u) for u in redirect_uris):
            return _dcr_error(
                "invalid_redirect_uri",
                "Each redirect_uri must be an absolute https URL (or http on localhost, 127.0.0.1 or [::1]).",
            )

        grant_types = body.get("grant_types") or ["authorization_code"]
        if not isinstance(grant_types, list) or set(grant_types) - _SUPPORTED_GRANTS:
            return _dcr_error(
                "invalid_client_metadata",
                "Only authorization_code and refresh_token grants are supported.",
            )

        auth_method = body.get("token_endpoint_auth_method", "none")
        if auth_method != "none":
            return _dcr_error(
                "invalid_client_metadata",
                "Only public clients (token_endpoint_auth_method 'none') are supported.",
            )

        client_name = body.get("client_name") or "MCP client"
        if not isinstance(client_name, str):
            return _dcr_error("invalid_client_metadata", "client_name must be a string.")
        client_name = client_name[:255]

        application_model = get_application_model()
        app = application_model.objects.create(
            name=client_name,
            client_type=application_model.CLIENT_PUBLIC,
            authorization_grant_type=application_model.GRANT_AUTHORIZATION_CODE,
            redirect_uris=" ".join(redirect_uris),
            skip_authorization=False,
        )
        LOG.info(
            "OAuth DCR: registered client_id=%s name=%r",
            app.client_id,
            client_name,
        )

        return JsonResponse(
            {
                "client_id": app.client_id,
                "client_id_issued_at": int(time.time()),
                "redirect_uris": redirect_uris,
                "grant_types": sorted(set(grant_types) | {"authorization_code"}),
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
                "client_name": client_name,
                "scope": "mcp",
            },
            status=201,
        )


@require_GET
def authorization_server_metadata_view(request):
    """RFC 8414 — served at /.well-known/oauth-authorization-server."""
    return JsonResponse(authorization_server_metadata())


@require_GET
def protected_resource_metadata_view(request):
    """RFC 9728 — served at /.well-known/oauth-protected-resource[/api/v1/mcp]."""
    return JsonResponse(protected_resource_metadata())


def _requested_capabilities(scope: str) -> tuple[list[str], bool]:
    """What the client asked for as ``(capabilities, explicit)``.

    A client that names ``cap:`` scopes is offered exactly those. One that asks only for ``mcp`` has named
    nothing, so every capability is on offer. Either way only the low- and medium-risk ones start ticked.
    """
    asked = caps.from_scope(scope)
    return (asked, True) if asked else (list(caps.CAPABILITY_KEYS), False)


class ConsentAuthorizationView(AuthorizationView):
    """The consent screen, with the requested capabilities listed in plain language to narrow before approving.

    The ticked capabilities become ``cap:`` scopes on the grant, so the access token and its refresh tokens carry
    the narrowed set. The screen is always shown (``REQUEST_APPROVAL_PROMPT`` is ``force``), and a client cannot
    skip it by sending ``approval_prompt=auto``.
    """

    def get(self, request, *args, **kwargs):
        if caps.ORG_SCOPE_PREFIX in request.GET.get("scope", ""):
            # Only the consent screen ties a grant to an organization; a client can't choose one.
            return HttpResponseBadRequest("The org: scope can't be requested.")
        if "approval_prompt" in request.GET:
            request.GET = request.GET.copy()
            request.GET.pop("approval_prompt")
        return super().get(request, *args, **kwargs)

    def _target(self, application):
        """``(organization, connected app or None)`` this approval is for: the person's current organization."""
        from apps.api.auth import _resolve_active_membership
        from apps.api_keys.models import ConnectedApp

        membership = _resolve_active_membership(self.request.user)
        if membership is None or application is None:
            return None, None
        organization = membership.workspace.organization
        connection = ConnectedApp.objects.filter(
            user=self.request.user, application=application, organization=organization
        ).first()
        return organization, connection

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        form = context.get("form")
        scope = (form.initial.get("scope") if form is not None and not form.is_bound else None) or ""
        if form is not None and form.is_bound:
            scope = form.data.get("scope", "")
        organization, connection = self._target(context.get("application"))
        offered, _ = _requested_capabilities(scope)
        ceiling = connection.admin_capabilities if connection is not None else None
        if ceiling is not None:
            offered = [k for k in offered if k in ceiling]
        chosen = set(self.request.POST.getlist("capability")) if self.request.method == "POST" else None
        # What the client asked for limits what is offered, and so does the ceiling an admin set; nothing that
        # publishes, sends or changes settings starts ticked, whatever the client asked.
        context["capability_choices"] = [
            {"cap": c, "checked": (c.key in chosen) if chosen is not None else c.default_on}
            for c in caps.CAPABILITIES
            if c.key in offered
        ]
        context["limited_by_admin"] = ceiling is not None
        context["organization"] = organization
        context["blocked_by_admin"] = connection is not None and connection.admin_revoked_at is not None
        context["requires_approval"] = (
            self.request.method == "POST" and bool(self.request.POST.get("requires_approval"))
        ) or bool(connection is not None and connection.admin_requires_approval)
        context["approval_forced"] = bool(connection is not None and connection.admin_requires_approval)
        return context

    def form_valid(self, form):
        if not form.cleaned_data.get("allow"):
            return super().form_valid(form)  # Cancel: DOT answers access_denied.
        application = get_application_model().objects.get(client_id=form.cleaned_data["client_id"])
        organization, connection = self._target(application)
        requested = form.cleaned_data.get("scope") or ""
        offered, _ = _requested_capabilities(requested)
        if connection is not None and connection.admin_capabilities is not None:
            offered = [k for k in offered if k in connection.admin_capabilities]
        chosen = [k for k in caps.clean(self.request.POST.getlist("capability")) if k in offered]
        if organization is None:
            form.add_error(None, "You need a workspace before an app can connect to it.")
        elif connection is not None and connection.admin_revoked_at is not None:
            form.add_error(None, "An admin disconnected this app. Ask an admin of your organization to allow it again.")
        elif not chosen:
            # Tampered or empty: re-show the screen rather than issue a grant that names no capability,
            # which would read as "not narrowed".
            form.add_error(None, "Choose at least one thing the app may do, or cancel.")
        if form.errors:
            return self.render_to_response(self.get_context_data(form=form, **self._context_for(form)))
        base = [
            s
            for s in requested.split()
            if not s.startswith(caps.SCOPE_PREFIX) and not s.startswith(caps.ORG_SCOPE_PREFIX)
        ] or ["mcp"]
        # The ``org:`` scope ties every token of this grant to the organization's connected app.
        form.cleaned_data["scope"] = " ".join([*base, *caps.scopes_for(chosen), caps.org_scope(organization.id)])
        services.record_oauth_consent(
            user=self.request.user,
            application=application,
            organization=organization,
            capabilities=chosen,
            requires_approval=bool(self.request.POST.get("requires_approval")),
        )
        return super().form_valid(form)

    def _context_for(self, form) -> dict:
        """The template context ``get`` builds, rebuilt from the posted form for an error re-render."""
        application = get_application_model().objects.get(client_id=form.cleaned_data["client_id"])
        scopes = [s for s in (form.cleaned_data.get("scope") or "").split() if s == "mcp"]
        return {"application": application, "scopes": scopes, "scopes_descriptions": []}
