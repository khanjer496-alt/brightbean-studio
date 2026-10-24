"""REST and MCP for engagement automations (/plugs) and RSS auto-posting (/feeds)."""

from __future__ import annotations

import json
import uuid
from datetime import timedelta

import httpx
import pytest
from django.utils import timezone

from apps.api.tests.world import bearer, call_tool, make_post, tool_error, tool_result
from apps.api_keys import services as key_services
from apps.composer.models import Feed, PlatformPost
from apps.members.models import PERMISSION_KEYS
from apps.social_accounts.models import SocialAccount

pytestmark = pytest.mark.django_db

FEED_URL = "https://blog.example/feed.xml"
RSS = (
    '<?xml version="1.0"?><rss version="2.0"><channel><title>Blog</title><link>https://blog.example</link>'
    "<item><title>One</title><link>https://blog.example/1</link><guid>1</guid><description>Hi</description></item>"
    "</channel></rss>"
)


@pytest.fixture
def world(db):
    from apps.api.tests.world import build_world

    return build_world()


@pytest.fixture(autouse=True)
def fake_web(monkeypatch):
    def fetch(url, headers, *, max_bytes, content_types=None):
        if url == FEED_URL:
            return httpx.Response(200, content=RSS.encode(), headers={"ETag": '"x"'}), url
        return None, None

    monkeypatch.setattr("apps.autopost.feeds.safe_fetch", fetch)


def published(world, account, hours_ago=1):
    post = make_post(world.workspace, world.owner, [account], status="published")
    PlatformPost.objects.filter(post=post).update(
        published_at=timezone.now() - timedelta(hours=hours_ago), platform_post_id="at://x/y/1"
    )
    return post


def post_json(client, path, payload):
    return client.post(path, data=json.dumps(payload), content_type="application/json")


def patch_json(client, path, payload):
    return client.patch(path, data=json.dumps(payload), content_type="application/json")


def rule_payload(world, **overrides):
    return {
        "social_account_id": str(world.account1.id),
        "trigger": "likes",
        "threshold": 50,
        "window_hours": 12,
        "action": "repost",
        **overrides,
    }


