"""Agent REST API and MCP: list/get/update pages and links, read stats."""

from __future__ import annotations

import json

import pytest
from django.test import Client, RequestFactory

from apps.api_keys import services as key_services
from apps.linkinbio import services
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


def make_client(workspace, owner, accounts, permissions):
    key = key_services.issue_api_key(
        workspace=workspace, social_accounts=list(accounts), issued_by=owner, name="bio", permissions=permissions
    )
    return SecureClient(HTTP_AUTHORIZATION=f"Bearer {key.plaintext_token}")


@pytest.fixture
def api(workspace, owner, instagram, page):
    return make_client(workspace, owner, [instagram], list(PERMISSION_KEYS))


@pytest.fixture
def read_only_api(workspace, owner, instagram, page):
    return make_client(workspace, owner, [instagram], ["view_analytics"])


def rpc(client, name, arguments=None):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments or {}}}
    return client.post("/api/v1/mcp/", data=json.dumps(body), content_type="application/json").json()


def text(body):
    return json.loads(body["result"]["content"][0]["text"])


class TestRest:
    def test_list_and_get(self, api, page):
        services.add_link(page, title="Shop", url="https://example.com/shop")
        listed = api.get("/api/v1/bio/pages").json()
        assert [p["slug"] for p in listed["pages"]] == ["acme"] and listed["next_cursor"] is None
        one = api.get(f"/api/v1/bio/pages/{page.id}").json()
        assert one["path"] == "/l/acme/" and one["links"][0]["title"] == "Shop" and one["links"][0]["visible_now"]

    def test_update_page(self, api, page, instagram):
        r = api.patch(
            f"/api/v1/bio/pages/{page.id}",
            data=json.dumps({"title": "Renamed", "theme": "sunrise", "social_account_ids": [str(instagram.id)]}),
            content_type="application/json",
        )
        assert (
            r.status_code == 200
            and r.json()["title"] == "Renamed"
            and r.json()["social_account_ids"] == [str(instagram.id)]
        )
        bad = api.patch(
            f"/api/v1/bio/pages/{page.id}", data=json.dumps({"slug": "admin"}), content_type="application/json"
        )
        assert bad.status_code == 422 and "reserved" in bad.json()["detail"]
        assert (
            api.patch(
                f"/api/v1/bio/pages/{page.id}", data=json.dumps({"theme": "neon"}), content_type="application/json"
            ).status_code
            == 422
        )

    def test_link_lifecycle(self, api, page):
        r = api.post(
            f"/api/v1/bio/pages/{page.id}/links",
            data=json.dumps({"title": "A", "url": "https://example.com/a"}),
            content_type="application/json",
        )
        assert r.status_code == 201
        api.post(
            f"/api/v1/bio/pages/{page.id}/links",
            data=json.dumps({"title": "B", "url": "https://example.com/b"}),
            content_type="application/json",
        )
        a, b = page.links.all()
        r = api.patch(
            f"/api/v1/bio/pages/{page.id}/links/{b.id}",
            data=json.dumps({"move": "up", "title": "Bee"}),
            content_type="application/json",
        )
        assert [x["title"] for x in r.json()["links"]] == ["Bee", "A"]
        assert api.delete(f"/api/v1/bio/pages/{page.id}/links/{a.id}").json() == {"deleted": True, "id": str(a.id)}
        assert page.links.count() == 1

    def test_rejects_bad_urls(self, api, page):
        for url in ("javascript:alert(1)", "data:text/html,x", "ftp://example.com"):
            r = api.post(
                f"/api/v1/bio/pages/{page.id}/links",
                data=json.dumps({"title": "A", "url": url}),
                content_type="application/json",
            )
            assert r.status_code == 422
        assert not page.links.exists()

    def test_stats(self, api, page):
        link = services.add_link(page, title="Shop", url="https://example.com/shop")
        request = RequestFactory().get("/", HTTP_USER_AGENT="Firefox/130")
        services.record_view(page, request)
        services.record_click(page, link.id, request)
        stats = api.get(f"/api/v1/bio/pages/{page.id}/stats?days=7").json()
        assert (stats["views"], stats["clicks"], stats["days"]) == (1, 1, 7)
        assert stats["links"][0]["title"] == "Shop" and len(stats["series"]) == 7
        assert api.get(f"/api/v1/bio/pages/{page.id}/stats?days=500").status_code == 422

    def test_read_only_key_cannot_edit(self, read_only_api, page):
        assert read_only_api.get(f"/api/v1/bio/pages/{page.id}").status_code == 200
        assert (
            read_only_api.patch(
                f"/api/v1/bio/pages/{page.id}", data=json.dumps({"title": "x"}), content_type="application/json"
            ).status_code
            == 403
        )
        assert (
            read_only_api.post(
                f"/api/v1/bio/pages/{page.id}/links",
                data=json.dumps({"title": "A", "url": "https://e.co"}),
                content_type="application/json",
            ).status_code
            == 403
        )

    def test_other_workspace_page_is_404(self, api, workspace):
        other = type(workspace).objects.create(organization=workspace.organization, name="Other")
        foreign = services.create_page(other, title="F", slug="foreign")
        assert api.get(f"/api/v1/bio/pages/{foreign.id}").status_code == 404
        assert api.get(f"/api/v1/bio/pages/{foreign.id}/stats").status_code == 404

    def test_allowlist_limits_icons(self, workspace, owner, page, instagram):
        from apps.analytics.tests.helpers import make_account

        x = make_account(workspace, "x", "Acme X")
        narrow = make_client(workspace, owner, [instagram], list(PERMISSION_KEYS))
        services.update_page(page, social_account_ids=[x.id])
        r = narrow.patch(
            f"/api/v1/bio/pages/{page.id}",
            data=json.dumps({"social_account_ids": [str(x.id)]}),
            content_type="application/json",
        )
        assert r.status_code == 403
        r = narrow.patch(
            f"/api/v1/bio/pages/{page.id}",
            data=json.dumps({"social_account_ids": [str(instagram.id)]}),
            content_type="application/json",
        )
        # The icon the key can't see stays in place.
        assert set(r.json()["social_account_ids"]) == {str(x.id), str(instagram.id)}

    def test_requires_auth(self, page):
        assert Client().get("/api/v1/bio/pages", secure=True).status_code == 401


