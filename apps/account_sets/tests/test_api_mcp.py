"""Agent REST API and MCP for account sets, ``set_id`` expansion, and the capability registry."""

from __future__ import annotations

import json

import pytest
from django.test import Client

from apps.account_sets import services
from apps.account_sets.models import AccountSet
from apps.api_keys import capabilities
from apps.api_keys import services as key_services
from apps.composer.models import PlatformPost
from apps.members.models import PERMISSION_KEYS

pytestmark = pytest.mark.django_db


class SecureClient(Client):
    def generic(self, method, path, *args, **kwargs):
        kwargs["secure"] = True
        return super().generic(method, path, *args, **kwargs)


@pytest.fixture(autouse=True)
def _clear_throttle():
    from django.core.cache import cache

    cache.delete("agent_api:auth_fail:127.0.0.1")


def make_client(workspace, owner, accounts, permissions=None, capabilities_=None):
    key = key_services.issue_api_key(
        workspace=workspace,
        social_accounts=list(accounts),
        issued_by=owner,
        name="sets",
        permissions=permissions or list(PERMISSION_KEYS),
        capabilities=capabilities_,
    )
    return SecureClient(HTTP_AUTHORIZATION=f"Bearer {key.plaintext_token}")


def rpc(client, name, arguments=None):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments or {}}}
    return client.post("/api/v1/mcp/", data=json.dumps(body), content_type="application/json").json()


def text(body):
    return json.loads(body["result"]["content"][0]["text"])


@pytest.fixture
def api(workspace, owner, linkedin, pinterest, tiktok):
    return make_client(workspace, owner, [linkedin, pinterest, tiktok])


