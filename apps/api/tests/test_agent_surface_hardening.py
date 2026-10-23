"""Retries, scope edges and freshness on the agent surface (REST + MCP)."""

from __future__ import annotations

import json

import pytest

from apps.api.tests.world import make_post, tool_error, tool_result
from apps.approvals import comments as comment_service
from apps.approvals.models import PostComment
from apps.composer.models import Idea, Post, Tag
from apps.members.models import WorkspaceMembership


def send(client, method, url, payload=None, **headers):
    return getattr(client, method)(url, data=json.dumps(payload or {}), content_type="application/json", **headers)


@pytest.mark.django_db
class TestIdempotency:
    def test_rest_create_replays_with_the_same_key(self, world):
        first = send(world.full, "post", "/api/v1/ideas/", {"title": "Launch"}, HTTP_IDEMPOTENCY_KEY="k-1")
        again = send(world.full, "post", "/api/v1/ideas/", {"title": "Launch"}, HTTP_IDEMPOTENCY_KEY="k-1")
        assert first.status_code == again.status_code == 201
        assert first.json()["id"] == again.json()["id"]
        assert Idea.objects.filter(workspace=world.workspace).count() == 1

    def test_rest_key_reused_with_another_body_is_refused(self, world):
        send(world.full, "post", "/api/v1/ideas/", {"title": "A"}, HTTP_IDEMPOTENCY_KEY="k-2")
        reused = send(world.full, "post", "/api/v1/ideas/", {"title": "B"}, HTTP_IDEMPOTENCY_KEY="k-2")
        assert reused.status_code == 422

    def test_rest_refusal_releases_the_key(self, world):
        bad = send(world.full, "post", "/api/v1/ideas/", {"title": " "}, HTTP_IDEMPOTENCY_KEY="k-3")
        assert bad.status_code == 422
        # Same key, same (still bad) body: re-run rather than a stuck "in flight".
        assert send(world.full, "post", "/api/v1/ideas/", {"title": " "}, HTTP_IDEMPOTENCY_KEY="k-3").status_code == 422

    def test_without_a_key_each_call_creates(self, world):
        send(world.full, "post", "/api/v1/ideas/", {"title": "Launch"})
        send(world.full, "post", "/api/v1/ideas/", {"title": "Launch"})
        assert Idea.objects.filter(workspace=world.workspace).count() == 2

    def test_mcp_create_tool_replays(self, world):
        args = {"title": "Launch", "idempotency_key": "m-1"}
        first = tool_result(world.full, "create_idea", args)
        again = tool_result(world.full, "create_idea", args)
        assert first["id"] == again["id"]
        assert Idea.objects.filter(workspace=world.workspace).count() == 1
        assert "different" in tool_error(world.full, "create_idea", {"title": "Other", "idempotency_key": "m-1"})

    def test_mcp_create_draft_replays(self, world):
        args = {"social_account_id": str(world.account1.id), "caption": "hi", "idempotency_key": "m-2"}
        assert (
            tool_result(world.full, "create_draft", args)["id"] == tool_result(world.full, "create_draft", args)["id"]
        )
        assert Post.objects.filter(workspace=world.workspace).count() == 1


@pytest.mark.django_db
class TestWorkspaceFreshness:
    def test_settings_changed_in_the_web_app_apply_on_the_next_call(self, world):
        assert world.full.get("/api/v1/brand-voice/").status_code == 200  # caches the key row
        type(world.workspace).objects.filter(pk=world.workspace.pk).update(brand_voice_tone="Dry and precise")
        assert world.full.get("/api/v1/brand-voice/").json()["tone"] == "Dry and precise"


@pytest.mark.django_db
class TestTagScope:
    def test_tag_on_posts_outside_the_allowlist_cannot_be_rewritten(self, world):
        tag = Tag.objects.create(workspace=world.workspace, name="launch")
        hidden = make_post(world.workspace, world.owner, [world.account2], tags=["launch"])
        renamed = send(world.narrow, "patch", f"/api/v1/tags/{tag.id}", {"name": "go"})
        assert renamed.status_code == 403
        assert send(world.narrow, "delete", f"/api/v1/tags/{tag.id}").status_code == 403
        hidden.refresh_from_db()
        assert hidden.tags == ["launch"]
        # A key that sees every post carrying it can.
        assert send(world.full, "patch", f"/api/v1/tags/{tag.id}", {"name": "go"}).status_code == 200
        hidden.refresh_from_db()
        assert hidden.tags == ["go"]


@pytest.mark.django_db
class TestCommentVisibility:
    def test_clients_do_not_see_internal_replies(self, world):
        post = make_post(world.workspace, world.owner, [world.account1], status="pending_review")
        top = PostComment.objects.create(post=post, author=world.owner, body="for the client", visibility="external")
        PostComment.objects.create(
            post=post, author=world.owner, body="team only", visibility="internal", parent_comment=top
        )
        client_view = list(comment_service.get_comments_for_post(post, world.client_user))
        assert [r.body for r in client_view[0].replies.all()] == []
        team_view = list(comment_service.get_comments_for_post(post, world.owner))
        assert [r.body for r in team_view[0].replies.all()] == ["team only"]


@pytest.mark.django_db
class TestUpdatePostTool:
    def test_edits_a_draft_like_patch(self, world):
        post = make_post(world.workspace, world.owner, [world.account1])
        result = tool_result(
            world.full, "update_post", {"post_id": str(post.id), "caption": "new words", "tags": ["launch"]}
        )
        assert result["caption"] == "new words" and result["tags"] == ["launch"]
        assert Tag.objects.filter(workspace=world.workspace, name="launch").exists()

    def test_same_refusals_as_patch(self, world):
        scheduled = make_post(world.workspace, world.owner, [world.account1], status="scheduled")
        published = make_post(world.workspace, world.owner, [world.account1], status="published")
        outside = make_post(world.workspace, world.owner, [world.account2])
        assert "create_posts" in tool_error(world.bare, "update_post", {"post_id": str(scheduled.id), "caption": "x"})
        assert "not editable" in tool_error(world.full, "update_post", {"post_id": str(published.id), "caption": "x"})
        assert tool_error(world.narrow, "update_post", {"post_id": str(outside.id), "caption": "x"}) == "Post not found"
        assert "scheduled_at" in tool_error(
            world.full, "update_post", {"post_id": str(scheduled.id), "scheduled_at": "soon"}
        )


@pytest.mark.django_db
class TestMembers:
    def test_rest_and_mcp_list_the_team_without_emails(self, world):
        rest = world.full.get("/api/v1/members/")
        assert rest.status_code == 200
        members = rest.json()["members"]
        assert {m["workspace_role"] for m in members} == {"owner", "client"}
        assert "email" not in json.dumps(members)
        client = next(m for m in members if m["workspace_role"] == WorkspaceMembership.WorkspaceRole.CLIENT)
        assert client["mention"] == "@cleoclient"
        assert tool_result(world.full, "list_members") == rest.json()
        assert world.other.get("/api/v1/members/").json()["members"][0]["user_id"] == str(world.other_owner.id)