class TestMcp:
    def test_tools_listed(self, api):
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        names = {
            t["name"]
            for t in api.post("/api/v1/mcp/", data=json.dumps(body), content_type="application/json").json()["result"][
                "tools"
            ]
        }
        assert {
            "list_bio_pages",
            "get_bio_page",
            "update_bio_page",
            "add_bio_link",
            "update_bio_link",
            "delete_bio_link",
            "get_bio_stats",
        } <= names

    def test_flow_matches_rest(self, api, page):
        assert text(rpc(api, "list_bio_pages")) == api.get("/api/v1/bio/pages").json()
        added = text(rpc(api, "add_bio_link", {"page_id": str(page.id), "title": "A", "url": "https://example.com/a"}))
        link_id = added["links"][0]["id"]
        moved = text(rpc(api, "update_bio_link", {"page_id": str(page.id), "link_id": link_id, "title": "Alpha"}))
        assert moved["links"][0]["title"] == "Alpha"
        assert (
            text(rpc(api, "get_bio_page", {"page_id": str(page.id)})) == api.get(f"/api/v1/bio/pages/{page.id}").json()
        )
        assert text(rpc(api, "get_bio_stats", {"page_id": str(page.id)}))["days"] == 30
        assert text(rpc(api, "delete_bio_link", {"page_id": str(page.id), "link_id": link_id}))["deleted"]

    def test_update_page(self, api, page):
        assert text(rpc(api, "update_bio_page", {"page_id": str(page.id), "title": "Via MCP"}))["title"] == "Via MCP"

    def test_bad_input_is_an_error(self, api, page):
        body = rpc(api, "add_bio_link", {"page_id": str(page.id), "title": "A", "url": "javascript:alert(1)"})
        assert "error" in body
        body = rpc(api, "update_bio_page", {"page_id": str(page.id), "theme": "neon"})
        assert "error" in body

    def test_read_only_key_cannot_edit(self, read_only_api, page):
        body = rpc(read_only_api, "update_bio_page", {"page_id": str(page.id), "title": "x"})
        assert "error" in body and "Permission denied" in body["error"]["message"]
        assert text(rpc(read_only_api, "get_bio_page", {"page_id": str(page.id)}))["slug"] == "acme"
