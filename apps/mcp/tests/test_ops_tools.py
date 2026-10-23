"""The MCP twins of the REST coverage endpoints: posts, approvals, queues, planning content, usage."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from apps.api.tests.world import MCP_URL, call_tool, make_post, tool_error, tool_result
from apps.calendar.models import PostingSlot, Queue
from apps.composer.models import ContentCategory, Idea, IdeaGroup, Post, PostTemplate, Tag
from apps.mcp.tools import all_tools, get_tool

NO_UUID = "00000000-0000-4000-8000-000000000000"


def names(items, key="name"):
    return [i[key] for i in items]


@pytest.mark.django_db
class TestCatalog:
    NEW_TOOLS = {
        "update_post", "delete_post", "duplicate_post", "list_approvals", "review_post", "list_members",
        "list_post_comments", "add_post_comment", "list_queues", "get_queue", "add_post_to_queue", "remove_from_queue",
        "list_posting_slots", "create_posting_slot", "update_posting_slot", "delete_posting_slot",
        "find_next_free_slot", "list_ideas", "get_idea", "create_idea", "update_idea", "delete_idea",
        "list_idea_groups", "create_idea_group", "delete_idea_group", "list_templates", "get_template",
        "create_template", "update_template", "delete_template", "list_tags", "create_tag", "rename_tag",
        "delete_tag", "list_categories", "create_category", "update_category", "delete_category",
        "list_calendar_events", "create_calendar_event", "delete_calendar_event", "list_hashtag_sets",
        "list_signatures", "get_brand_voice", "update_brand_voice", "get_usage",
    }  # fmt: skip

    def test_every_new_tool_is_listed_with_an_agent_grade_description(self, world):
        registered = {t.name for t in all_tools()}
        assert registered >= self.NEW_TOOLS
        for name in self.NEW_TOOLS:
            tool = get_tool(name)
            assert len(tool.description) > 60, name
            assert tool.input_schema["type"] == "object" and tool.input_schema["additionalProperties"] is False

    def test_write_tools_keep_their_audit_trail_and_reads_do_not(self):
        reads = {n for n in self.NEW_TOOLS if n.startswith(("list_", "get_", "find_"))}
        for name in self.NEW_TOOLS:
            assert get_tool(name).read_only == (name in reads), name

    def test_permission_requirements_are_described(self):
        for name in self.NEW_TOOLS - {"list_queues", "get_queue", "list_posting_slots", "list_categories",
                                       "list_hashtag_sets", "list_signatures", "list_tags", "get_template",
                                       "list_templates", "find_next_free_slot", "get_usage", "list_calendar_events",
                                       "list_members"}:  # fmt: skip
            assert "Needs" in get_tool(name).description, name

    def test_unauthenticated_calls_are_401(self, world):
        r = world.anonymous.post(
            MCP_URL, data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}), content_type="application/json"
        )
        assert r.status_code == 401

    def test_schema_validation_rejects_bad_arguments(self, world):
        assert "post_id" in tool_error(world.full, "delete_post", {})
        assert "additional" in tool_error(world.full, "get_usage", {"x": 1}).lower()
        assert tool_error(world.full, "create_idea", {"title": 5})


@pytest.mark.django_db
class TestPostsTools:
    def test_list_posts_filters_and_paginates(self, world):
        a = make_post(world.workspace, world.owner, [world.account1], caption="Spring sale", tags=["promo"])
        make_post(world.workspace, world.owner, [world.account2], status="scheduled")
        make_post(world.workspace, world.owner, [world.account1, world.account2])
        assert len(tool_result(world.full, "list_posts")["posts"]) == 3
        assert [p["id"] for p in tool_result(world.narrow, "list_posts")["posts"]] == [str(a.id)]
        assert [p["id"] for p in tool_result(world.full, "list_posts", {"tag": "promo"})["posts"]] == [str(a.id)]
        assert [p["id"] for p in tool_result(world.full, "list_posts", {"search": "spring"})["posts"]] == [str(a.id)]
        only = tool_result(
            world.full, "list_posts", {"status": "scheduled", "social_account_id": str(world.account2.id)}
        )
        assert len(only["posts"]) == 1
        page = tool_result(world.full, "list_posts", {"limit": 2})
        assert len(page["posts"]) == 2 and page["next_cursor"]
        rest = tool_result(world.full, "list_posts", {"limit": 2, "cursor": page["next_cursor"]})
        assert len(rest["posts"]) == 1 and rest["next_cursor"] is None

    def test_list_posts_validation_and_allowlist(self, world):
        assert "allowlist" in tool_error(world.narrow, "list_posts", {"social_account_id": str(world.account2.id)})
        assert "cursor" in tool_error(world.full, "list_posts", {"cursor": "junk"})
        assert "date_from" in tool_error(world.full, "list_posts", {"date_from": "tomorrow"})
        assert tool_error(world.full, "list_posts", {"limit": 0})
        bad_range = {"date_from": "2026-02-01T00:00:00Z", "date_to": "2026-01-01T00:00:00Z"}
        assert "date_to" in tool_error(world.full, "list_posts", bad_range)

    def test_list_posts_matches_rest(self, world):
        make_post(world.workspace, world.owner, [world.account1], tags=["a"])
        rest = world.full.get("/api/v1/posts/?limit=10").json()
        assert tool_result(world.full, "list_posts", {"limit": 10}) == rest

    def test_delete_post(self, world):
        post = make_post(world.workspace, world.owner, [world.account1])
        published = make_post(world.workspace, world.owner, [world.account1], status="published")
        outside = make_post(world.workspace, world.owner, [world.account2])
        foreign = make_post(world.other_workspace, world.other_owner, [world.other_account])
        assert "create_posts" in tool_error(world.bare, "delete_post", {"post_id": str(post.id)})
        assert "published" in tool_error(world.full, "delete_post", {"post_id": str(published.id)})
        assert tool_error(world.narrow, "delete_post", {"post_id": str(outside.id)}) == "Post not found"
        assert tool_error(world.full, "delete_post", {"post_id": str(foreign.id)}) == "Post not found"
        assert "UUID" in tool_error(world.full, "delete_post", {"post_id": "nope"})
        assert tool_result(world.full, "delete_post", {"post_id": str(post.id)}) == {
            "deleted": True,
            "id": str(post.id),
        }
        assert Post.objects.filter(id__in=[published.id, outside.id, foreign.id]).count() == 3
        assert not Post.objects.filter(id=post.id).exists()

    def test_duplicate_post(self, world):
        post = make_post(world.workspace, world.owner, [world.account1], status="published", caption="Hi")
        outside = make_post(world.workspace, world.owner, [world.account1, world.account2])
        assert "create_posts" in tool_error(world.bare, "duplicate_post", {"post_id": str(post.id)})
        assert tool_error(world.narrow, "duplicate_post", {"post_id": str(outside.id)}) == "Post not found"
        copy = tool_result(world.full, "duplicate_post", {"post_id": str(post.id)})
        assert copy["status"] == "draft" and copy["caption"] == "Hi" and copy["id"] != str(post.id)

    def test_create_draft_and_schedule_post_take_tags_and_category(self, world):
        category = ContentCategory.objects.create(workspace=world.workspace, name="Promo")
        args = {
            "social_account_id": str(world.account1.id),
            "caption": "hello",
            "tags": ["a", "b"],
            "category_id": str(category.id),
        }
        draft = tool_result(world.full, "create_draft", args)
        assert draft["tags"] == ["a", "b"] and draft["category_id"] == str(category.id)
        when = (dt.datetime.now(dt.UTC) + dt.timedelta(days=2)).isoformat()
        scheduled = tool_result(world.full, "schedule_post", {**args, "scheduled_at": when})
        assert scheduled["tags"] == ["a", "b"]
        foreign = ContentCategory.objects.create(workspace=world.other_workspace, name="Theirs")
        assert "category_id" in tool_error(world.full, "create_draft", {**args, "category_id": str(foreign.id)})
        assert Post.objects.filter(workspace=world.workspace).count() == 2


@pytest.mark.django_db
class TestApprovalTools:
    def pending(self, world, *accounts, status="pending_review"):
        return make_post(world.workspace, world.owner, list(accounts) or [world.account1], status=status)

    def test_list_and_decide(self, world):
        a, b, c = self.pending(world), self.pending(world), self.pending(world)
        self.pending(world, world.account2)
        listed = tool_result(world.full, "list_approvals")
        assert len(listed["posts"]) == 4
        assert len(tool_result(world.narrow, "list_approvals")["posts"]) == 3
        assert (
            tool_result(world.full, "review_post", {"post_id": str(a.id), "decision": "approve", "comment": "ok"})[
                "status"
            ]
            == "approved"
        )
        assert (
            tool_result(
                world.full, "review_post", {"post_id": str(b.id), "decision": "request_changes", "comment": "fix"}
            )["status"]
            == "changes_requested"
        )
        assert (
            tool_result(world.full, "review_post", {"post_id": str(c.id), "decision": "reject", "comment": "no"})[
                "status"
            ]
            == "rejected"
        )
        assert len(tool_result(world.full, "list_approvals", {"status": "approved"})["posts"]) == 1

    def test_validation_scope_and_isolation(self, world):
        post = self.pending(world)
        outside = self.pending(world, world.account2)
        foreign = make_post(world.other_workspace, world.other_owner, [world.other_account], status="pending_review")
        assert "approve_posts" in tool_error(
            world.bare, "review_post", {"post_id": str(post.id), "decision": "approve"}
        )
        assert "comment is required" in tool_error(
            world.full, "review_post", {"post_id": str(post.id), "decision": "reject", "comment": " "}
        )
        assert "comment" in tool_error(
            world.full, "review_post", {"post_id": str(post.id), "decision": "request_changes"}
        )
        assert (
            tool_error(world.narrow, "review_post", {"post_id": str(outside.id), "decision": "approve"})
            == "Post not found"
        )
        assert (
            tool_error(world.full, "review_post", {"post_id": str(foreign.id), "decision": "approve"})
            == "Post not found"
        )
        draft = make_post(world.workspace, world.owner, [world.account1])
        assert "Nothing to update" in tool_error(
            world.full, "review_post", {"post_id": str(draft.id), "decision": "approve"}
        )
        assert "status" in tool_error(world.full, "list_approvals", {"status": "bogus"})
        assert "permission" in tool_error(world.bare, "list_approvals").lower()

    def test_pagination(self, world):
        for _ in range(3):
            self.pending(world)
        page = tool_result(world.full, "list_approvals", {"limit": 2})
        assert len(page["posts"]) == 2 and page["next_cursor"]
        assert len(tool_result(world.full, "list_approvals", {"limit": 2, "cursor": page["next_cursor"]})["posts"]) == 1
        assert "cursor" in tool_error(world.full, "list_approvals", {"cursor": "x"})

    def test_comments_respect_visibility(self, world):
        post = self.pending(world, status="pending_client")
        top = tool_result(
            world.full, "add_post_comment", {"post_id": str(post.id), "body": "team", "visibility": "internal"}
        )
        ext = tool_result(world.full, "add_post_comment", {"post_id": str(post.id), "body": "all"})
        tool_result(world.full, "add_post_comment", {"post_id": str(post.id), "body": "re", "parent_id": top["id"]})
        team = tool_result(world.full, "list_post_comments", {"post_id": str(post.id)})["comments"]
        assert names(team, "body") == ["team", "all"] and names(team[0]["replies"], "body") == ["re"]
        client_view = tool_result(world.client_role, "list_post_comments", {"post_id": str(post.id)})["comments"]
        assert names(client_view, "body") == ["all"]
        assert "external" in tool_error(
            world.client_role, "add_post_comment", {"post_id": str(post.id), "body": "x", "visibility": "internal"}
        )
        assert (
            tool_error(
                world.client_role, "add_post_comment", {"post_id": str(post.id), "body": "x", "parent_id": top["id"]}
            )
            == "Parent comment not found"
        )
        assert (
            tool_result(world.client_role, "add_post_comment", {"post_id": str(post.id), "body": "ok"})["visibility"]
            == "external"
        )
        assert ext["visibility"] == "external"

    def test_comment_scope_validation_and_isolation(self, world):
        post = self.pending(world)
        outside = self.pending(world, world.account2)
        assert "Permission denied" in tool_error(world.bare, "add_post_comment", {"post_id": str(post.id), "body": "x"})
        assert "Permission denied" in tool_error(world.bare, "list_post_comments", {"post_id": str(post.id)})
        assert tool_error(world.full, "add_post_comment", {"post_id": str(post.id), "body": ""})
        assert tool_error(
            world.full, "add_post_comment", {"post_id": str(post.id), "body": "x", "visibility": "secret"}
        )
        assert tool_error(world.narrow, "list_post_comments", {"post_id": str(outside.id)}) == "Post not found"
        assert (
            tool_error(world.full, "add_post_comment", {"post_id": str(post.id), "body": "x", "parent_id": NO_UUID})
            == "Parent comment not found"
        )


@pytest.mark.django_db
class TestQueueAndSlotTools:
    @pytest.fixture
    def queue(self, world):
        for day in range(7):
            PostingSlot.objects.create(social_account=world.account1, day_of_week=day, time=dt.time(10, 0))
        return Queue.objects.create(workspace=world.workspace, name="Main", social_account=world.account1)

    def test_queue_flow(self, world, queue):
        post = make_post(world.workspace, world.owner, [world.account1])
        listed = tool_result(world.full, "list_queues")["queues"]
        assert names(listed) == ["Main"] and listed[0]["next_free_slot"]
        entry = tool_result(world.full, "add_post_to_queue", {"queue_id": str(queue.id), "post_id": str(post.id)})
        assert entry["assigned_slot_datetime"] == listed[0]["next_free_slot"]
        detail = tool_result(world.full, "get_queue", {"queue_id": str(queue.id)})
        assert [e["post_id"] for e in detail["entries"]] == [str(post.id)]
        assert tool_result(world.full, "remove_from_queue", {"queue_id": str(queue.id), "entry_id": entry["id"]})[
            "deleted"
        ]
        assert tool_result(world.full, "get_queue", {"queue_id": str(queue.id)})["entries"] == []

    def test_queue_scope_allowlist_and_validation(self, world, queue):
        other_queue = Queue.objects.create(workspace=world.workspace, name="B", social_account=world.account2)
        foreign = Queue.objects.create(workspace=world.other_workspace, name="C", social_account=world.other_account)
        post = make_post(world.workspace, world.owner, [world.account1])
        wrong = make_post(world.workspace, world.owner, [world.account2])
        args = {"queue_id": str(queue.id), "post_id": str(post.id)}
        assert "Permission denied" in tool_error(world.bare, "add_post_to_queue", args)
        assert tool_error(world.narrow, "get_queue", {"queue_id": str(other_queue.id)}) == "Queue not found"
        assert tool_error(world.full, "get_queue", {"queue_id": str(foreign.id)}) == "Queue not found"
        assert names(tool_result(world.narrow, "list_queues")["queues"]) == ["Main"]
        assert "no platform target" in tool_error(world.full, "add_post_to_queue", {**args, "post_id": str(wrong.id)})
        assert tool_error(world.full, "add_post_to_queue", {**args, "post_id": NO_UUID}) == "Post not found"
        assert "required" in tool_error(world.full, "add_post_to_queue", {"queue_id": str(queue.id)})
        Queue.objects.filter(id=queue.id).update(is_active=False)
        assert "not active" in tool_error(world.full, "add_post_to_queue", args)

    def test_slot_crud(self, world):
        created = tool_result(
            world.full,
            "create_posting_slot",
            {"social_account_id": str(world.account1.id), "day_of_week": 3, "time": "08:45"},
        )
        assert created["day_name"] == "Thursday" and created["time"] == "08:45"
        assert "already exists" in tool_error(
            world.full,
            "create_posting_slot",
            {"social_account_id": str(world.account1.id), "day_of_week": 3, "time": "08:45"},
        )
        updated = tool_result(
            world.full, "update_posting_slot", {"slot_id": created["id"], "time": "09:15", "is_active": False}
        )
        assert updated["time"] == "09:15" and updated["is_active"] is False
        listed = tool_result(world.full, "list_posting_slots", {"social_account_id": str(world.account1.id)})["slots"]
        assert [s["id"] for s in listed] == [created["id"]]
        assert tool_result(world.full, "delete_posting_slot", {"slot_id": created["id"]})["deleted"]
        assert tool_result(world.full, "list_posting_slots")["slots"] == []

    def test_slot_scope_allowlist_and_validation(self, world):
        slot = PostingSlot.objects.create(social_account=world.account2, day_of_week=0, time=dt.time(7, 0))
        theirs = PostingSlot.objects.create(social_account=world.other_account, day_of_week=0, time=dt.time(7, 0))
        args = {"social_account_id": str(world.account1.id), "day_of_week": 1, "time": "08:00"}
        assert "manage_social_accounts" in tool_error(world.bare, "create_posting_slot", args)
        assert "manage_social_accounts" in tool_error(world.bare, "delete_posting_slot", {"slot_id": str(slot.id)})
        assert "allowlist" in tool_error(
            world.narrow, "create_posting_slot", {**args, "social_account_id": str(world.account2.id)}
        )
        assert (
            tool_error(world.narrow, "update_posting_slot", {"slot_id": str(slot.id), "is_active": False})
            == "Posting slot not found"
        )
        assert tool_error(world.full, "delete_posting_slot", {"slot_id": str(theirs.id)}) == "Posting slot not found"
        assert "HH:MM" in tool_error(world.full, "create_posting_slot", {**args, "time": "noonish"})
        assert tool_error(world.full, "create_posting_slot", {**args, "day_of_week": 9})
        assert "allowlist" in tool_error(
            world.narrow, "list_posting_slots", {"social_account_id": str(world.account2.id)}
        )
        assert {s["social_account_id"] for s in tool_result(world.narrow, "list_posting_slots")["slots"]} == set()

    def test_find_next_free_slot(self, world, queue):
        slots = tool_result(
            world.full, "find_next_free_slot", {"social_account_id": str(world.account1.id), "count": 3}
        )
        assert len(slots["slots"]) == 3 and slots["slots"] == sorted(slots["slots"])
        assert (
            tool_result(world.full, "find_next_free_slot", {"social_account_id": str(world.account2.id)})["slots"] == []
        )
        assert "allowlist" in tool_error(
            world.narrow, "find_next_free_slot", {"social_account_id": str(world.account2.id)}
        )
        assert "allowlist" in tool_error(
            world.full, "find_next_free_slot", {"social_account_id": str(world.other_account.id)}
        )
        assert tool_error(world.full, "find_next_free_slot", {"social_account_id": str(world.account1.id), "count": 99})
        assert "required" in tool_error(world.full, "find_next_free_slot", {})


@pytest.mark.django_db
class TestIdeaTools:
    def test_kanban_flow(self, world):
        col = tool_result(world.full, "create_idea_group", {"name": "Backlog"})
        other = tool_result(world.full, "create_idea_group", {"name": "Doing"})
        idea = tool_result(world.full, "create_idea", {"title": "Reel", "tags": ["video"], "group_id": col["id"]})
        moved = tool_result(
            world.full,
            "update_idea",
            {"idea_id": idea["id"], "group_id": other["id"], "position": 2, "title": "Reel v2"},
        )
        assert moved["group_id"] == other["id"] and moved["title"] == "Reel v2"
        assert tool_result(world.full, "get_idea", {"idea_id": idea["id"]})["position"] == 2
        counts = {g["name"]: g["idea_count"] for g in tool_result(world.full, "list_idea_groups")["groups"]}
        assert counts == {"Backlog": 0, "Doing": 1}
        assert "empty" in tool_error(world.full, "delete_idea_group", {"group_id": other["id"]})
        assert tool_result(world.full, "delete_idea", {"idea_id": idea["id"]})["deleted"]
        assert tool_result(world.full, "delete_idea_group", {"group_id": other["id"]})["deleted"]

    def test_scope_validation_isolation_and_pagination(self, world):
        theirs = Idea.objects.create(workspace=world.other_workspace, title="theirs")
        theirs_group = IdeaGroup.objects.create(workspace=world.other_workspace, name="g")
        assert "create_posts" in tool_error(world.bare, "create_idea", {"title": "x"})
        assert "create_posts" in tool_error(world.bare, "list_ideas")
        assert tool_error(world.full, "get_idea", {"idea_id": str(theirs.id)}) == "Idea not found"
        assert tool_error(world.full, "update_idea", {"idea_id": str(theirs.id), "title": "x"}) == "Idea not found"
        assert tool_error(world.full, "delete_idea", {"idea_id": str(theirs.id)}) == "Idea not found"
        assert tool_error(world.full, "delete_idea_group", {"group_id": str(theirs_group.id)}) == "Idea group not found"
        assert (
            tool_error(world.full, "create_idea", {"title": "x", "group_id": str(theirs_group.id)})
            == "Idea group not found"
        )
        assert tool_error(world.full, "create_idea", {"title": "  "})
        assert tool_error(world.full, "create_idea", {"title": "x", "tags": ["y" * 101]})
        for i in range(3):
            Idea.objects.create(workspace=world.workspace, title=f"i{i}", position=i)
        page = tool_result(world.full, "list_ideas", {"limit": 2})
        assert names(page["ideas"], "title") == ["i0", "i1"] and page["next_cursor"]
        assert names(
            tool_result(world.full, "list_ideas", {"limit": 2, "cursor": page["next_cursor"]})["ideas"], "title"
        ) == ["i2"]
        assert names(tool_result(world.full, "list_ideas", {"search": "i1"})["ideas"], "title") == ["i1"]
        assert "cursor" in tool_error(world.full, "list_ideas", {"cursor": "bad"})


@pytest.mark.django_db
class TestTemplateTagCategoryTools:
    def test_template_flow_and_allowlist(self, world):
        created = tool_result(
            world.full,
            "create_template",
            {
                "name": "Weekly",
                "caption": "Hi",
                "platform_account_ids": [str(world.account1.id), str(world.account2.id)],
            },
        )
        assert created["caption"] == "Hi"
        assert len(tool_result(world.full, "get_template", {"template_id": created["id"]})["platform_account_ids"]) == 2
        # Aimed at an account outside the narrow key's allowlist: hidden from it, like a post.
        assert tool_error(world.narrow, "get_template", {"template_id": created["id"]}) == "Template not found"
        updated = tool_result(
            world.full, "update_template", {"template_id": created["id"], "name": "Weekly 2", "tags": ["t"]}
        )
        assert updated["name"] == "Weekly 2" and updated["tags"] == ["t"]
        assert names(tool_result(world.full, "list_templates", {"search": "weekly"})["templates"]) == ["Weekly 2"]
        assert tool_result(world.full, "delete_template", {"template_id": created["id"]})["deleted"]

    def test_template_scope_validation_isolation_pagination(self, world):
        theirs = PostTemplate.objects.create(workspace=world.other_workspace, name="theirs")
        assert "create_posts" in tool_error(world.bare, "create_template", {"name": "x"})
        assert "allowlist" in tool_error(
            world.narrow, "create_template", {"name": "x", "platform_account_ids": [str(world.account2.id)]}
        )
        assert tool_error(world.full, "get_template", {"template_id": str(theirs.id)}) == "Template not found"
        assert (
            tool_error(world.full, "update_template", {"template_id": str(theirs.id), "name": "x"})
            == "Template not found"
        )
        assert tool_error(world.full, "delete_template", {"template_id": str(theirs.id)}) == "Template not found"
        assert "category_id" in tool_error(world.full, "create_template", {"name": "x", "category_id": NO_UUID})
        assert "Media asset" in tool_error(world.full, "create_template", {"name": "x", "media_asset_ids": [NO_UUID]})
        assert tool_error(world.full, "create_template", {"name": ""})
        for n in "abc":
            PostTemplate.objects.create(workspace=world.workspace, name=n)
        page = tool_result(world.full, "list_templates", {"limit": 2})
        assert names(page["templates"]) == ["a", "b"] and page["next_cursor"]
        assert names(
            tool_result(world.full, "list_templates", {"limit": 2, "cursor": page["next_cursor"]})["templates"]
        ) == ["c"]

    def test_tags(self, world):
        tag = tool_result(world.full, "create_tag", {"name": "launch"})
        assert tool_result(world.full, "create_tag", {"name": "launch"})["id"] == tag["id"]
        post = make_post(world.workspace, world.owner, [world.account1], tags=["launch"])
        assert tool_result(world.full, "rename_tag", {"tag_id": tag["id"], "name": "release"})["name"] == "release"
        post.refresh_from_db()
        assert post.tags == ["release"]
        Tag.objects.create(workspace=world.workspace, name="other")
        assert "exists" in tool_error(world.full, "rename_tag", {"tag_id": tag["id"], "name": "other"})
        assert names(tool_result(world.full, "list_tags", {"search": "rel"})["tags"]) == ["release"]
        assert tool_result(world.full, "delete_tag", {"tag_id": tag["id"]})["deleted"]
        post.refresh_from_db()
        assert post.tags == []

    def test_tag_scope_validation_isolation_pagination(self, world):
        theirs = Tag.objects.create(workspace=world.other_workspace, name="theirs")
        assert "create_posts" in tool_error(world.bare, "create_tag", {"name": "x"})
        assert tool_error(world.full, "rename_tag", {"tag_id": str(theirs.id), "name": "x"}) == "Tag not found"
        assert tool_error(world.full, "delete_tag", {"tag_id": str(theirs.id)}) == "Tag not found"
        assert tool_error(world.full, "create_tag", {"name": "  "})
        for n in ("a", "b", "c"):
            Tag.objects.create(workspace=world.workspace, name=n)
        page = tool_result(world.full, "list_tags", {"limit": 2})
        assert names(page["tags"]) == ["a", "b"] and page["next_cursor"]
        assert names(tool_result(world.full, "list_tags", {"limit": 2, "cursor": page["next_cursor"]})["tags"]) == ["c"]

    def test_categories(self, world):
        category = tool_result(world.full, "create_category", {"name": "Edu", "color": "#112233"})
        assert (
            tool_result(
                world.full, "update_category", {"category_id": category["id"], "name": "Education", "position": 4}
            )["position"]
            == 4
        )
        assert names(tool_result(world.full, "list_categories")["categories"]) == ["Education"]
        assert "exists" in tool_error(world.full, "create_category", {"name": "Education"})
        assert "color" in tool_error(world.full, "create_category", {"name": "x", "color": "red"})
        assert "create_posts" in tool_error(world.bare, "create_category", {"name": "x"})
        theirs = ContentCategory.objects.create(workspace=world.other_workspace, name="theirs")
        assert (
            tool_error(world.full, "update_category", {"category_id": str(theirs.id), "name": "x"})
            == "Category not found"
        )
        assert tool_error(world.full, "delete_category", {"category_id": str(theirs.id)}) == "Category not found"
        assert tool_result(world.full, "delete_category", {"category_id": category["id"]})["deleted"]
        assert tool_result(world.full, "list_categories")["categories"] == []


@pytest.mark.django_db
class TestEventBrandVoiceUsageTools:
    def test_calendar_events(self, world):
        a = tool_result(
            world.full,
            "create_calendar_event",
            {"title": "Launch", "start_date": "2026-03-01", "end_date": "2026-03-03"},
        )
        tool_result(world.full, "create_calendar_event", {"title": "Later", "start_date": "2026-06-01"})
        assert names(tool_result(world.full, "list_calendar_events")["events"], "title") == ["Launch", "Later"]
        assert names(
            tool_result(world.full, "list_calendar_events", {"date_from": "2026-03-02", "date_to": "2026-03-10"})[
                "events"
            ],
            "title",
        ) == ["Launch"]
        page = tool_result(world.full, "list_calendar_events", {"limit": 1})
        assert len(page["events"]) == 1 and page["next_cursor"]
        assert names(
            tool_result(world.full, "list_calendar_events", {"limit": 1, "cursor": page["next_cursor"]})["events"],
            "title",
        ) == ["Later"]
        assert tool_result(world.full, "delete_calendar_event", {"event_id": a["id"]})["deleted"]

    def test_calendar_event_scope_validation_isolation(self, world):
        from apps.calendar.models import CustomCalendarEvent

        theirs = CustomCalendarEvent.objects.create(
            workspace=world.other_workspace, title="t", start_date="2026-01-01", end_date="2026-01-01"
        )
        assert "create_posts" in tool_error(
            world.bare, "create_calendar_event", {"title": "x", "start_date": "2026-01-01"}
        )
        assert "create_posts" in tool_error(world.bare, "delete_calendar_event", {"event_id": str(theirs.id)})
        assert tool_error(world.full, "delete_calendar_event", {"event_id": str(theirs.id)}) == "Event not found"
        assert "YYYY-MM-DD" in tool_error(world.full, "create_calendar_event", {"title": "x", "start_date": "soon"})
        assert "end_date" in tool_error(
            world.full, "create_calendar_event", {"title": "x", "start_date": "2026-02-02", "end_date": "2026-02-01"}
        )
        assert "color" in tool_error(
            world.full, "create_calendar_event", {"title": "x", "start_date": "2026-02-02", "color": "blue"}
        )
        assert tool_result(world.full, "list_calendar_events")["events"] == []
        assert "cursor" in tool_error(world.full, "list_calendar_events", {"cursor": "bad"})

    def test_brand_voice(self, world):
        assert tool_result(world.full, "get_brand_voice")["tone"] == ""
        updated = tool_result(world.full, "update_brand_voice", {"tone": "Warm", "examples": ["A", "B"]})
        assert updated["tone"] == "Warm" and updated["examples"] == ["A", "B"]
        assert tool_result(world.full, "update_brand_voice", {"words_to_use": "plain"})["tone"] == "Warm"
        assert "manage_workspace_settings" in tool_error(world.bare, "update_brand_voice", {"tone": "x"})
        assert "Permission denied" in tool_error(world.bare, "get_brand_voice")
        assert "too long" in tool_error(world.full, "update_brand_voice", {"words_to_use": "x" * 1001})
        assert tool_error(world.full, "update_brand_voice", {"examples": ["a", "b", "c", "d"]})
        assert tool_result(world.other, "get_brand_voice")["tone"] == ""

    def test_usage_and_read_only_lists(self, world):
        usage = tool_result(world.bare, "get_usage")
        assert {m["key"] for m in usage["usage"]} == {"channels", "workspaces", "posts", "api_calls", "ai_requests"}
        from apps.composer.models import HashtagSet, Signature

        HashtagSet.objects.create(workspace=world.workspace, name="Coffee", hashtags=["#coffee"])
        HashtagSet.objects.create(workspace=world.other_workspace, name="Theirs", hashtags=["#x"])
        Signature.objects.create(workspace=world.workspace, name="House", body="Hi")
        assert names(tool_result(world.full, "list_hashtag_sets")["hashtag_sets"]) == ["Coffee"]
        assert names(tool_result(world.full, "list_signatures")["signatures"]) == ["House"]
        assert call_tool(world.full, "get_usage")["result"]["isError"] is False
