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
        assert actor.effective_permissions["create_posts"] is False
        assert actor.effective_permissions["publish_directly"] is False

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
