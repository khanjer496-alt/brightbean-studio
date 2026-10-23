"""``/api/v1/best-times/*``, ``/api/v1/evergreen/*`` and their MCP twins."""

from __future__ import annotations

import datetime as dt
import json
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.api.tests.world import make_post, tool_error, tool_result
from apps.calendar.models import EvergreenItem, PostingSlot
from apps.composer.models import ContentCategory, Idea, PostTemplate
from apps.mcp.tools import get_tool

BEST = "/api/v1/best-times/"
EVERGREEN = "/api/v1/evergreen/"
NO_UUID = "00000000-0000-4000-8000-000000000000"


def send(client, method, url, payload=None):
    return getattr(client, method)(url, data=json.dumps(payload or {}), content_type="application/json")


def published(world, accounts=None, caption="Evergreen tip"):
    post = make_post(world.workspace, world.owner, accounts or [world.account1], status="published", caption=caption)
    post.platform_posts.update(published_at=timezone.now() - timedelta(days=60))
    return post


@pytest.mark.django_db
class TestBestTimesRest:
    def test_requires_a_key(self, world):
        assert world.anonymous.get(f"{BEST}?social_account_id={world.account1.id}").status_code == 401
        assert send(world.anonymous, "post", f"{BEST}apply", {}).status_code == 401

    def test_defaults_are_labelled_and_shaped(self, world):
        body = world.full.get(f"{BEST}?social_account_id={world.account1.id}&count=4").json()
        assert body["basis"] == "defaults" and body["sample_size"] == 0
        assert body["basis_label"] == "Based on general defaults (no verified study for this network)"
        assert 1 <= len(body["suggestions"]) <= 4
        assert len(body["grid"]) == 7 and all(len(row) == 24 for row in body["grid"])
        assert len(body["grid_posts"]) == 7 and not any(map(any, body["grid_posts"]))
        first = body["suggestions"][0]
        assert set(first) == {"day_of_week", "day_name", "time", "score", "posts"}

    def test_allowlist_and_workspace_scoping(self, world):
        assert world.narrow.get(f"{BEST}?social_account_id={world.account2.id}").status_code == 403
        assert world.full.get(f"{BEST}?social_account_id={world.other_account.id}").status_code == 403

    def test_count_is_bounded(self, world):
        assert world.full.get(f"{BEST}?social_account_id={world.account1.id}&count=99").status_code == 422

    def test_apply_replaces_slots_and_is_idempotent(self, world):
        PostingSlot.objects.create(social_account=world.account1, day_of_week=6, time=dt.time(3, 7))
        payload = {"social_account_id": str(world.account1.id), "count": 3, "mode": "replace"}
        first = send(world.full, "post", f"{BEST}apply", payload).json()
        assert first["applied"] is True and first["remove"] == ["Sun 03:07"]
        assert len(first["add"]) == 3
        slots = PostingSlot.objects.filter(social_account=world.account1)
        assert slots.count() == 3 and not slots.filter(time=dt.time(3, 7)).exists()
        again = send(world.full, "post", f"{BEST}apply", payload).json()
        assert again["add"] == [] and again["remove"] == []
        assert PostingSlot.objects.filter(social_account=world.account1).count() == 3

    def test_add_mode_keeps_existing_slots_and_preview_writes_nothing(self, world):
        PostingSlot.objects.create(social_account=world.account1, day_of_week=6, time=dt.time(3, 7))
        preview = send(
            world.full,
            "post",
            f"{BEST}apply",
            {"social_account_id": str(world.account1.id), "mode": "add", "preview": True},
        ).json()
        assert preview["applied"] is False and preview["add"] and preview["remove"] == []
        assert PostingSlot.objects.filter(social_account=world.account1).count() == 1
        send(world.full, "post", f"{BEST}apply", {"social_account_id": str(world.account1.id), "mode": "add"})
        assert PostingSlot.objects.filter(social_account=world.account1, time=dt.time(3, 7)).exists()
        assert PostingSlot.objects.filter(social_account=world.account1).count() > 1

    def test_apply_needs_manage_social_accounts_and_the_allowlist(self, world):
        body = {"social_account_id": str(world.account1.id)}
        assert send(world.bare, "post", f"{BEST}apply", body).status_code == 403
        assert (
            send(world.narrow, "post", f"{BEST}apply", {"social_account_id": str(world.account2.id)}).status_code == 403
        )
        assert PostingSlot.objects.filter(social_account=world.account1).count() == 0

    def test_apply_rejects_unknown_mode(self, world):
        r = send(world.full, "post", f"{BEST}apply", {"social_account_id": str(world.account1.id), "mode": "wipe"})
        assert r.status_code == 422