class TestPlugsRest:
    def test_create_list_read_update_delete(self, world):
        post = published(world, world.account1)
        created = post_json(world.full, "/api/v1/plugs/", rule_payload(world, post_id=str(post.id)))
        assert created.status_code == 201, created.content
        rule_id = created.json()["id"]
        assert created.json()["summary"].startswith("At 50 likes within 12 hours")

        listing = world.full.get("/api/v1/plugs/").json()
        assert [r["id"] for r in listing["rules"]] == [rule_id]
        by_account = {n["social_account_id"]: n for n in listing["networks"]}
        network = by_account[str(world.account1.id)]
        assert network["triggers"] == ["likes", "reposts"]
        # Bluesky's guidelines rule out boosting with several accounts.
        assert network["cross_actions"] == [] and network["reposter_account_ids"] == []
        assert network["max_actions_per_day"] >= 1 and network["requires_approval"] is False
        assert created.json()["reads_per_post"] == 1 + 12 * 60 // network["check_interval_minutes"]

        assert world.full.get(f"/api/v1/plugs/{rule_id}").json()["runs"] == []
        changed = patch_json(world.full, f"/api/v1/plugs/{rule_id}", {"threshold": 75, "is_active": False})
        assert changed.json()["threshold"] == 75 and changed.json()["is_active"] is False
        assert world.full.delete(f"/api/v1/plugs/{rule_id}").json()["deleted"] is True
        assert world.full.get(f"/api/v1/plugs/{rule_id}").status_code == 404

    def test_default_rule_and_cross_account_reposter(self, world):
        # Mastodon lets another account boost (each one waits for a person's approval).
        SocialAccount.objects.filter(pk__in=[world.account1.pk, world.account2.pk]).update(platform="mastodon")
        response = post_json(
            world.full, "/api/v1/plugs/", rule_payload(world, reposter_account_id=str(world.account2.id))
        )
        assert response.status_code == 201
        body = response.json()
        assert body["post_id"] is None and body["reposter_account_id"] == str(world.account2.id)

    def test_unsupported_combinations_say_why(self, world):
        response = post_json(world.full, "/api/v1/plugs/", rule_payload(world, trigger="views"))
        assert response.status_code == 422 and "does not report views" in response.json()["detail"]
        response = post_json(world.full, "/api/v1/plugs/", rule_payload(world, action="reply"))
        assert response.status_code == 422 and "Write the message" in response.json()["detail"]

    def test_reposter_from_another_workspace_is_refused(self, world):
        response = post_json(
            world.full, "/api/v1/plugs/", rule_payload(world, reposter_account_id=str(world.other_account.id))
        )
        assert response.status_code == 403

    def test_allowlist(self, world):
        # The narrow key only holds account 1.
        assert (
            post_json(
                world.narrow, "/api/v1/plugs/", rule_payload(world, social_account_id=str(world.account2.id))
            ).status_code
            == 403
        )
        assert (
            post_json(
                world.narrow, "/api/v1/plugs/", rule_payload(world, reposter_account_id=str(world.account2.id))
            ).status_code
            == 403
        )
        made = post_json(
            world.full, "/api/v1/plugs/", rule_payload(world, social_account_id=str(world.account2.id))
        ).json()
        assert world.narrow.get(f"/api/v1/plugs/{made['id']}").status_code == 404
        assert world.narrow.get("/api/v1/plugs/").json()["rules"] == []

    def test_other_workspaces_cannot_see_or_touch_rules(self, world):
        made = post_json(world.full, "/api/v1/plugs/", rule_payload(world)).json()
        assert world.other.get(f"/api/v1/plugs/{made['id']}").status_code == 404
        assert world.other.delete(f"/api/v1/plugs/{made['id']}").status_code == 404
        assert world.other.get("/api/v1/plugs/").json()["rules"] == []

    def test_permissions(self, world):
        assert world.bare.get("/api/v1/plugs/").status_code == 403
        assert post_json(world.bare, "/api/v1/plugs/", rule_payload(world)).status_code == 403
        reader = key_services.issue_api_key(
            workspace=world.workspace, social_accounts=[world.account1], issued_by=world.owner, name="r",
            permissions=["create_posts"],
        )  # fmt: skip
        client = bearer(reader.plaintext_token)
        assert client.get("/api/v1/plugs/").status_code == 200
        assert post_json(client, "/api/v1/plugs/", rule_payload(world)).status_code == 403

    def test_post_must_be_on_that_account(self, world):
        other_post = published(world, world.account2)
        response = post_json(world.full, "/api/v1/plugs/", rule_payload(world, post_id=str(other_post.id)))
        assert response.status_code == 404
        assert (
            post_json(world.full, "/api/v1/plugs/", rule_payload(world, post_id=str(uuid.uuid4()))).status_code == 404
        )

    def test_capability_tokens(self, world):
        def token(caps):
            issued = key_services.issue_api_key(
                workspace=world.workspace, social_accounts=[world.account1, world.account2], issued_by=world.owner,
                name="t", permissions=list(PERMISSION_KEYS), capabilities=caps,
            )  # fmt: skip
            return bearer(issued.plaintext_token)

        reader = token(["read_calendar"])
        assert reader.get("/api/v1/plugs/").status_code == 200
        denied = post_json(reader, "/api/v1/plugs/", rule_payload(world))
        assert denied.status_code == 403 and "schedule_posts" in denied.json()["detail"]
        assert post_json(token(["schedule_posts"]), "/api/v1/plugs/", rule_payload(world)).status_code == 201
        assert token(["draft_posts"]).get("/api/v1/plugs/").status_code == 403