class TestRest:
    def test_crud(self, api, linkedin, pinterest):
        created = api.post(
            "/api/v1/sets/",
            data={
                "name": "Client A",
                "social_account_ids": [str(linkedin.id), str(pinterest.id)],
                "defaults": {str(pinterest.id): {"board_id": "42"}},
            },
            content_type="application/json",
        )
        assert created.status_code == 201
        body = created.json()
        assert set(body["social_account_ids"]) == {str(linkedin.id), str(pinterest.id)}
        assert body["defaults"] == {str(pinterest.id): {"board_id": "42"}}
        listed = api.get("/api/v1/sets/").json()
        assert [s["name"] for s in listed["sets"]] == ["Client A"]
        assert api.get(f"/api/v1/sets/{body['id']}").json()["name"] == "Client A"
        updated = api.patch(
            f"/api/v1/sets/{body['id']}",
            data={"name": "Renamed", "social_account_ids": [str(linkedin.id)]},
            content_type="application/json",
        )
        assert updated.json()["name"] == "Renamed" and updated.json()["defaults"] == {}
        assert api.delete(f"/api/v1/sets/{body['id']}").json()["deleted"] is True
        assert not AccountSet.objects.exists()

    def test_validation_is_422(self, api, linkedin):
        bad = api.post(
            "/api/v1/sets/",
            data={
                "name": "X",
                "social_account_ids": [str(linkedin.id)],
                "defaults": {str(linkedin.id): {"board_id": "1"}},
            },
            content_type="application/json",
        )
        assert bad.status_code == 422 and "can't default" in bad.json()["detail"]

    def test_the_allowlist_limits_what_a_key_sees_and_changes(self, workspace, owner, linkedin, pinterest, api):
        both = services.create_set(
            workspace,
            name="Both",
            account_ids=[linkedin.id, pinterest.id],
            defaults={str(pinterest.id): {"board_id": "1"}},
        )
        narrow = make_client(workspace, owner, [linkedin])
        seen = narrow.get(f"/api/v1/sets/{both.id}").json()
        assert seen["social_account_ids"] == [str(linkedin.id)] and seen["defaults"] == {}
        assert narrow.delete(f"/api/v1/sets/{both.id}").status_code == 403
        narrow.patch(f"/api/v1/sets/{both.id}", data={"name": "Both!"}, content_type="application/json")
        both.refresh_from_db()
        assert both.name == "Both!" and set(both.accounts.all()) == {linkedin, pinterest}
        out = narrow.post(
            "/api/v1/sets/",
            data={"name": "Mine", "social_account_ids": [str(pinterest.id)]},
            content_type="application/json",
        )
        assert out.status_code == 403

    def test_permissions(self, workspace, owner, linkedin):
        reader = make_client(workspace, owner, [linkedin], ["create_posts"])
        assert reader.get("/api/v1/sets/").status_code == 200
        assert (
            reader.post(
                "/api/v1/sets/",
                data={"name": "N", "social_account_ids": [str(linkedin.id)]},
                content_type="application/json",
            ).status_code
            == 403
        )
        nobody = make_client(workspace, owner, [linkedin], ["view_analytics"])
        assert nobody.get("/api/v1/sets/").status_code == 403

    def test_foreign_sets_are_404(self, api, other_workspace, stranger):
        foreign = services.create_set(other_workspace, name="Theirs", account_ids=[stranger.id])
        assert api.get(f"/api/v1/sets/{foreign.id}").status_code == 404

    def test_one_draft_across_the_whole_set_with_each_accounts_settings(
        self, api, workspace, linkedin, pinterest, tiktok
    ):
        made = services.create_set(
            workspace,
            name="Launch",
            account_ids=[linkedin.id, pinterest.id, tiktok.id],
            defaults={str(pinterest.id): {"board_id": "b-1"}, str(tiktok.id): {"privacy_level": "SELF_ONLY"}},
        )
        response = api.post(
            f"/api/v1/sets/{made.id}/drafts",
            data={"caption": "Hello set", "tags": ["launch"]},
            content_type="application/json",
        )
        assert response.status_code == 201, response.content
        children = {pp.social_account.platform: pp for pp in PlatformPost.objects.select_related("social_account")}
        assert set(children) == {"linkedin", "pinterest", "tiktok"} and {c.status for c in children.values()} == {
            "draft"
        }
        assert children["pinterest"].platform_extra["board_id"] == "b-1"
        assert children["tiktok"].platform_extra["privacy_level"] == "SELF_ONLY"
        assert len({c.post_id for c in children.values()}) == 1
        assert len(response.json()["platform_posts"]) == 3 if "platform_posts" in response.json() else True

    def test_a_set_with_no_usable_account_is_refused(self, api, workspace, linkedin):
        made = services.create_set(workspace, name="Dead", account_ids=[linkedin.id])
        linkedin.connection_status = "disconnected"
        linkedin.save(update_fields=["connection_status"])
        assert (
            api.post(
                f"/api/v1/sets/{made.id}/drafts", data={"caption": "x"}, content_type="application/json"
            ).status_code
            == 422
        )

    def test_repurpose_outputs_accept_set_id(self, api, workspace, linkedin, tiktok, monkeypatch):
        from apps.repurpose import outputs

        made = services.create_set(workspace, name="Both", account_ids=[linkedin.id, tiktok.id])
        captured = {}

        def fake_parse(workspace_, raw, **kwargs):
            captured["raw"] = raw
            raise ValueError("stop here")

        monkeypatch.setattr(outputs, "parse_requests", fake_parse)
        response = api.post(
            "/api/v1/repurpose/jobs",
            data={"source_type": "text", "text": "hello world", "outputs": [{"kind": "post", "set_id": str(made.id)}]},
            content_type="application/json",
        )
        assert response.status_code == 422
        assert set(captured["raw"][0]["account_ids"]) == {linkedin.id, tiktok.id}


