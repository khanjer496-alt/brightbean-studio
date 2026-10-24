"""Admin-registered confidential clients, loopback redirects on any port, and the consent screen's redirect host."""

from __future__ import annotations

import base64
from io import StringIO
from urllib.parse import parse_qs, urlparse

import pytest
from django.core.management import CommandError, call_command
from django.test import Client
from oauth2_provider.models import get_application_model, redirect_to_uri_allowed

from apps.oauth_server.test_consent import CHALLENGE, authorize_params, signed_in  # noqa: F401 - fixture

pytestmark = pytest.mark.django_db

COPILOT_REDIRECT = "https://global.consent.azure-apim.net/redirect/postdelegate"


def _create(*uris, name="Microsoft Copilot Studio"):
    out = StringIO()
    args = ["create", "--name", name]
    for uri in uris:
        args += ["--redirect-uri", uri]
    call_command("oauth_client", *args, stdout=out)
    lines = dict(line.split(":", 1) for line in out.getvalue().splitlines() if ":" in line and "secret is" not in line)
    return lines["client_id"].strip(), lines["client_secret"].strip()


def _public(redirect):
    model = get_application_model()
    return model.objects.create(
        name="Claude",
        client_type=model.CLIENT_PUBLIC,
        authorization_grant_type=model.GRANT_AUTHORIZATION_CODE,
        redirect_uris=redirect,
    )


def _basic(client_id, secret):
    return "Basic " + base64.b64encode(f"{client_id}:{secret}".encode()).decode()


class TestOAuthClientCommand:
    def test_create_stores_a_hashed_secret_shown_once(self):
        client_id, secret = _create(COPILOT_REDIRECT)
        app = get_application_model().objects.get(client_id=client_id)
        assert app.client_type == app.CLIENT_CONFIDENTIAL
        assert app.client_secret != secret and secret not in app.client_secret
        out = StringIO()
        call_command("oauth_client", "list", stdout=out)
        assert client_id in out.getvalue() and secret not in out.getvalue()

    def test_rotate_replaces_the_secret(self):
        client_id, old = _create(COPILOT_REDIRECT)
        out = StringIO()
        call_command("oauth_client", "rotate", "--client-id", client_id, stdout=out)
        new = out.getvalue().split("client_secret:")[1].split()[0]
        assert new != old
        app = get_application_model().objects.get(client_id=client_id)
        assert old not in app.client_secret and new not in app.client_secret

    def test_refuses_a_non_loopback_http_redirect(self):
        with pytest.raises(CommandError):
            _create("http://evil.example/cb")

    def test_rotate_refuses_a_public_client(self):
        app = _public("https://claude.ai/api/mcp/auth_callback")
        with pytest.raises(CommandError):
            call_command("oauth_client", "rotate", "--client-id", app.client_id, stdout=StringIO())


class TestConfidentialFlow:
    def _code(self, client, client_id, **extra):
        params = {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": COPILOT_REDIRECT,
            "scope": "mcp",
            "state": "s",
            **extra,
        }
        response = client.post("/oauth/authorize/", {**params, "capability": ["read_calendar"], "allow": "Authorize"})
        assert response.status_code == 302, response.content[:500]
        return parse_qs(urlparse(response["Location"]).query)

    def test_confidential_client_without_pkce_needs_its_secret(self, signed_in):  # noqa: F811
        client, *_ = signed_in
        client_id, secret = _create(COPILOT_REDIRECT)
        code = self._code(client, client_id)["code"][0]
        body = {"grant_type": "authorization_code", "code": code, "redirect_uri": COPILOT_REDIRECT}
        assert Client().post("/oauth/token/", {**body, "client_id": client_id}).status_code == 401
        wrong = Client().post("/oauth/token/", body, HTTP_AUTHORIZATION=_basic(client_id, "nope"))
        assert wrong.status_code == 401
        ok = Client().post("/oauth/token/", body, HTTP_AUTHORIZATION=_basic(client_id, secret))
        assert ok.status_code == 200, ok.content
        assert ok.json()["access_token"]

    def test_confidential_client_still_refuses_plain_pkce(self, signed_in):  # noqa: F811
        client, *_ = signed_in
        client_id, _secret = _create(COPILOT_REDIRECT)
        query = self._code(client, client_id, code_challenge="x" * 43, code_challenge_method="plain")
        assert query.get("error") == ["invalid_request"]

    def test_public_client_without_pkce_is_refused(self, signed_in):  # noqa: F811
        client, *_ = signed_in
        app = _public("https://claude.ai/api/mcp/auth_callback")
        params = authorize_params(app)
        del params["code_challenge"], params["code_challenge_method"]
        response = client.get("/oauth/authorize/", params)
        location = response.get("Location", "")
        assert "code=" not in location
        assert response.status_code in (302, 400) and ("error" in location or response.status_code == 400)


class TestLoopbackRedirects:
    @pytest.mark.parametrize(
        ("registered", "requested"),
        [
            ("http://127.0.0.1:33418/cb", "http://127.0.0.1:5555/cb"),
            ("http://[::1]:8787/cb", "http://[::1]:5555/cb"),
            ("http://localhost/cb", "http://localhost:5555/cb"),
        ],
    )
    def test_loopback_matches_on_any_port(self, registered, requested):
        assert redirect_to_uri_allowed(requested, [registered])

    def test_loopback_still_needs_the_same_host_and_path(self):
        assert not redirect_to_uri_allowed("http://127.0.0.1:5555/other", ["http://127.0.0.1:33418/cb"])
        assert not redirect_to_uri_allowed("http://localhost:5555/cb", ["http://127.0.0.1:33418/cb"])

    def test_https_redirects_still_match_exactly(self):
        assert not redirect_to_uri_allowed("https://claude.ai:8443/cb", ["https://claude.ai/cb"])


class TestConsentShowsWhereAccessGoes:
    def test_unverified_client_gets_a_warning_and_its_host(self, signed_in):  # noqa: F811
        client, *_ = signed_in
        app = _public("https://claude.ai/api/mcp/auth_callback https://agent.example.net/cb")
        html = client.get(
            "/oauth/authorize/", authorize_params(app, redirect_uri="https://agent.example.net/cb")
        ).content.decode()
        assert "agent.example.net" in html
        assert "has not verified this app" in html

    def test_known_ai_host_has_no_warning(self, signed_in):  # noqa: F811
        client, *_ = signed_in
        app = _public("https://claude.ai/api/mcp/auth_callback")
        html = client.get("/oauth/authorize/", authorize_params(app)).content.decode()
        assert "claude.ai" in html
        assert "has not verified this app" not in html

    def test_loopback_client_is_described_as_local(self, signed_in):  # noqa: F811
        client, *_ = signed_in
        app = _public("http://127.0.0.1:33418/cb")
        html = client.get(
            "/oauth/authorize/", authorize_params(app, redirect_uri="http://127.0.0.1:40000/cb")
        ).content.decode()
        assert "running on this computer" in html
        assert "has not verified this app" in html

    def test_admin_registered_client_is_verified(self, signed_in):  # noqa: F811
        client, *_ = signed_in
        client_id, _secret = _create("https://partner.example.org/cb", name="Partner")
        params = {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": "https://partner.example.org/cb",
            "scope": "mcp",
            "state": "s",
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
        }
        html = client.get("/oauth/authorize/", params).content.decode()
        assert "partner.example.org" in html
        assert "has not verified this app" not in html