class TestFeedsRest:
    def follow(self, world):
        response = post_json(world.full, "/api/v1/feeds/", {"url": FEED_URL})
        assert response.status_code == 201, response.content
        return response.json()

    def test_follow_enable_read_check_stop(self, world):
        feed = self.follow(world)
        assert feed["name"] == "Blog" and feed["autopost"] is None
        assert post_json(world.full, "/api/v1/feeds/", {"url": FEED_URL}).json()["id"] == feed["id"]

        enabled = patch_json(
            world.full, f"/api/v1/feeds/{feed['id']}",
            {"account_ids": [str(world.account1.id)], "mode": "link", "destination": "draft", "exclude_keywords": ["Sponsored"]},
        )  # fmt: skip
        assert enabled.status_code == 200, enabled.content
        settings = enabled.json()["autopost"]
        assert (
            settings["is_active"]
            and settings["exclude_keywords"] == ["sponsored"]
            and settings["max_posts_per_day"] == 3
        )

        assert [f["id"] for f in world.full.get("/api/v1/feeds/").json()["feeds"]] == [feed["id"]]
        assert world.full.get(f"/api/v1/feeds/{feed['id']}").json()["items"] == []

        checked = world.full.post(f"/api/v1/feeds/{feed['id']}/check")
        assert checked.status_code == 200 and checked.json()["status"] == "ok"
        # The item was already in the feed when auto-posting was switched on: recorded, not posted.
        assert world.full.get(f"/api/v1/feeds/{feed['id']}").json()["items"] == []
        assert world.full.post(f"/api/v1/feeds/{feed['id']}/check").status_code == 429

        paused = patch_json(world.full, f"/api/v1/feeds/{feed['id']}", {"is_active": False})
        assert paused.json()["autopost"]["is_active"] is False
        assert world.full.delete(f"/api/v1/feeds/{feed['id']}/autopost").status_code == 200
        assert world.full.get(f"/api/v1/feeds/{feed['id']}").json()["autopost"] is None

    def test_switching_on_needs_accounts_and_validates(self, world):
        feed = self.follow(world)
        assert patch_json(world.full, f"/api/v1/feeds/{feed['id']}", {"mode": "ai"}).status_code == 422
        bad = patch_json(
            world.full,
            f"/api/v1/feeds/{feed['id']}",
            {"account_ids": [str(world.account1.id)], "destination": "delay", "delay_minutes": 99999},
        )
        assert bad.status_code == 422  # beyond the schema's bound

    def test_not_a_feed(self, world):
        response = post_json(world.full, "/api/v1/feeds/", {"url": "https://nowhere.example/x"})
        assert response.status_code == 422

    def test_allowlist_and_workspace_scoping(self, world):
        feed = self.follow(world)
        assert (
            patch_json(
                world.narrow, f"/api/v1/feeds/{feed['id']}", {"account_ids": [str(world.account2.id)]}
            ).status_code
            == 403
        )
        patch_json(
            world.full, f"/api/v1/feeds/{feed['id']}", {"account_ids": [str(world.account1.id), str(world.account2.id)]}
        )
        # The narrow key can't see a feed that posts to an account it doesn't hold.
        assert world.narrow.get(f"/api/v1/feeds/{feed['id']}").status_code == 404
        assert world.narrow.get("/api/v1/feeds/").json()["feeds"] == []
        assert world.narrow.delete(f"/api/v1/feeds/{feed['id']}/autopost").status_code == 404
        assert world.other.get(f"/api/v1/feeds/{feed['id']}").status_code == 404
        assert world.other.get("/api/v1/feeds/").json()["feeds"] == []

    def test_permissions_and_capabilities(self, world):
        feed = self.follow(world)
        assert world.bare.get("/api/v1/feeds/").status_code == 403
        assert post_json(world.bare, "/api/v1/feeds/", {"url": FEED_URL}).status_code == 403
        issued = key_services.issue_api_key(
            workspace=world.workspace, social_accounts=[world.account1], issued_by=world.owner, name="t",
            permissions=list(PERMISSION_KEYS), capabilities=["draft_posts"],
        )  # fmt: skip
        client = bearer(issued.plaintext_token)
        assert client.get("/api/v1/feeds/").status_code == 403
        assert patch_json(client, f"/api/v1/feeds/{feed['id']}", {"is_active": True}).status_code == 403
        assert Feed.objects.filter(id=feed["id"]).exists()


