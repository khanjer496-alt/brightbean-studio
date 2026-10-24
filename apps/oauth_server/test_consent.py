"""The MCP consent screen: capabilities in plain language, narrowed by the user, stored on the grant."""

from __future__ import annotations

import base64
import hashlib
import json
from urllib.parse import parse_qs, urlparse

import pytest
from django.test import Client
from django.utils.html import escape

from apps.api_keys import capabilities as caps
from apps.mcp.tests.test_oauth_auth import CLAUDE_REDIRECT, _make_user_with_workspace
from apps.members.models import WorkspaceMembership

pytestmark = pytest.mark.django_db

VERIFIER = "v" * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip("=")


class SecureClient(Client):
    def generic(self, method, path, *args, **kwargs):
        kwargs["secure"] = True
        return super().generic(method, path, *args, **kwargs)


@pytest.fixture
def signed_in():
    user, workspace, _account = _make_user_with_workspace(
        "consent@example.com", WorkspaceMembership.WorkspaceRole.OWNER
    )
    client = SecureClient()
    client.force_login(user)
    return client, user, workspace


@pytest.fixture
def application():
    from oauth2_provider.models import get_application_model

    model = get_application_model()
    return model.objects.create(
        name="Claude Desktop",
        client_type=model.CLIENT_PUBLIC,
        authorization_grant_type=model.GRANT_AUTHORIZATION_CODE,
        redirect_uris=CLAUDE_REDIRECT,
    )


def authorize_params(application, scope="mcp", **extra):
    return {
        "client_id": application.client_id,
        "response_type": "code",
        "redirect_uri": CLAUDE_REDIRECT,
        "scope": scope,
        "state": "xyz",
        "code_challenge": CHALLENGE,
        "code_challenge_method": "S256",
        **extra,
    }


def consent_post(client, application, chosen, scope="mcp", allow=True):
    data = {**authorize_params(application, scope), "capability": chosen}
    if allow:
        data["allow"] = "Authorize"
    return client.post("/oauth/authorize/", data=data)


def exchange(application, response):
    assert response.status_code == 302, response.content[:500]
    code = parse_qs(urlparse(response["Location"]).query)["code"][0]
    token_response = Client().post(
        "/oauth/token/",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": CLAUDE_REDIRECT,
            "client_id": application.client_id,
            "code_verifier": VERIFIER,
        },
    )
    assert token_response.status_code == 200, token_response.content
    return token_response.json()


def list_tools(token):
    client = SecureClient(HTTP_AUTHORIZATION=f"Bearer {token}")
    response = client.post(
        "/api/v1/mcp/",
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
        content_type="application/json",
    )
    return {t["name"] for t in response.json()["result"]["tools"]}


class TestConsentScreen:
    def test_lists_every_capability_in_plain_language(self, signed_in, application):
        client, *_ = signed_in
        response = client.get("/oauth/authorize/", authorize_params(application))
        html = response.content.decode()
        assert response.status_code == 200
        for cap in caps.CAPABILITIES:
            assert escape(cap.label) in html and escape(cap.summary) in html
        assert html.count('name="capability"') == len(caps.CAPABILITIES)

    def test_high_risk_capabilities_start_unticked_and_the_rest_ticked(self, signed_in, application):
        client, *_ = signed_in
        ctx = client.get("/oauth/authorize/", authorize_params(application)).context
        ticked = {c["cap"].key for c in ctx["capability_choices"] if c["checked"]}
        assert ticked == {c.key for c in caps.CAPABILITIES if c.default_on}
        assert "schedule_posts" not in ticked and "write_settings" not in ticked

    def test_a_client_that_names_capabilities_is_offered_exactly_those(self, signed_in, application):
        client, *_ = signed_in
        scope = "mcp cap:read_calendar cap:draft_posts"
        ctx = client.get("/oauth/authorize/", authorize_params(application, scope)).context
        assert [c["cap"].key for c in ctx["capability_choices"]] == ["read_calendar", "draft_posts"]
        assert all(c["checked"] for c in ctx["capability_choices"])

    def test_a_client_asking_for_high_risk_capabilities_gets_them_offered_unticked(self, signed_in, application):
        client, *_ = signed_in
        scope = "mcp cap:read_calendar cap:schedule_posts cap:write_settings cap:reply_inbox"
        response = client.get("/oauth/authorize/", authorize_params(application, scope))
        ticked = {c["cap"].key for c in response.context["capability_choices"] if c["checked"]}
        assert ticked == {"read_calendar"}
        assert response.content.decode().count("High risk</span>") == 3

    def test_the_screen_is_protected_against_cross_site_posts(self, signed_in, application):
        from django.contrib.auth import get_user_model

        _, user, _ = signed_in
        forged = SecureClient(enforce_csrf_checks=True)
        forged.force_login(get_user_model().objects.get(pk=user.pk))
        data = {**authorize_params(application), "capability": ["read_calendar"], "allow": "Authorize"}
        assert forged.post("/oauth/authorize/", data=data).status_code == 403

    def test_a_returning_client_still_sees_the_screen(self, signed_in, application):
        client, *_ = signed_in
        exchange(application, consent_post(client, application, ["read_calendar"]))
        again = client.get("/oauth/authorize/", authorize_params(application))
        assert again.status_code == 200  # not a silent 302 on the earlier grant

    def test_a_client_cannot_skip_the_screen_with_approval_prompt_auto(self, signed_in, application):
        client, *_ = signed_in
        exchange(application, consent_post(client, application, ["read_calendar"]))
        again = client.get("/oauth/authorize/", authorize_params(application, approval_prompt="auto"))
        assert again.status_code == 200


