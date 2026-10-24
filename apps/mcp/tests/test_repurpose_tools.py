"""MCP repurposing tools: the twins of /api/v1/repurpose/jobs/*."""

from __future__ import annotations

import json

import pytest
from django.test import Client
from django.utils import timezone

from apps.ai_writer.models import AIRequestLog
from apps.ai_writer.tests.fakes import patch_anthropic
from apps.api_keys import services as key_services
from apps.members.models import PERMISSION_KEYS, OrgMembership, WorkspaceMembership
from apps.repurpose import services
from apps.repurpose.models import RepurposeJob

pytestmark = pytest.mark.django_db

TEXT = "Slow roasting keeps the sweetness in the bean. We roast every batch for eighteen minutes at lower heat."


MCP_URL = "/api/v1/mcp/"


class _SecureClient(Client):
    def generic(self, method, path, *args, **kwargs):
        kwargs["secure"] = True
        return super().generic(method, path, *args, **kwargs)


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
        email="mcp-rp@example.com", password="x", name="MCP RP", tos_accepted_at=timezone.now()
    )
    organization = Organization.objects.create(name="Org")
    workspace = Workspace.objects.create(name="WS", organization=organization)
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
    key = key_services.issue_api_key(
        workspace=workspace, social_accounts=[account], issued_by=user, name="rp", permissions=list(PERMISSION_KEYS)
    )
    return _SecureClient(HTTP_AUTHORIZATION=f"Bearer {key.plaintext_token}"), account, user


def _call(client, name, arguments):
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    return client.post(MCP_URL, data=json.dumps(payload), content_type="application/json").json()


def _payload(body):
    assert "error" not in body, body
    return json.loads(body["result"]["content"][0]["text"])


def test_tools_are_listed(setup):
    client, _account, _user = setup
    r = client.post(
        MCP_URL,
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}),
        content_type="application/json",
    )
    names = {t["name"] for t in r.json()["result"]["tools"]}
    assert {"create_repurpose_job", "get_repurpose_job", "accept_repurpose_outputs", "cancel_repurpose_job"} <= names


def test_create_get_accept(setup, fake_claude):
    client, account, user = setup
    fake_claude.queue({"parts": ["Hook.", "Second."]})
    job = _payload(
        _call(
            client,
            "create_repurpose_job",
            {
                "source_type": "text",
                "text": TEXT,
                "outputs": [{"kind": "thread", "social_account_ids": [str(account.id)]}],
            },
        )
    )
    assert job["status"] == "queued"
    services.execute_job(job["id"])
    got = _payload(_call(client, "get_repurpose_job", {"job_id": job["id"]}))
    assert got["status"] == "succeeded" and got["outputs"][0]["parts"] == ["Second."]
    assert AIRequestLog.objects.get().source == "mcp"
    accepted = _payload(
        _call(client, "accept_repurpose_outputs", {"job_id": job["id"], "output_ids": [got["outputs"][0]["id"]]})
    )
    assert accepted["outputs"][0]["status"] == "accepted" and accepted["outputs"][0]["post_id"]


def test_cancel(setup, fake_claude):
    client, _account, _user = setup
    job = _payload(
        _call(
            client, "create_repurpose_job", {"source_type": "text", "text": TEXT, "outputs": [{"kind": "newsletter"}]}
        )
    )
    assert _payload(_call(client, "cancel_repurpose_job", {"job_id": job["id"]}))["status"] == "cancelled"
    assert fake_claude.calls == []


def test_bad_arguments_are_readable_errors(setup):
    client, _account, _user = setup
    body = _call(client, "create_repurpose_job", {"source_type": "text", "text": TEXT, "outputs": [{"kind": "nope"}]})
    assert "is not one of" in body["error"]["message"]
    body = _call(client, "create_repurpose_job", {"source_type": "text", "text": TEXT, "outputs": [{"kind": "post"}]})
    assert "Choose at least one account" in body["error"]["message"]
    assert RepurposeJob.objects.count() == 0


def test_unknown_job_is_not_found(setup):
    client, _account, _user = setup
    body = _call(client, "get_repurpose_job", {"job_id": "00000000-0000-0000-0000-000000000000"})
    assert "not found" in body["error"]["message"]
