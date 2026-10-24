"""The capability registry is the single source of truth: every route and tool must be in it."""

from __future__ import annotations

import re

import pytest

from apps.api_keys import capabilities as caps


def _rest_routes() -> set[str]:
    """Every "METHOD /path" the Agent API serves, read from the live router tree."""
    from apps.api.api import api

    routes = set()
    for prefix, router in api._routers:
        for path, view in router.path_operations.items():
            for operation in view.operations:
                for method in operation.methods:
                    routes.add(f"{method} {prefix}{path}")
    return routes


def _tool_names() -> set[str]:
    from apps.mcp.tools import all_tools

    return {t.name for t in all_tools()}


class TestRegistryCompleteness:
    def test_every_rest_route_is_mapped(self):
        unmapped = _rest_routes() - set(caps.ROUTE_CAPABILITY) - caps.MCP_ROUTES
        assert not unmapped, f"Add these routes to apps/api_keys/capabilities.py: {sorted(unmapped)}"

    def test_every_mcp_tool_is_mapped(self):
        unmapped = _tool_names() - set(caps.TOOL_CAPABILITY)
        assert not unmapped, f"Add these tools to apps/api_keys/capabilities.py: {sorted(unmapped)}"

    def test_registry_names_no_route_or_tool_that_does_not_exist(self):
        assert set(caps.ROUTE_CAPABILITY) <= _rest_routes()
        assert set(caps.TOOL_CAPABILITY) <= _tool_names()

    def test_every_capability_covers_something(self):
        for cap in caps.CAPABILITIES:
            assert cap.key in set(caps.ROUTE_CAPABILITY.values()), cap.key
            assert cap.key in set(caps.TOOL_CAPABILITY.values()), cap.key

    def test_a_route_or_tool_has_one_capability(self):
        # ``_invert`` raises at import when a name appears twice; this pins that it still does.
        with pytest.raises(ValueError, match="two capabilities"):
            caps._invert({"read_calendar": ("a",), "draft_posts": ("a",)})

    def test_every_rest_route_resolves_from_a_request_path(self):
        for route, expected in caps.ROUTE_CAPABILITY.items():
            method, template = route.split(" ", 1)
            path = caps.API_PREFIX + re.sub(r"\{[^}]+\}", "11111111-1111-1111-1111-111111111111", template)
            assert caps.capability_for_request(method, path) == expected, route

    def test_only_the_caption_routes_and_tools_spend_one_ai_call_each(self):
        assert caps.route_uses_ai("POST", "/api/v1/ai/caption")
        assert caps.tool_uses_ai("adapt_caption")
        # A repurposing run reserves all of its calls itself, so the route must not spend one more.
        assert not caps.route_uses_ai("POST", "/api/v1/repurpose/jobs")
        assert not caps.tool_uses_ai("create_repurpose_job")
        assert caps.capability_for_request("POST", "/api/v1/repurpose/jobs") == "use_ai_assistant"
        assert caps.capability_for_request("POST", f"/api/v1/repurpose/jobs/{'1' * 8}/accept") == "draft_posts"

    def test_a_literal_segment_wins_over_a_parameter(self):
        assert caps.capability_for_request("GET", "/api/v1/slots/next-free") == "read_calendar"
        assert caps.capability_for_request("PATCH", "/api/v1/slots/abc") == "schedule_posts"

    def test_unknown_routes_resolve_to_nothing(self):
        assert caps.capability_for_request("GET", "/api/v1/not-a-route/") is None
        assert caps.capability_for_request("PUT", "/api/v1/posts/") is None

    def test_mcp_endpoint_is_recognised_with_and_without_a_slash(self):
        assert caps.is_mcp_route("POST", "/api/v1/mcp")
        assert caps.is_mcp_route("POST", "/api/v1/mcp/")
        assert not caps.is_mcp_route("GET", "/api/v1/mcp/")


class TestPresetsAndHelpers:
    def test_presets_use_known_capabilities(self):
        assert [p.key for p in caps.PRESETS] == [
            "read_only_analyst",
            "drafting_assistant",
            "inbox_helper",
            "full_agent",
        ]
        for preset in caps.PRESETS:
            assert preset.capabilities and set(preset.capabilities) <= set(caps.CAPABILITY_KEYS)

    def test_read_only_analyst_cannot_write(self):
        analyst = caps.preset("read_only_analyst")
        assert {caps.capability(k).risk for k in analyst.capabilities} == {"low"}

    def test_approval_presets_require_approval(self):
        assert caps.preset("drafting_assistant").requires_approval
        assert caps.preset("inbox_helper").requires_approval
        assert not caps.preset("full_agent").requires_approval

    def test_full_agent_leaves_out_approvals_and_settings_write(self):
        full = set(caps.preset("full_agent").capabilities)
        assert "manage_approvals" not in full and "write_settings" not in full

    def test_clean_drops_unknown_and_keeps_catalog_order(self):
        assert caps.clean(["write_settings", "nope", "read_calendar", "read_calendar"]) == [
            "read_calendar",
            "write_settings",
        ]

    def test_scope_round_trip(self):
        scope = " ".join(["mcp", *caps.scopes_for(["draft_posts", "read_calendar"])])
        assert caps.from_scope(scope) == ["read_calendar", "draft_posts"]
        assert caps.from_scope("mcp") is None

    def test_narrow_permissions_withdraws_the_guarded_permissions_held_capabilities_do_not_carry(self):
        perms = {
            "create_posts": True,
            "publish_directly": True,
            "approve_posts": True,
            "reply_from_inbox": True,
            "edit_others_posts": True,
            "view_analytics": True,
        }
        narrowed = caps.narrow_permissions(perms, ["draft_posts"])
        assert narrowed["create_posts"] is True
        assert narrowed["publish_directly"] is False  # only schedule_posts carries it
        assert narrowed["approve_posts"] is False  # only manage_approvals carries it
        assert narrowed["reply_from_inbox"] is False  # only reply_inbox carries it
        # Not guarded: these only matter on routes their own capability already gates, so reads keep working.
        assert narrowed["view_analytics"] is True
        assert narrowed["edit_others_posts"] is True
        assert caps.narrow_permissions(perms, ["schedule_posts"])["publish_directly"] is True
        assert caps.narrow_permissions(perms, None) is perms

    def test_every_guarded_permission_is_carried_by_some_capability(self):
        carried = {p for c in caps.CAPABILITIES for p in c.permissions}
        assert carried >= caps.GUARDED_PERMISSIONS

    def test_permissions_for_capabilities(self):
        assert caps.permissions_for(["schedule_posts"]) == [
            "create_posts",
            "manage_social_accounts",
            "publish_directly",
        ]
