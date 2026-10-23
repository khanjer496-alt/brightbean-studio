"""The brand kit over REST and MCP."""

import pytest

pytestmark = pytest.mark.django_db


class TestApi:
    URL = "/api/v1/brand-kit/"

    def test_requires_a_key_and_permissions(self, world):
        assert world.anonymous.get(self.URL).status_code == 401
        assert world.bare.get(self.URL).status_code == 403
        assert world.bare.patch(self.URL, data="{}", content_type="application/json").status_code == 403

    def test_read_update_and_isolation(self, world):
        import json

        r = world.full.patch(
            self.URL,
            data=json.dumps({"tagline": "Hi", "colors": [{"name": "A", "hex": "#112233"}], "logos": {"primary": None}}),
            content_type="application/json",
        )
        assert r.status_code == 200, r.content
        body = world.full.get(self.URL).json()
        assert body["tagline"] == "Hi" and body["colors"][0]["hex"] == "#112233" and body["logos"]["primary"] is None
        assert world.other.get(self.URL).json()["tagline"] == ""
        bad = world.full.patch(self.URL, data=json.dumps({"colors": [{"hex": "red"}]}), content_type="application/json")
        assert bad.status_code == 422
        slot = world.full.patch(self.URL, data=json.dumps({"logos": {"huge": None}}), content_type="application/json")
        assert slot.status_code == 422

    def test_mcp_tools(self, world):
        from apps.api.tests.world import tool_error, tool_result

        assert tool_result(world.full, "get_brand_kit")["tagline"] == ""
        updated = tool_result(world.full, "update_brand_kit", {"dos": "Be clear", "default_hashtags": ["x"]})
        assert updated["dos"] == "Be clear" and updated["default_hashtags"] == ["#x"]
        assert "manage_workspace_settings" in tool_error(world.bare, "update_brand_kit", {"dos": "x"})
        assert "Permission denied" in tool_error(world.bare, "get_brand_kit")
        assert tool_result(world.other, "get_brand_kit")["dos"] == ""
