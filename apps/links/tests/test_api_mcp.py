"""Agent REST API and MCP: make short links, list them with clicks, read stats."""

from __future__ import annotations

import json

import pytest
from django.test import Client

from apps.api_keys import services as key_services
from apps.links import services
from apps.members.models import PERMISSION_KEYS

from .conftest import BROWSER, configure

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
        workspace=workspace, social_accounts=list(accounts), issued_by=owner, name="links", permissions=permissions
    )
    return SecureClient(HTTP_AUTHORIZATION=f"Bearer {key.plaintext_token}")


@pytest.fixture
def api(workspace, owner, linkedin):
    return make_client(workspace, owner, [linkedin], list(PERMISSION_KEYS))


@pytest.fixture
def read_only_api(workspace, owner, linkedin):
    return make_client(workspace, owner, [linkedin], ["view_analytics"])


def rpc(client, name, arguments=None):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments or {}}}
    return client.post("/api/v1/mcp/", data=json.dumps(body), content_type="application/json").json()


def text(body):
    return json.loads(body["result"]["content"][0]["text"])


def click(link):
    Client().get(link.short_url.replace("http://testserver", "").replace("https://example.test", ""), **BROWSER)


class TestRest:
    def test_create_and_follow(self, api, workspace, linkedin):
        response = api.post(
            "/api/v1/links/",
            data={"url": "https://example.com/offer", "social_account_id": str(linkedin.id)},
            content_type="application/json",
        )
        assert response.status_code == 201
        body = response.json()
        assert body["destination_url"] == "https://example.com/offer" and body["provider"] == "builtin"
        assert body["social_account_id"] == str(linkedin.id) and body["is_active"] is True
        code = body["short_url"].rsplit("/", 1)[1]
        assert Client().get(f"/s/{code}", **BROWSER)["Location"] == "https://example.com/offer"

    def test_bad_addresses_are_422(self, api):
        for bad in ("javascript:alert(1)", "ftp://example.com", "not a url"):
            response = api.post("/api/v1/links/", data={"url": bad}, content_type="application/json")
            assert response.status_code == 422, bad

    def test_an_account_off_the_allowlist_is_refused(self, api, x_account):
        response = api.post(
            "/api/v1/links/",
            data={"url": "https://example.com/", "social_account_id": str(x_account.id)},
            content_type="application/json",
        )
        assert response.status_code == 403

    def test_idempotency_key_replays(self, api):
        first = api.post(
            "/api/v1/links/",
            data={"url": "https://example.com/a"},
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="k1",
        )
        again = api.post(
            "/api/v1/links/",
            data={"url": "https://example.com/a"},
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="k1",
        )
        assert first.json()["id"] == again.json()["id"]

    def test_list_and_stats(self, api, workspace, linkedin):
        link = services.create_short_link(workspace, "https://example.com/a", social_account=linkedin)
        click(link)
        click(link)
        listed = api.get("/api/v1/links/").json()
        assert listed["links"][0]["id"] == str(link.id) and listed["links"][0]["clicks"] == 2
        stats = api.get("/api/v1/links/stats?days=7").json()
        assert stats["clicks"] == 2 and stats["days"] == 7 and len(stats["series"]) == 7
        assert stats["accounts"][0]["social_account_id"] == str(linkedin.id) and stats["accounts"][0]["clicks"] == 2
        assert stats["links"][0]["short_url"] == link.short_url
        assert api.get(f"/api/v1/links/stats?link_id={link.id}").json()["clicks"] == 2

    def test_links_of_hidden_accounts_are_invisible(self, api, workspace, x_account):
        hidden = services.create_short_link(workspace, "https://example.com/hidden", social_account=x_account)
        assert api.get("/api/v1/links/").json()["links"] == []
        assert api.get(f"/api/v1/links/stats?link_id={hidden.id}").status_code == 404

    def test_permissions(self, read_only_api, api, workspace):
        assert read_only_api.get("/api/v1/links/").status_code == 200
        assert (
            read_only_api.post(
                "/api/v1/links/", data={"url": "https://example.com/"}, content_type="application/json"
            ).status_code
            == 403
        )
        assert api.get("/api/v1/links/stats?days=0").status_code == 422

    def test_ai_routes_accept_a_set_id(self, workspace, owner, linkedin, x_account, monkeypatch):
        from apps.account_sets import services as set_services

        made = set_services.create_set(workspace, name="Both", account_ids=[linkedin.id, x_account.id])
        client = make_client(workspace, owner, [linkedin, x_account], list(PERMISSION_KEYS))
        seen = {}

        def fake_adapt(context, caption, accounts):
            seen["ids"] = {a.id for a in accounts}
            return {str(a.id): caption for a in accounts}

        monkeypatch.setattr("apps.api.routers.ai.adapt_for_networks", fake_adapt)
        response = client.post(
            "/api/v1/ai/adapt", data={"caption": "Hello", "set_id": str(made.id)}, content_type="application/json"
        )
        assert response.status_code == 200 and seen["ids"] == {linkedin.id, x_account.id}
        empty = client.post("/api/v1/ai/adapt", data={"caption": "Hello"}, content_type="application/json")
        assert empty.status_code == 422

    def test_create_post_takes_shorten_links(self, workspace, owner, linkedin, autonomous_agents):
        configure(workspace, shorten_mode="ask")
        client = make_client(workspace, owner, [linkedin], list(PERMISSION_KEYS))
        body = {
            "social_account_id": str(linkedin.id),
            "caption": "Read https://example.com/a-long-address-here",
            "action": "schedule",
            "scheduled_at": "2030-01-01T10:00:00Z",
            "shorten_links": True,
        }
        response = client.post("/api/v1/posts/", data=body, content_type="application/json")
        assert response.status_code == 201, response.content
        from apps.composer.models import PlatformPost

        assert "/s/" in PlatformPost.objects.get().effective_caption
        body2 = {**body, "caption": "Read https://example.com/another", "shorten_links": None}
        client.post("/api/v1/posts/", data=body2, content_type="application/json")
        assert (
            PlatformPost.objects.filter(platform_specific_caption__isnull=True).count() == 1
        )  # "ask" with no choice: untouched


class TestMcp:
    def test_tools_are_listed_by_capability(self, api, read_only_api):
        names = {
            t["name"]
            for t in api.post(
                "/api/v1/mcp/",
                data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
                content_type="application/json",
            ).json()["result"]["tools"]
        }
        assert {
            "create_short_link",
            "list_short_links",
            "get_link_stats",
            "list_account_sets",
            "create_set_draft",
        } <= names

    def test_create_list_and_stats(self, api, workspace, linkedin):
        made = text(
            rpc(api, "create_short_link", {"url": "https://example.com/mcp", "social_account_id": str(linkedin.id)})
        )
        assert made["destination_url"] == "https://example.com/mcp"
        code = made["short_url"].rsplit("/", 1)[1]
        Client().get(f"/s/{code}", **BROWSER)
        listed = text(rpc(api, "list_short_links"))
        assert listed["links"][0]["clicks"] == 1
        stats = text(rpc(api, "get_link_stats", {"days": 3}))
        assert stats["clicks"] == 1 and len(stats["series"]) == 3

    def test_refusals_come_back_as_errors(self, api, read_only_api):
        assert "error" in rpc(api, "create_short_link", {"url": "javascript:alert(1)"})
        assert "error" in rpc(read_only_api, "create_short_link", {"url": "https://example.com/"})
        assert "error" in rpc(api, "get_link_stats", {"days": 500})
