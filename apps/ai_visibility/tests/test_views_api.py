"""The AI visibility page, and the REST and MCP read of the latest report."""

from __future__ import annotations

import json

import pytest
from django.test import Client
from django.urls import reverse

from apps.ai_visibility import services
from apps.ai_visibility.models import VisibilityAnswer, VisibilityProfile, VisibilityRun
from apps.analytics.tests.helpers import make_account, make_member
from apps.api_keys import services as key_services
from apps.billing.tests.support import set_plan
from apps.composer.models import Idea
from apps.members.models import PERMISSION_KEYS, WorkspaceMembership
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace

from .conftest import BRAND

pytestmark = pytest.mark.django_db
ROLE = WorkspaceMembership.WorkspaceRole


def url(name, workspace, **kw):
    return reverse(f"ai_visibility:{name}", kwargs={"workspace_id": workspace.id, **kw})


@pytest.fixture
def owner(client, workspace):
    user = make_member(workspace, "owner@example.com")
    client.force_login(user)
    return user


@pytest.fixture
def report(profile):
    run = VisibilityRun.objects.create(
        profile=profile,
        status="complete",
        brand_name=BRAND,
        website=profile.website,
        engines={"anthropic": "claude-opus-5-5"},
        prompts_total=2,
        prompts_mentioned=1,
        mention_rate=50.0,
        suggested_ideas=[{"text": "A post for the Dubai gap", "idea_id": None}],
    )
    VisibilityAnswer.objects.create(
        run=run,
        kind="category",
        question="best scheduler in Dubai",
        engine="anthropic",
        model="claude-opus-5-5",
        answer=f"1. {BRAND} 2. Buffer",
        answered=True,
        mentioned=True,
        position=1,
        sentiment="positive",
        competitors=["Buffer"],
        cited_urls=["https://acme-scheduler.com"],
        cites_brand_site=True,
    )
    VisibilityAnswer.objects.create(
        run=run,
        kind="category",
        question="schedule Instagram posts",
        engine="anthropic",
        model="claude-opus-5-5",
        answer="Use Later.",
        answered=True,
        mentioned=False,
        competitors=["Later"],
    )
    VisibilityAnswer.objects.create(
        run=run,
        kind="brand",
        question=f"What do you know about {BRAND}?",
        engine="anthropic",
        model="claude-opus-5-5",
        answer="A scheduler.",
        answered=True,
        mentioned=True,
    )
    return run


class TestPage:
    def test_report_is_honest_about_what_it_is(self, client, owner, workspace, report):
        html = client.get(url("index", workspace)).content.decode()
        assert "not search" in html and "rankings" in html
        assert "claude-opus-5-5" in html and report.created_at.strftime("%b") in html
        assert "50%" in html and "Not named" in html and "A post for the Dubai gap" in html
        assert "Who else gets named" in html and "Buffer" in html

    def test_setup_state_without_a_profile(self, client, owner, workspace):
        html = client.get(url("index", workspace)).content.decode()
        assert "Set up your report" in html and "AI visibility</a>" in html

    def test_free_plan_sees_the_upgrade_state(self, client, owner, workspace, profile):
        set_plan(workspace.organization, "free")
        html = client.get(url("index", workspace)).content.decode()
        assert "isn't in your plan" in html and url("save", workspace) not in html

    def test_warns_when_ai_is_not_configured(self, client, owner, workspace, profile, settings):
        settings.ANTHROPIC_API_KEY = ""
        assert "Connect your AI" in client.get(url("index", workspace)).content.decode()

    def test_members_without_view_analytics_and_strangers_are_refused(self, client, workspace, profile):
        other = Workspace.objects.create(organization=Organization.objects.create(name="O"), name="Other")
        client.force_login(make_member(other, "stranger@example.com"))
        assert client.get(url("index", workspace)).status_code == 403
        assert client.post(url("save", workspace), {"brand_name": "x"}).status_code == 403

    def test_anonymous_is_redirected(self, client, workspace):
        assert client.get(url("index", workspace)).status_code == 302