@pytest.mark.django_db
class TestEvergreenRest:
    def test_requires_a_key_and_create_posts(self, world):
        assert world.anonymous.get(EVERGREEN).status_code == 401
        assert world.bare.get(EVERGREEN).status_code == 403
        assert send(world.bare, "post", EVERGREEN, {"post_id": NO_UUID}).status_code == 403

    def test_add_from_a_published_post_one_item_per_channel(self, world):
        post = published(world, [world.account1, world.account2])
        r = send(world.full, "post", EVERGREEN, {"post_id": str(post.id), "category_name": "Evergreen: Tips"})
        assert r.status_code == 201, r.content
        items = r.json()["items"]
        assert len(items) == 2 and {i["category_name"] for i in items} == {"Evergreen: Tips"}
        assert items[0]["interval_days"] == 30 and items[0]["repeat_count"] == 0
        assert ContentCategory.objects.filter(workspace=world.workspace, name="Evergreen: Tips").count() == 1
        again = send(world.full, "post", EVERGREEN, {"post_id": str(post.id)})
        assert again.status_code == 409

    def test_a_post_is_visible_only_when_every_channel_is_on_the_allowlist(self, world):
        both = published(world, [world.account1, world.account2])
        assert send(world.narrow, "post", EVERGREEN, {"post_id": str(both.id)}).status_code == 404
        only_first = published(world, [world.account1])
        items = send(world.narrow, "post", EVERGREEN, {"post_id": str(only_first.id)}).json()["items"]
        assert [i["social_account_id"] for i in items] == [str(world.account1.id)]
        assert send(
            world.full, "post", EVERGREEN, {"post_id": str(both.id), "social_account_id": str(world.account2.id)}
        ).json()["items"][0]["social_account_id"] == str(world.account2.id)

    def test_unpublished_posts_and_foreign_posts_are_refused(self, world):
        draft = make_post(world.workspace, world.owner, [world.account1])
        assert send(world.full, "post", EVERGREEN, {"post_id": str(draft.id)}).status_code == 422
        foreign = make_post(world.other_workspace, world.other_owner, [world.other_account], status="published")
        assert send(world.full, "post", EVERGREEN, {"post_id": str(foreign.id)}).status_code == 404

    def test_exactly_one_source_is_required(self, world):
        assert send(world.full, "post", EVERGREEN, {}).status_code == 422
        both = {"post_id": NO_UUID, "template_id": NO_UUID}
        assert send(world.full, "post", EVERGREEN, both).status_code == 422

    def test_templates_and_ideas_need_an_account(self, world):
        template = PostTemplate.objects.create(
            workspace=world.workspace, name="T", template_data={"caption": "Template tip"}
        )
        idea = Idea.objects.create(workspace=world.workspace, title="I", description="Idea tip")
        assert send(world.full, "post", EVERGREEN, {"template_id": str(template.id)}).status_code == 422
        ok = send(
            world.full,
            "post",
            EVERGREEN,
            {"template_id": str(template.id), "social_account_id": str(world.account1.id)},
        )
        assert ok.status_code == 201 and ok.json()["items"][0]["source_kind"] == "template"
        ok = send(world.full, "post", EVERGREEN, {"idea_id": str(idea.id), "social_account_id": str(world.account2.id)})
        assert ok.status_code == 201 and ok.json()["items"][0]["caption"] == "Idea tip"
        outside = send(
            world.narrow, "post", EVERGREEN, {"idea_id": str(idea.id), "social_account_id": str(world.account2.id)}
        )
        assert outside.status_code == 403

    def test_pre_approval_needs_approve_posts(self, world):
        from apps.api_keys import services
        from apps.members.models import PERMISSION_KEYS

        issued = services.issue_api_key(
            workspace=world.workspace,
            social_accounts=[world.account1],
            issued_by=world.owner,
            name="writer",
            permissions=[p for p in PERMISSION_KEYS if p != "approve_posts"],
        )
        from apps.api.tests.world import bearer

        writer = bearer(issued.plaintext_token)
        post = published(world)
        r = send(writer, "post", EVERGREEN, {"post_id": str(post.id), "pre_approved": True})
        assert r.status_code == 403
        assert send(world.full, "post", EVERGREEN, {"post_id": str(post.id), "pre_approved": True}).status_code == 201

    def test_list_filter_get_update_pause_and_remove(self, world):
        post = published(world, [world.account1, world.account2])
        items = send(world.full, "post", EVERGREEN, {"post_id": str(post.id)}).json()["items"]
        first = next(i for i in items if i["social_account_id"] == str(world.account1.id))
        listed = world.full.get(EVERGREEN).json()
        assert len(listed["items"]) == 2 and listed["next_cursor"] is None
        assert [i["id"] for i in world.narrow.get(EVERGREEN).json()["items"]] == [first["id"]]
        assert len(world.full.get(f"{EVERGREEN}?paused=true").json()["items"]) == 0

        r = send(
            world.full, "patch", f"{EVERGREEN}{first['id']}", {"is_paused": True, "interval_days": 90, "max_repeats": 2}
        )
        assert r.status_code == 200, r.content
        assert (r.json()["is_paused"], r.json()["interval_days"], r.json()["max_repeats"]) == (True, 90, 2)
        assert len(world.full.get(f"{EVERGREEN}?paused=true").json()["items"]) == 1
        cleared = send(
            world.full, "patch", f"{EVERGREEN}{first['id']}", {"clear_max_repeats": True, "is_paused": False}
        )
        assert cleared.json()["max_repeats"] is None and cleared.json()["is_paused"] is False

        detail = world.full.get(f"{EVERGREEN}{first['id']}").json()
        assert detail["repeats"] == [] and detail["id"] == first["id"]

        assert (
            world.narrow.get(f"{EVERGREEN}{[i for i in items if i['id'] != first['id']][0]['id']}").status_code == 404
        )
        assert world.full.delete(f"{EVERGREEN}{first['id']}").status_code == 200
        assert EvergreenItem.objects.filter(id=first["id"]).count() == 0
        assert world.full.delete(f"{EVERGREEN}{first['id']}").status_code == 404

    def test_other_workspaces_items_are_invisible(self, world):
        foreign = EvergreenItem.objects.create(
            workspace=world.other_workspace, social_account=world.other_account, caption="theirs"
        )
        assert world.full.get(f"{EVERGREEN}{foreign.id}").status_code == 404
        assert world.full.delete(f"{EVERGREEN}{foreign.id}").status_code == 404
        assert send(world.full, "patch", f"{EVERGREEN}{foreign.id}", {"is_paused": True}).status_code == 404
        assert world.full.get(EVERGREEN).json()["items"] == []

    def test_rules_are_validated(self, world):
        post = published(world)
        assert send(world.full, "post", EVERGREEN, {"post_id": str(post.id), "interval_days": 0}).status_code == 422

    def test_idempotency_key_replays_the_create(self, world):
        post = published(world)
        headers = {"HTTP_IDEMPOTENCY_KEY": "evergreen-1"}
        body = json.dumps({"post_id": str(post.id)})
        first = world.full.post(EVERGREEN, data=body, content_type="application/json", **headers)
        second = world.full.post(EVERGREEN, data=body, content_type="application/json", **headers)
        assert first.status_code == second.status_code == 201
        assert EvergreenItem.objects.count() == 1


