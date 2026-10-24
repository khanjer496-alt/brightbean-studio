"""MCP ``generate_caption`` / ``adapt_caption`` — same services as REST /ai/*."""

from __future__ import annotations

import json

import pytest
from django.test import Client
from django.utils import timezone

from apps.ai_writer.models import AIRequestLog
from apps.ai_writer.tests.fakes import patch_anthropic
from apps.api_keys import services
from apps.members.models import PERMISSION_KEYS, OrgMembership, WorkspaceMembership

MCP_URL = "/api/v1/mcp/"


class _SecureClient(Client):
    def generic(self, method, path, *args, **kwargs):
        kwargs["secure"] = True
        return super().generic(method, path, *args, **kwargs)


def _rpc(method, params):
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}


@pytest.fixture(autouse=True)
def fake_claude():
    with patch_anthropic() as fake:
        yield fake


@pytest.fixture
def setup(db, settings):
    from apps.accounts.models import User
    from apps.organizations.models import Organization
    from apps.social_accounts.models import SocialAccount
    from apps.workspaces.models import Workspace

    settings.ANTHROPIC_API_KEY = "sk-ant-platform-key-for-tests"
    user = User.objects.create_user(
        email="mcp-ai@example.com", password="x", name="MCP AI", tos_accepted_at=timezone.now()
    )
    organization = Organization.objects.create(name="Org")
    workspace = Workspace.objects.create(name="WS", organization=organization, brand_voice_tone="Dry humour.")
    OrgMembership.objects.create(user=user, organization=organization, org_role=OrgMembership.OrgRole.OWNER)
    WorkspaceMembership.objects.create(
        user=user, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
    )
    account = SocialAccount.objects.create(
        workspace=workspace,
        platform="bluesky",
        account_platform_id="b-1",
        account_name="Sky",
        connection_status="connected",
        oauth_access_token="tok",
    )
    key = services.issue_api_key(
        workspace=workspace, social_accounts=[account], issued_by=user, name="ai", permissions=list(PERMISSION_KEYS)
    )
    client = _SecureClient(HTTP_AUTHORIZATION=f"Bearer {key.plaintext_token}")
    return client, account, user


def _call(client, name, arguments):
    r = client.post(
        MCP_URL,
        data=json.dumps(_rpc("tools/call", {"name": name, "arguments": arguments})),
        content_type="application/json",
    )
    return r.json()


def test_tools_are_listed(setup):
    client, _account, _user = setup
    r = client.post(MCP_URL, data=json.dumps(_rpc("tools/list", {})), content_type="application/json")
    names = {t["name"] for t in r.json()["result"]["tools"]}
    assert {"generate_caption", "adapt_caption"} <= names


def test_generate_caption(setup, fake_claude):
    client, account, user = setup
    fake_claude.queue({"text": "Well, that happened."})
    body = _call(client, "generate_caption", {"prompt": "launch", "social_account_ids": [str(account.id)]})
    assert json.loads(body["result"]["content"][0]["text"]) == {"caption": "Well, that happened."}
    assert "Dry humour." in fake_claude.systems[0]
    log = AIRequestLog.objects.get()
    assert (log.source, log.user_id) == ("mcp", user.id)


def test_adapt_caption(setup, fake_claude):
    client, account, _user = setup
    fake_claude.queue({"captions": [{"account_id": str(account.id), "caption": "word " * 100}]}, {"text": "Short."})
    body = _call(client, "adapt_caption", {"caption": "Launch", "social_account_ids": [str(account.id)]})
    payload = json.loads(body["result"]["content"][0]["text"])
    assert payload["captions"][0]["caption"] == "Short."
    assert payload["captions"][0]["char_limit"] == 300


def test_errors_are_readable(setup, settings, fake_claude):
    client, _account, _user = setup
    settings.ANTHROPIC_API_KEY = ""
    body = _call(client, "generate_caption", {"prompt": "launch"})
    assert "Connect your AI" in body["error"]["message"]
    assert fake_claude.calls == []
