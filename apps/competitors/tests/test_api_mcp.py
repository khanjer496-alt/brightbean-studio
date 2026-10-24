"""Agent REST API and MCP: list, add, remove competitors and read the comparison."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from apps.analytics.tests.helpers import make_account, make_member
from apps.api_keys import services as key_services
from apps.billing.tests.support import set_plan
from apps.competitors.models import Competitor, CompetitorSnapshot
from apps.members.models import PERMISSION_KEYS
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db


class SecureClient(Client):
    def generic(self, method, path, *args, **kwargs):
        kwargs["secure"] = True
        return super().generic(method, path, *args, **kwargs)


def make_client(workspace, accounts, permissions):
    owner = make_member(workspace, f"o{len(permissions)}{len(accounts)}@example.com")
    key = key_services.issue_api_key(
        workspace=workspace, social_accounts=list(accounts), issued_by=owner, name="k", permissions=permissions
    )
    return SecureClient(HTTP_AUTHORIZATION=f"Bearer {key.plaintext_token}")


@pytest.fixture
def api(workspace, bsky):
    return make_client(workspace, [bsky], list(PERMISSION_KEYS))


@pytest.fixture
def read_only_api(workspace, bsky):
    return make_client(workspace, [bsky], ["view_analytics"])


@pytest.fixture
def tracked(workspace):
    comp = Competitor.objects.create(
        workspace=workspace, platform="bluesky", handle="rival.bsky.social", display_name="Rival"
    )
    today = timezone.now().date()
    CompetitorSnapshot.objects.create(competitor=comp, date=today - timedelta(days=10), followers=100)
    CompetitorSnapshot.objects.create(competitor=comp, date=today, followers=120, posts_per_week=2.5, avg_engagement=9)
    return comp


def rpc(client, name, arguments=None):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments or {}}}
    return client.post("/api/v1/mcp/", data=json.dumps(body), content_type="application/json").json()


def payload(body):
    return json.loads(body["result"]["content"][0]["text"])


class TestRest:
    def test_list_shows_competitors_and_networks(self, api, tracked, workspace):
        make_account(workspace, "linkedin_personal", "Me")
        data = api.get("/api/v1/competitors/").json()
        [item] = data["competitors"]
        assert (item["handle"], item["followers"], item["posts_per_week"]) == ("rival.bsky.social", 120, 2.5)
        nets = {n["platform"]: n for n in data["networks"]}
        assert nets["bluesky"]["supported"] and nets["bluesky"]["limit"] == 10 and nets["bluesky"]["tracked"] == 1
        assert not nets["linkedin_personal"]["supported"] and nets["linkedin_personal"]["reason"]

    def test_comparison(self, api, tracked):
        data = api.get("/api/v1/competitors/comparison?days=30").json()
        [net] = data["networks"]
        assert net["platform"] == "bluesky"
        assert net["competitors"][0]["growth_pct"] == 20.0
        assert net["you"][0]["label"] == "Acme Bluesky"

    def test_comparison_hides_accounts_outside_the_allowlist(self, workspace, bsky, tracked):
        other = make_account(workspace, "bluesky", "Hidden Bluesky")
        client = make_client(workspace, [bsky], list(PERMISSION_KEYS))
        labels = [r["label"] for r in client.get("/api/v1/competitors/comparison").json()["networks"][0]["you"]]
        assert "Hidden Bluesky" not in labels and other.account_name not in labels

    def test_comparison_needs_both_ends_of_a_custom_range(self, api):
        assert api.get("/api/v1/competitors/comparison?start=2026-01-01").status_code == 422

    def test_add_and_remove(self, api, read):
        r = api.post(
            "/api/v1/competitors/",
            data=json.dumps({"platform": "bluesky", "handle": "@Rival.bsky.social"}),
            content_type="application/json",
        )
        assert r.status_code == 201 and r.json()["handle"] == "rival.bsky.social"
        gone = api.delete(f"/api/v1/competitors/{r.json()['id']}")
        assert gone.status_code == 200 and not Competitor.objects.exists()

    def test_add_over_the_plan_limit_is_403(self, api, workspace, read):
        set_plan(workspace.organization, "creator")
        for i in range(3):
            assert (
                api.post(
                    "/api/v1/competitors/",
                    data=json.dumps({"platform": "bluesky", "handle": f"r{i}.bsky.social"}),
                    content_type="application/json",
                ).status_code
                == 201
            )
        r = api.post(
            "/api/v1/competitors/",
            data=json.dumps({"platform": "bluesky", "handle": "r9.bsky.social"}),
            content_type="application/json",
        )
        assert r.status_code == 403 and "3 competitors per network" in r.json()["detail"]

    def test_add_unsupported_network(self, api, read):
        r = api.post(
            "/api/v1/competitors/",
            data=json.dumps({"platform": "x", "handle": "someone"}),
            content_type="application/json",
        )
        assert r.status_code == 422

    def test_write_needs_manage_permission(self, read_only_api, tracked, read):
        assert read_only_api.get("/api/v1/competitors/").status_code == 200
        assert (
            read_only_api.post(
                "/api/v1/competitors/",
                data=json.dumps({"platform": "bluesky", "handle": "a.bsky.social"}),
                content_type="application/json",
            ).status_code
            == 403
        )
        assert read_only_api.delete(f"/api/v1/competitors/{tracked.id}").status_code == 403

    def test_other_workspaces_competitor_is_404(self, api, workspace):
        other = Workspace.objects.create(organization=workspace.organization, name="Other")
        comp = Competitor.objects.create(workspace=other, platform="bluesky", handle="x.bsky.social")
        assert api.delete(f"/api/v1/competitors/{comp.id}").status_code == 404
        assert api.get("/api/v1/competitors/").json()["competitors"] == []

    def test_requires_auth(self):
        assert Client().get("/api/v1/competitors/", secure=True).status_code == 401


class TestMcp:
    def test_tools_are_listed(self, api):
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        names = {
            t["name"]
            for t in api.post("/api/v1/mcp/", data=json.dumps(body), content_type="application/json").json()["result"][
                "tools"
            ]
        }
        assert {"list_competitors", "get_competitor_comparison", "add_competitor", "remove_competitor"} <= names

    def test_list_and_compare(self, api, tracked):
        assert payload(rpc(api, "list_competitors"))["competitors"][0]["handle"] == "rival.bsky.social"
        assert (
            payload(rpc(api, "get_competitor_comparison", {"days": 30}))["networks"][0]["competitors"][0]["growth_pct"]
            == 20.0
        )

    def test_add_then_remove(self, api, read):
        added = payload(rpc(api, "add_competitor", {"platform": "bluesky", "handle": "rival.bsky.social"}))
        assert added["handle"] == "rival.bsky.social"
        assert payload(rpc(api, "remove_competitor", {"competitor_id": added["id"]}))["deleted"] is True

    def test_validation_and_refusals(self, api, read_only_api, tracked):
        assert "error" in rpc(api, "add_competitor", {"platform": "bluesky"})
        assert "error" in rpc(api, "get_competitor_comparison", {"days": 0})
        assert "error" in rpc(read_only_api, "remove_competitor", {"competitor_id": str(tracked.id)})
