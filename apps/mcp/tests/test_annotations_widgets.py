"""Tool annotations derived from the capability registry, and the UI widgets served as MCP resources."""

from __future__ import annotations

import json

import pytest

from apps.api.tests.world import MCP_URL, make_post
from apps.api_keys import capabilities as caps
from apps.mcp.tools import all_tools, get_tool
from apps.mcp.widgets import WIDGET_MIME_TYPE, WIDGETS, widget_for_tool

pytestmark = pytest.mark.usefixtures("autonomous_agents")

HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")


def rpc(client, method, params=None):
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    return client.post(MCP_URL, data=json.dumps(body), content_type="application/json").json()


@pytest.mark.django_db
class TestAnnotationsFromCapabilities:
    def test_every_tool_has_a_title_and_all_four_boolean_hints(self):
        for tool in all_tools():
            wire = tool.to_mcp_dict()
            assert wire["title"] and wire["title"][0].isupper(), tool.name
            assert set(wire["annotations"]) == set(HINTS), tool.name
            assert all(isinstance(wire["annotations"][h], bool) for h in HINTS), tool.name

    def test_tools_in_low_risk_capabilities_are_read_only(self):
        for tool in all_tools():
            cap = caps.capability(caps.tool_capability(tool.name))
            if cap.risk == "low":
                assert caps.tool_annotations(tool.name)["readOnlyHint"], tool.name

    def test_tools_the_registry_flags_read_only_are_annotated_read_only(self):
        for tool in all_tools():
            if tool.read_only:
                assert caps.tool_annotations(tool.name)["readOnlyHint"], tool.name

    def test_a_read_only_tool_is_never_destructive(self):
        for tool in all_tools():
            hints = caps.tool_annotations(tool.name)
            if hints["readOnlyHint"]:
                assert not hints["destructiveHint"], tool.name

    def test_lookup_verbs_are_read_only_even_inside_a_mixed_capability(self):
        for name in ("list_approvals", "list_post_comments", "get_repurpose_job", "get_media", "search_media"):
            assert caps.tool_annotations(name)["readOnlyHint"], name
        for name in ("review_post", "add_post_comment", "create_repurpose_job", "upload_media", "run_report"):
            assert not caps.tool_annotations(name)["readOnlyHint"], name

    def test_removals_are_destructive_and_idempotent(self):
        for tool in all_tools():
            if tool.name.startswith(("delete_", "remove_", "cancel_", "discard_")):
                hints = caps.tool_annotations(tool.name)
                assert hints["destructiveHint"] and hints["idempotentHint"], tool.name

    def test_creating_something_new_is_additive(self):
        for name in ("create_draft", "schedule_post", "add_bio_link", "generate_caption"):
            hints = caps.tool_annotations(name)
            assert not hints["destructiveHint"] and not hints["idempotentHint"], name

    def test_replacing_or_sending_is_destructive(self):
        for name in ("update_post", "update_autonomy_policy", "send_reply", "review_post", "rotate_webhook_secret"):
            assert caps.tool_annotations(name)["destructiveHint"], name

    def test_open_world_tools_are_real_tools_that_reach_outside_the_workspace(self):
        registered = {t.name for t in all_tools()}
        assert registered >= caps.OPEN_WORLD_TOOLS
        for name in ("schedule_post", "review_post", "add_to_evergreen", "create_webhook_endpoint", "check_feed"):
            assert caps.tool_annotations(name)["openWorldHint"], name
        assert not caps.tool_annotations("create_draft")["openWorldHint"]
        assert not caps.tool_annotations("update_brand_voice")["openWorldHint"]

    def test_tools_list_carries_annotations_on_the_wire(self, world):
        tools = rpc(world.clients["full"], "tools/list")["result"]["tools"]
        by_name = {t["name"]: t for t in tools}
        assert by_name["list_posts"]["annotations"]["readOnlyHint"] is True
        assert by_name["delete_post"]["annotations"]["destructiveHint"] is True
        assert by_name["delete_post"]["title"] == "Delete post"


@pytest.mark.django_db
class TestWidgetResources:
    def test_initialize_advertises_resources(self, world):
        result = rpc(world.clients["full"], "initialize", {"protocolVersion": "2025-03-26"})["result"]
        assert "resources" in result["capabilities"]

    def test_resources_list_names_the_three_widgets(self, world):
        resources = rpc(world.clients["full"], "resources/list")["result"]["resources"]
        assert [r["name"] for r in resources] == ["Post preview", "Week calendar", "Approval list"]
        assert {r["mimeType"] for r in resources} == {WIDGET_MIME_TYPE}
        assert all(r["uri"].startswith("ui://widget/") for r in resources)

    def test_resources_read_returns_self_contained_html_with_meta(self, world):
        for widget in WIDGETS:
            content = rpc(world.clients["full"], "resources/read", {"uri": widget.uri})["result"]["contents"][0]
            assert content["uri"] == widget.uri
            assert content["mimeType"] == WIDGET_MIME_TYPE
            assert content["text"].lower().startswith("<!doctype html>")
            assert "/*__" not in content["text"]
            ui = content["_meta"]["ui"]
            assert ui["csp"] == {"connectDomains": [], "resourceDomains": []}
            assert ui["prefersBorder"] is True

    def test_resources_read_unknown_uri_is_resource_not_found(self, world):
        body = rpc(world.clients["full"], "resources/read", {"uri": "ui://widget/nope.html"})
        assert body["error"]["code"] == -32002

    def test_resources_read_requires_a_uri(self, world):
        assert rpc(world.clients["full"], "resources/read")["error"]["code"] == -32602

    def test_resources_require_authentication(self, world):
        r = world.anonymous.post(
            MCP_URL,
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "resources/list"}),
            content_type="application/json",
        )
        assert r.status_code == 401

    def test_widget_domain_is_included_only_when_configured(self, world, settings):
        widget = WIDGETS[0]
        assert "domain" not in widget.content()["_meta"]["ui"]
        settings.MCP_WIDGET_DOMAIN = "https://widgets.example.test"
        assert widget.content()["_meta"]["ui"]["domain"] == "https://widgets.example.test"

    def test_every_widget_tool_exists_and_points_at_its_template(self, world):
        tools = {t["name"]: t for t in rpc(world.clients["full"], "tools/list")["result"]["tools"]}
        for widget in WIDGETS:
            assert widget.path.exists(), widget.path
            for name in widget.tools:
                assert get_tool(name) is not None, name
                meta = tools[name]["_meta"]
                assert meta["ui"]["resourceUri"] == widget.uri == meta["openai/outputTemplate"]
        assert "_meta" not in tools["list_tags"]
        assert widget_for_tool("list_tags") is None


@pytest.mark.django_db
class TestStructuredContent:
    def test_object_results_repeat_as_structured_content(self, world):
        make_post(world.workspace, world.owner, [world.account1])
        envelope = rpc(world.clients["full"], "tools/call", {"name": "list_posts", "arguments": {}})["result"]
        assert envelope["structuredContent"] == json.loads(envelope["content"][0]["text"])
        assert "posts" in envelope["structuredContent"]