class TestMcp:
    def test_tools_are_listed_and_mapped(self, world):
        envelope = world.full.post(
            "/api/v1/mcp/",
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
            content_type="application/json",
        ).json()
        names = {t["name"] for t in envelope["result"]["tools"]}
        assert {
            "list_plug_rules", "get_plug_rule", "create_plug_rule", "update_plug_rule", "delete_plug_rule",
            "list_feeds", "get_feed", "add_feed", "update_feed_autopost", "check_feed", "stop_feed_autopost",
        } <= names  # fmt: skip

    def test_plug_rule_round_trip(self, world):
        rule = tool_result(world.full, "create_plug_rule", {k: v for k, v in rule_payload(world).items()})
        assert rule["action"] == "repost"
        assert tool_result(world.full, "list_plug_rules")["rules"][0]["id"] == rule["id"]
        tool_result(world.full, "update_plug_rule", {"rule_id": rule["id"], "threshold": 10})
        assert tool_result(world.full, "get_plug_rule", {"rule_id": rule["id"]})["threshold"] == 10
        assert tool_result(world.full, "delete_plug_rule", {"rule_id": rule["id"]})["deleted"] is True

    def test_same_refusals_as_rest(self, world):
        assert "does not report views" in tool_error(
            world.full, "create_plug_rule", rule_payload(world, trigger="views")
        )
        assert "allowlist" in tool_error(
            world.narrow, "create_plug_rule", rule_payload(world, social_account_id=str(world.account2.id))
        )
        assert "threshold" in tool_error(world.full, "create_plug_rule", rule_payload(world, threshold=0))
        assert "Permission denied" in tool_error(world.bare, "create_plug_rule", rule_payload(world))
        assert "not found" in tool_error(world.other, "get_plug_rule", {"rule_id": str(uuid.uuid4())}).lower()

    def test_feed_round_trip(self, world):
        feed = tool_result(world.full, "add_feed", {"url": FEED_URL})
        enabled = tool_result(
            world.full,
            "update_feed_autopost",
            {"feed_id": feed["id"], "account_ids": [str(world.account1.id)], "mode": "ai"},
        )
        assert enabled["autopost"]["mode"] == "ai"
        assert tool_result(world.full, "list_feeds")["feeds"][0]["id"] == feed["id"]
        assert tool_result(world.full, "check_feed", {"feed_id": feed["id"]})["status"] == "ok"
        assert tool_result(world.full, "get_feed", {"feed_id": feed["id"]})["autopost"]["is_active"] is True
        assert tool_result(world.full, "stop_feed_autopost", {"feed_id": feed["id"]})["deleted"] is True

    def test_limited_tokens_only_see_the_tools_they_can_use(self, world):
        issued = key_services.issue_api_key(
            workspace=world.workspace, social_accounts=[world.account1], issued_by=world.owner, name="t",
            permissions=list(PERMISSION_KEYS), capabilities=["read_calendar"],
        )  # fmt: skip
        client = bearer(issued.plaintext_token)
        names = {
            t["name"]
            for t in client.post(
                "/api/v1/mcp/",
                data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
                content_type="application/json",
            ).json()["result"]["tools"]
        }
        assert {"list_plug_rules", "get_feed"} <= names and "create_plug_rule" not in names
        assert "capability" in json.dumps(call_tool(client, "create_plug_rule", rule_payload(world))).lower()