@pytest.mark.django_db
class TestMcp:
    TOOLS = (
        "get_best_times", "apply_best_times", "list_evergreen_items", "add_to_evergreen", "pause_evergreen_item",
        "remove_from_evergreen",
    )  # fmt: skip

    def test_tools_are_listed_with_agent_grade_descriptions(self):
        for name in self.TOOLS:
            tool = get_tool(name)
            assert len(tool.description) > 60 and tool.input_schema["additionalProperties"] is False
        assert get_tool("get_best_times").read_only and get_tool("list_evergreen_items").read_only
        assert not get_tool("add_to_evergreen").read_only and not get_tool("apply_best_times").read_only

    def test_best_times_and_apply_match_rest(self, world):
        mcp = tool_result(world.full, "get_best_times", {"social_account_id": str(world.account1.id), "count": 3})
        rest = world.full.get(f"{BEST}?social_account_id={world.account1.id}&count=3").json()
        assert mcp["suggestions"] == rest["suggestions"] and mcp["basis"] == "defaults"
        preview = tool_result(
            world.full, "apply_best_times", {"social_account_id": str(world.account1.id), "preview": True}
        )
        assert preview["applied"] is False
        done = tool_result(world.full, "apply_best_times", {"social_account_id": str(world.account1.id), "count": 2})
        assert done["applied"] is True and PostingSlot.objects.filter(social_account=world.account1).count() == 2

    def test_apply_is_permission_checked(self, world):
        assert "Permission denied" in tool_error(
            world.bare, "apply_best_times", {"social_account_id": str(world.account1.id)}
        )
        assert "allowlist" in tool_error(world.narrow, "get_best_times", {"social_account_id": str(world.account2.id)})

    def test_evergreen_round_trip(self, world):
        post = published(world)
        added = tool_result(world.full, "add_to_evergreen", {"post_id": str(post.id), "interval_days": 14})
        item_id = added["items"][0]["id"]
        assert added["items"][0]["interval_days"] == 14
        listed = tool_result(world.full, "list_evergreen_items", {})
        assert [i["id"] for i in listed["items"]] == [item_id]
        paused = tool_result(world.full, "pause_evergreen_item", {"item_id": item_id})
        assert paused["is_paused"] is True
        resumed = tool_result(world.full, "pause_evergreen_item", {"item_id": item_id, "paused": False})
        assert resumed["is_paused"] is False
        assert tool_result(world.full, "remove_from_evergreen", {"item_id": item_id})["id"] == item_id
        assert tool_result(world.full, "list_evergreen_items", {})["items"] == []

    def test_evergreen_tools_enforce_scope_and_permissions(self, world):
        post = published(world, [world.account2])
        assert "Permission denied" in tool_error(world.bare, "list_evergreen_items", {})
        item = tool_result(world.full, "add_to_evergreen", {"post_id": str(post.id)})["items"][0]
        assert "not found" in tool_error(world.narrow, "pause_evergreen_item", {"item_id": item["id"]}).lower()
        assert "not found" in tool_error(world.other, "remove_from_evergreen", {"item_id": item["id"]}).lower()
        assert "post_id" in tool_error(world.full, "add_to_evergreen", {"post_id": "nope"})