class TestNarrowingIsStored:
    def test_the_grant_carries_only_the_ticked_capabilities(self, signed_in, application):
        client, *_ = signed_in
        tokens = exchange(application, consent_post(client, application, ["read_calendar", "draft_posts"]))
        assert set(tokens["scope"].split()) == {"mcp", "cap:read_calendar", "cap:draft_posts"}

    def test_mcp_tools_list_shows_only_what_was_granted(self, signed_in, application):
        client, *_ = signed_in
        tokens = exchange(application, consent_post(client, application, ["read_calendar"]))
        assert list_tools(tokens["access_token"]) == {
            n for n, c in caps.TOOL_CAPABILITY.items() if c == "read_calendar"
        }

    def test_a_tool_outside_the_grant_is_refused(self, signed_in, application):
        client, *_ = signed_in
        tokens = exchange(application, consent_post(client, application, ["read_calendar"]))
        response = SecureClient(HTTP_AUTHORIZATION=f"Bearer {tokens['access_token']}").post(
            "/api/v1/mcp/",
            data=json.dumps(
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "create_draft", "arguments": {}}}
            ),
            content_type="application/json",
        )
        assert response.json()["error"]["message"] == "Missing capability: draft_posts"

    def test_the_narrowed_set_survives_a_refresh(self, signed_in, application):
        client, *_ = signed_in
        tokens = exchange(application, consent_post(client, application, ["read_calendar"]))
        refreshed = Client().post(
            "/oauth/token/",
            data={
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": application.client_id,
            },
        )
        assert refreshed.status_code == 200, refreshed.content
        assert list_tools(refreshed.json()["access_token"]) == {
            n for n, c in caps.TOOL_CAPABILITY.items() if c == "read_calendar"
        }

    def test_permissions_are_withdrawn_with_their_capabilities(self, signed_in, application):
        from apps.api.auth import _resolve_oauth_actor

        client, *_ = signed_in
        tokens = exchange(application, consent_post(client, application, ["read_calendar"]))
        actor = _resolve_oauth_actor(tokens["access_token"])
        assert actor.capabilities == ["read_calendar"]
        assert actor.effective_permissions["publish_directly"] is False
        assert actor.effective_permissions["approve_posts"] is False
        assert actor.effective_permissions["reply_from_inbox"] is False

    def test_only_offered_capabilities_can_be_granted(self, signed_in, application):
        client, *_ = signed_in
        scope = "mcp cap:read_calendar"
        tokens = exchange(application, consent_post(client, application, ["read_calendar", "write_settings"], scope))
        assert set(tokens["scope"].split()) == {"mcp", "cap:read_calendar"}

    def test_unknown_capabilities_are_ignored(self, signed_in, application):
        client, *_ = signed_in
        tokens = exchange(application, consent_post(client, application, ["read_calendar", "launch_missiles"]))
        assert set(tokens["scope"].split()) == {"mcp", "cap:read_calendar"}

    def test_approving_with_nothing_ticked_shows_the_screen_again_and_grants_nothing(self, signed_in, application):
        from oauth2_provider.models import get_grant_model

        client, *_ = signed_in
        response = consent_post(client, application, [])
        assert response.status_code == 200
        assert "Choose at least one" in response.content.decode()
        assert not get_grant_model().objects.exists()

    def test_cancel_denies_the_request(self, signed_in, application):
        client, *_ = signed_in
        response = consent_post(client, application, ["read_calendar"], allow=False)
        assert response.status_code == 302
        assert "error=access_denied" in response["Location"]