class TestMcp:
    def test_round_trip(self, api, linkedin, pinterest):
        made = text(
            rpc(
                api,
                "create_account_set",
                {
                    "name": "MCP set",
                    "social_account_ids": [str(linkedin.id), str(pinterest.id)],
                    "defaults": {str(pinterest.id): {"board_id": "9"}},
                },
            )
        )
        assert made["name"] == "MCP set"
        assert text(rpc(api, "list_account_sets"))["sets"][0]["id"] == made["id"]
        assert text(rpc(api, "get_account_set", {"set_id": made["id"]}))["defaults"] == {
            str(pinterest.id): {"board_id": "9"}
        }
        renamed = text(rpc(api, "update_account_set", {"set_id": made["id"], "name": "Renamed"}))
        assert renamed["name"] == "Renamed" and renamed["defaults"] == {str(pinterest.id): {"board_id": "9"}}
        draft = text(rpc(api, "create_set_draft", {"set_id": made["id"], "caption": "Hi"}))
        assert draft["id"] and PlatformPost.objects.count() == 2
        assert text(rpc(api, "delete_account_set", {"set_id": made["id"]}))["deleted"] is True

    def test_set_id_on_adapt_caption(self, api, workspace, linkedin, tiktok, monkeypatch):
        made = services.create_set(workspace, name="Both", account_ids=[linkedin.id, tiktok.id])
        seen = {}

        def fake_adapt(context, caption, accounts):
            seen["ids"] = {a.id for a in accounts}
            return {str(a.id): caption for a in accounts}

        monkeypatch.setattr("apps.ai_writer.services.adapt_for_networks", fake_adapt)
        out = text(rpc(api, "adapt_caption", {"caption": "Hello", "set_id": str(made.id)}))
        assert len(out["captions"]) == 2 and seen["ids"] == {linkedin.id, tiktok.id}
        assert "error" in rpc(api, "adapt_caption", {"caption": "Hello"})

    def test_errors(self, api, linkedin):
        assert "error" in rpc(api, "get_account_set", {"set_id": "00000000-0000-4000-8000-000000000001"})
        assert "error" in rpc(
            api,
            "create_account_set",
            {"name": "X", "social_account_ids": [str(linkedin.id)], "defaults": {str(linkedin.id): {"nope": 1}}},
        )


class TestCapabilityRegistry:
    ROUTES = {
        "GET /sets/": "read_calendar",
        "GET /sets/{set_id}": "read_calendar",
        "POST /sets/": "write_settings",
        "PATCH /sets/{set_id}": "write_settings",
        "DELETE /sets/{set_id}": "write_settings",
        "POST /sets/{set_id}/drafts": "draft_posts",
        "POST /links/": "draft_posts",
        "GET /links/": "read_analytics",
        "GET /links/stats": "read_analytics",
    }
    TOOLS = {
        "list_account_sets": "read_calendar",
        "get_account_set": "read_calendar",
        "create_account_set": "write_settings",
        "update_account_set": "write_settings",
        "delete_account_set": "write_settings",
        "create_set_draft": "draft_posts",
        "create_short_link": "draft_posts",
        "list_short_links": "read_analytics",
        "get_link_stats": "read_analytics",
    }

    def test_every_new_route_and_tool_is_mapped(self):
        for route, capability in self.ROUTES.items():
            method, path = route.split(" ", 1)
            assert (
                capabilities.capability_for_request(method, "/api/v1" + path.replace("{set_id}", "x")) == capability
            ), route
        for tool, capability in self.TOOLS.items():
            assert capabilities.tool_capability(tool) == capability, tool

    def test_a_token_limited_to_reading_cannot_write_sets_or_make_links(self, workspace, owner, linkedin):
        client = make_client(workspace, owner, [linkedin], capabilities_=["read_calendar", "read_analytics"])
        assert client.get("/api/v1/sets/").status_code == 200
        assert client.get("/api/v1/links/").status_code == 200
        assert (
            client.post(
                "/api/v1/sets/",
                data={"name": "N", "social_account_ids": [str(linkedin.id)]},
                content_type="application/json",
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/v1/links/", data={"url": "https://example.com/"}, content_type="application/json"
            ).status_code
            == 403
        )
        assert "error" in rpc(client, "create_short_link", {"url": "https://example.com/"})
