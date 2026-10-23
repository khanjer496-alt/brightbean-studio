"""Agent REST API and MCP: list reports, run one, read the latest run."""

from __future__ import annotations

import json

import pytest
from django.test import Client

from apps.api_keys import services as key_services
from apps.billing.tests.support import set_plan
from apps.members.models import PERMISSION_KEYS
from apps.reports import services
from apps.reports.models import ReportRun

pytestmark = pytest.mark.django_db


class SecureClient(Client):
    def generic(self, method, path, *args, **kwargs):
        kwargs["secure"] = True
        return super().generic(method, path, *args, **kwargs)


@pytest.fixture(autouse=True)
def _clear_throttle():
    from django.core.cache import cache

    cache.delete("agent_api:auth_fail:127.0.0.1")


def make_client(workspace, owner, accounts, permissions):
    key = key_services.issue_api_key(
        workspace=workspace, social_accounts=list(accounts), issued_by=owner, name="rep", permissions=permissions
    )
    return SecureClient(HTTP_AUTHORIZATION=f"Bearer {key.plaintext_token}")


@pytest.fixture
def api(workspace, owner, seeded, definition):
    return make_client(workspace, owner, seeded, list(PERMISSION_KEYS))


@pytest.fixture
def read_only_api(workspace, owner, seeded, definition):
    return make_client(workspace, owner, seeded, ["view_analytics"])


@pytest.fixture
def narrow_api(workspace, owner, seeded, definition):
    """A key allowlisted on Instagram only."""
    return make_client(workspace, owner, seeded[:1], list(PERMISSION_KEYS))


def rpc(client, name, arguments=None):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments or {}}}
    return client.post("/api/v1/mcp/", data=json.dumps(body), content_type="application/json").json()


def text(body):
    return json.loads(body["result"]["content"][0]["text"])


class TestRest:
    def test_list(self, api, definition):
        r = api.get("/api/v1/reports/")
        assert r.status_code == 200
        [item] = r.json()["reports"]
        assert r.json()["next_cursor"] is None
        assert item["id"] == str(definition.id) and item["recipients"] == 1 and item["latest_run"] is None

    def test_run_then_latest(self, api, definition):
        assert api.get(f"/api/v1/reports/{definition.id}/runs/latest").status_code == 404
        r = api.post(f"/api/v1/reports/{definition.id}/run")
        assert r.status_code == 202 and r.json()["status"] == "pending"
        services.execute_run(r.json()["id"])
        latest = api.get(f"/api/v1/reports/{definition.id}/runs/latest").json()
        assert latest["status"] == "succeeded" and latest["metrics"] and latest["summary"]

    def test_run_needs_manage_permission(self, read_only_api, definition):
        assert read_only_api.post(f"/api/v1/reports/{definition.id}/run").status_code == 403
        assert read_only_api.get("/api/v1/reports/").status_code == 200

    def test_other_workspace_report_is_404(self, api, workspace):
        from apps.reports.models import ReportDefinition
        from apps.workspaces.models import Workspace

        other = Workspace.objects.create(organization=workspace.organization, name="Other")
        d = ReportDefinition.objects.create(workspace=other, name="X")
        assert api.post(f"/api/v1/reports/{d.id}/run").status_code == 404

    def test_allowlist_hides_reports_covering_other_accounts(self, narrow_api, definition, seeded):
        # The definition covers every account, LinkedIn included: a key limited to Instagram can't see it.
        assert narrow_api.get("/api/v1/reports/").json()["reports"] == []
        assert narrow_api.get(f"/api/v1/reports/{definition.id}/runs/latest").status_code == 404
        assert narrow_api.post(f"/api/v1/reports/{definition.id}/run").status_code == 404
        definition.social_accounts.set([seeded[0]])
        [item] = narrow_api.get("/api/v1/reports/").json()["reports"]
        assert item["id"] == str(definition.id)
        definition.social_accounts.add(seeded[1])
        assert narrow_api.get("/api/v1/reports/").json()["reports"] == []

    def test_run_now_is_rate_limited(self, api, definition, monkeypatch):
        monkeypatch.setattr(services, "MANUAL_RUNS_PER_HOUR", 1)
        assert api.post(f"/api/v1/reports/{definition.id}/run").status_code == 202
        r = api.post(f"/api/v1/reports/{definition.id}/run")
        assert r.status_code == 429 and "last hour" in r.json()["detail"]

    def test_plan_gate(self, api, definition, settings, workspace):
        settings.BILLING_ENABLED = True
        set_plan(workspace.organization, "team")
        assert api.get("/api/v1/reports/").status_code in (402, 403)

    def test_requires_auth(self, definition):
        assert Client().get("/api/v1/reports/", secure=True).status_code == 401


class TestMcp:
    def test_tools_listed(self, api):
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        names = {
            t["name"]
            for t in api.post("/api/v1/mcp/", data=json.dumps(body), content_type="application/json").json()["result"][
                "tools"
            ]
        }
        assert {"list_reports", "run_report", "get_latest_report_run"} <= names

    def test_flow_matches_rest(self, api, definition):
        assert text(rpc(api, "list_reports")) == api.get("/api/v1/reports/").json()
        run = text(rpc(api, "run_report", {"report_id": str(definition.id)}))
        assert run["status"] == "pending"
        services.execute_run(run["id"])
        latest = text(rpc(api, "get_latest_report_run", {"report_id": str(definition.id)}))
        rest = api.get(f"/api/v1/reports/{definition.id}/runs/latest").json()
        # MCP serialises with pydantic, REST with Ninja: same data, different datetime precision.
        for body in (latest, rest):
            body.pop("created_at"), body.pop("finished_at")
        assert latest == rest
        assert ReportRun.objects.count() == 1

    def test_run_needs_manage_permission(self, read_only_api, definition):
        body = rpc(read_only_api, "run_report", {"report_id": str(definition.id)})
        assert "error" in body and "Permission denied" in body["error"]["message"]

    def test_allowlist_applies(self, narrow_api, definition):
        assert text(rpc(narrow_api, "list_reports"))["reports"] == []
        body = rpc(narrow_api, "run_report", {"report_id": str(definition.id)})
        assert "error" in body and "not found" in body["error"]["message"]

    def test_unknown_report(self, api):
        body = rpc(api, "get_latest_report_run", {"report_id": "00000000-0000-0000-0000-000000000000"})
        assert "error" in body