class TestActions:
    def test_save_profile(self, client, owner, workspace):
        r = client.post(
            url("save", workspace),
            {
                "brand_name": "Acme",
                "website": "acme.com",
                "aliases": "Acme Co, Acme Studio",
                "prompts": "best tool\nsecond prompt\n",
                "is_active": "on",
            },
            follow=True,
        )
        profile = VisibilityProfile.objects.get(workspace=workspace)
        assert profile.aliases == ["Acme Co", "Acme Studio"] and profile.prompts.count() == 2 and profile.is_active
        assert "Saved" in r.content.decode()

    def test_too_many_prompts_is_explained(self, client, owner, workspace):
        r = client.post(
            url("save", workspace), {"brand_name": "Acme", "prompts": "\n".join(f"q{i}" for i in range(6))}, follow=True
        )
        assert "5 category prompts" in r.content.decode() and not VisibilityProfile.objects.exists()

    def test_run_now_queues_the_report(self, client, owner, workspace, profile):
        client.post(url("run", workspace))
        assert VisibilityRun.objects.get().status in ("queued", "complete", "running", "failed", "partial")

    def test_run_now_needs_the_settings_permission(self, client, workspace, profile):
        client.force_login(make_member(workspace, "editor@example.com", ROLE.EDITOR))
        assert client.post(url("run", workspace)).status_code == 403
        assert not VisibilityRun.objects.exists()

    def test_run_now_shows_why_it_cannot_run(self, client, owner, workspace, profile, settings):
        settings.ANTHROPIC_API_KEY = ""
        r = client.post(url("run", workspace), follow=True)
        assert "Connect your AI" in r.content.decode() and not VisibilityRun.objects.exists()

    def test_save_idea_adds_it_to_the_ideas_board_once(self, client, owner, workspace, report):
        target = url("save_idea", workspace, run_id=report.id)
        client.post(target, {"index": "0"})
        assert Idea.objects.filter(workspace=workspace, description="A post for the Dubai gap").count() == 1
        r = client.post(target, {"index": "0"}, follow=True)
        assert Idea.objects.count() == 1 and "already saved" in r.content.decode()
        assert "Saved to Ideas" in client.get(url("index", workspace)).content.decode()

    def test_save_idea_needs_create_posts(self, client, workspace, report):
        client.force_login(make_member(workspace, "viewer@example.com", ROLE.VIEWER))
        assert client.post(url("save_idea", workspace, run_id=report.id), {"index": "0"}).status_code == 403
        assert not Idea.objects.exists()

    def test_save_idea_cannot_reach_another_workspaces_report(self, client, owner, workspace, report):
        other = Workspace.objects.create(organization=workspace.organization, name="Other")
        WorkspaceMembership.objects.create(user=owner, workspace=other, workspace_role=ROLE.OWNER)
        assert client.post(url("save_idea", other, run_id=report.id), {"index": "0"}).status_code == 404

    def test_bad_index(self, client, owner, workspace, report):
        assert client.post(url("save_idea", workspace, run_id=report.id), {"index": "x"}).status_code == 404


class SecureClient(Client):
    def generic(self, method, path, *args, **kwargs):
        kwargs["secure"] = True
        return super().generic(method, path, *args, **kwargs)


def api_client(workspace, permissions):
    owner = make_member(workspace, f"api{len(permissions)}@example.com")
    account = make_account(workspace, "bluesky", f"key-{len(permissions)}")
    key = key_services.issue_api_key(
        workspace=workspace, social_accounts=[account], issued_by=owner, name="k", permissions=permissions
    )
    return SecureClient(HTTP_AUTHORIZATION=f"Bearer {key.plaintext_token}")


def rpc(client, name):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": {}}}
    return client.post("/api/v1/mcp/", data=json.dumps(body), content_type="application/json").json()


class TestApiMcp:
    def test_rest_latest_report(self, workspace, report):
        data = api_client(workspace, ["view_analytics"]).get("/api/v1/ai-visibility/").json()
        assert data["mention_rate"] == 50.0 and data["brand_name"] == BRAND
        assert "not search rankings" in data["note"]
        assert data["gaps"] == ["schedule Instagram posts"] and data["others_named"] == ["Buffer", "Later"]
        assert data["engines"] == {"anthropic": "claude-opus-5-5"}
        assert data["suggested_ideas"] == [{"text": "A post for the Dubai gap", "saved": False}]
        assert {a["kind"] for a in data["answers"]} == {"category", "brand"}

    def test_mcp_matches_rest(self, workspace, report):
        client = api_client(workspace, list(PERMISSION_KEYS))
        names = {
            t["name"]
            for t in client.post(
                "/api/v1/mcp/",
                data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}),
                content_type="application/json",
            ).json()["result"]["tools"]
        }
        assert "get_ai_visibility_report" in names
        via_mcp = json.loads(rpc(client, "get_ai_visibility_report")["result"]["content"][0]["text"])
        assert via_mcp == json.loads(client.get("/api/v1/ai-visibility/").content)

    def test_404_before_the_first_report(self, workspace, profile):
        client = api_client(workspace, ["view_analytics"])
        assert client.get("/api/v1/ai-visibility/").status_code == 404
        assert "error" in rpc(client, "get_ai_visibility_report")

    def test_only_this_workspaces_report(self, workspace, report):
        other_ws = Workspace.objects.create(organization=workspace.organization, name="Other")
        assert api_client(other_ws, ["view_analytics"]).get("/api/v1/ai-visibility/").status_code == 404

    def test_needs_view_analytics(self, workspace, report):
        assert api_client(workspace, ["create_posts"]).get("/api/v1/ai-visibility/").status_code == 403

    def test_plan_gate(self, workspace, report):
        set_plan(workspace.organization, "free")
        r = api_client(workspace, ["view_analytics"]).get("/api/v1/ai-visibility/")
        assert r.status_code == 403 and "AI visibility" in r.json()["detail"]

    def test_requires_auth(self):
        assert Client().get("/api/v1/ai-visibility/", secure=True).status_code == 401

    def test_unfinished_runs_are_not_served(self, workspace, profile):
        VisibilityRun.objects.create(profile=profile, status="running", brand_name=BRAND)
        assert api_client(workspace, ["view_analytics"]).get("/api/v1/ai-visibility/").status_code == 404
        assert services.latest_run(profile) is None