class TestGrantsFromBeforeNarrowing:
    def test_a_token_with_only_the_mcp_scope_is_not_limited(self):
        from apps.api.auth import _resolve_oauth_actor
        from apps.mcp.tests.test_oauth_auth import _mint_oauth_token

        user, *_ = _make_user_with_workspace("old@example.com", WorkspaceMembership.WorkspaceRole.OWNER)
        raw = _mint_oauth_token(user)
        actor = _resolve_oauth_actor(raw)
        assert actor.capabilities is None
        assert list_tools(raw) == set(caps.TOOL_CAPABILITY)


def refresh(application, refresh_token, scope=None):
    data = {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": application.client_id}
    if scope is not None:
        data["scope"] = scope
    return Client().post("/oauth/token/", data=data)


def connection_for(user, application):
    from apps.api_keys.models import ConnectedApp

    return ConnectedApp.objects.get(user=user, application=application)


class TestTheConnectionHoldsTheLimits:
    def test_consent_records_the_capabilities_and_approval_mode(self, signed_in, application):
        client, user, _ = signed_in
        data = {**authorize_params(application), "capability": ["read_calendar"], "allow": "Authorize"}
        exchange(application, client.post("/oauth/authorize/", data={**data, "requires_approval": "1"}))
        connection = connection_for(user, application)
        assert connection.capabilities == ["read_calendar"]
        assert connection.requires_approval is True

    def test_a_refresh_that_drops_the_capability_scopes_cannot_widen_the_grant(self, signed_in, application):
        from apps.api.auth import _resolve_oauth_actor

        client, *_ = signed_in
        tokens = exchange(application, consent_post(client, application, ["read_calendar"]))
        widened = refresh(application, tokens["refresh_token"], scope="mcp")
        assert widened.status_code == 200, widened.content
        assert widened.json()["scope"] == "mcp"
        assert _resolve_oauth_actor(widened.json()["access_token"]).capabilities == ["read_calendar"]

    def test_a_refresh_cannot_ask_for_a_capability_that_was_not_granted(self, signed_in, application):
        client, *_ = signed_in
        tokens = exchange(application, consent_post(client, application, ["read_calendar"]))
        response = refresh(application, tokens["refresh_token"], scope="mcp cap:read_calendar cap:schedule_posts")
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_scope"

    def test_revoking_stops_the_access_token_and_the_refresh_token(self, signed_in, application):
        from apps.api.auth import _resolve_oauth_actor
        from apps.api_keys import services

        client, user, _ = signed_in
        tokens = exchange(application, consent_post(client, application, ["read_calendar"]))
        services.revoke_connected_app(connection_for(user, application))
        assert _resolve_oauth_actor(tokens["access_token"]) is None
        assert refresh(application, tokens["refresh_token"]).status_code == 400

    def test_an_expired_connection_refuses_its_tokens_and_cannot_refresh(self, signed_in, application):
        from datetime import timedelta

        from django.utils import timezone

        from apps.api.auth import _resolve_oauth_actor

        client, user, _ = signed_in
        tokens = exchange(application, consent_post(client, application, ["read_calendar"]))
        connection = connection_for(user, application)
        connection.expires_at = timezone.now() - timedelta(minutes=1)
        connection.save(update_fields=["expires_at"])
        assert _resolve_oauth_actor(tokens["access_token"]) is None
        assert refresh(application, tokens["refresh_token"]).status_code == 400

    def test_approving_again_after_a_revocation_reconnects(self, signed_in, application):
        from apps.api.auth import _resolve_oauth_actor
        from apps.api_keys import services

        client, user, _ = signed_in
        exchange(application, consent_post(client, application, ["read_calendar"]))
        services.revoke_connected_app(connection_for(user, application))
        tokens = exchange(application, consent_post(client, application, ["read_calendar"]))
        assert _resolve_oauth_actor(tokens["access_token"]) is not None

    def test_the_request_budget_applies_to_the_connection(self, signed_in, application):
        client, user, _ = signed_in
        tokens = exchange(application, consent_post(client, application, ["read_calendar"]))
        connection = connection_for(user, application)
        connection.daily_request_budget = 1
        connection.save(update_fields=["daily_request_budget"])
        assert list_tools(tokens["access_token"])
        response = SecureClient(HTTP_AUTHORIZATION=f"Bearer {tokens['access_token']}").post(
            "/api/v1/mcp/",
            data=json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
            content_type="application/json",
        )
        assert response.status_code == 429

    def test_read_only_until_approved_reaches_the_autonomy_gate(self, signed_in, application):
        from apps.api.auth import _resolve_oauth_actor

        client, user, _ = signed_in
        data = {**authorize_params(application), "capability": ["read_calendar"], "allow": "Authorize"}
        tokens = exchange(application, client.post("/oauth/authorize/", data={**data, "requires_approval": "1"}))
        actor = _resolve_oauth_actor(tokens["access_token"])
        assert actor.requires_approval is True
        assert actor.connection == connection_for(user, application)
