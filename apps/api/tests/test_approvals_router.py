"""``/api/v1/approvals/*`` — review queue, decisions, comments."""

from __future__ import annotations

import pytest

from apps.api.tests.world import make_post
from apps.approvals.models import ApprovalAction, PostComment
from apps.composer.models import Post

URL = "/api/v1/approvals/"


def pending(world, *accounts, status="pending_review"):
    return make_post(world.workspace, world.owner, list(accounts) or [world.account1], status=status)


def post_json(client, url, payload=None):
    return client.post(url, data=payload or {}, content_type="application/json")


def statuses(post):
    return sorted(Post.objects.get(id=post.id).platform_posts.values_list("status", flat=True))


@pytest.mark.django_db
class TestListApprovals:
    def test_requires_a_key(self, world):
        assert world.anonymous.get(URL).status_code == 401

    def test_needs_approve_or_create_posts(self, world):
        assert world.bare.get(URL).status_code == 403

    def test_defaults_to_pending_and_filters_by_status(self, world):
        waiting = pending(world)
        client_stage = pending(world, status="pending_client")
        pending(world, status="approved")
        pending(world, status="draft")
        body = world.full.get(URL).json()
        assert {p["id"] for p in body["posts"]} == {str(waiting.id), str(client_stage.id)}
        only = world.full.get(URL + "?status=approved").json()["posts"]
        assert [p["status"] for p in only] == ["approved"]
        assert len(world.full.get(URL + "?status=all").json()["posts"]) == 3

    def test_allowlist_and_workspace_isolation(self, world):
        mine = pending(world)
        pending(world, world.account2)
        make_post(world.other_workspace, world.other_owner, [world.other_account], status="pending_review")
        assert [p["id"] for p in world.narrow.get(URL).json()["posts"]] == [str(mine.id)]
        assert len(world.full.get(URL).json()["posts"]) == 2
        assert world.narrow.get(URL + f"?social_account_id={world.account2.id}").status_code == 403

    def test_validation_and_pagination(self, world):
        for _ in range(3):
            pending(world)
        assert world.full.get(URL + "?status=bogus").status_code == 422
        assert world.full.get(URL + "?cursor=zzz").status_code == 422
        first = world.full.get(URL + "?limit=2").json()
        assert len(first["posts"]) == 2 and first["next_cursor"]
        second = world.full.get(URL + f"?limit=2&cursor={first['next_cursor']}").json()
        assert len(second["posts"]) == 1 and second["next_cursor"] is None


@pytest.mark.django_db
class TestDecisions:
    def test_requires_a_key(self, world):
        post = pending(world)
        assert post_json(world.anonymous, f"{URL}{post.id}/approve").status_code == 401

    @pytest.mark.parametrize("action", ["approve", "request-changes", "reject"])
    def test_needs_approve_posts(self, world, action):
        post = pending(world)
        assert post_json(world.bare, f"{URL}{post.id}/{action}", {"comment": "x"}).status_code == 403
        assert statuses(post) == ["pending_review"]

    def test_approve_moves_the_post_and_records_the_action(self, world):
        post = pending(world)
        r = post_json(world.full, f"{URL}{post.id}/approve", {"comment": "Looks good"})
        assert r.status_code == 200, r.content
        assert r.json()["status"] == "approved"
        action = ApprovalAction.objects.get(post=post)
        assert action.action == "approved" and action.comment == "Looks good" and action.user == world.owner
        assert action.channel == "api" and len(action.fingerprint) == 64

    def test_request_changes_and_reject_need_a_comment(self, world):
        post = pending(world)
        assert post_json(world.full, f"{URL}{post.id}/request-changes").status_code == 422
        assert post_json(world.full, f"{URL}{post.id}/reject", {"comment": "   "}).status_code == 422
        assert statuses(post) == ["pending_review"]
        r = post_json(world.full, f"{URL}{post.id}/request-changes", {"comment": "Shorter please"})
        assert r.status_code == 200 and r.json()["status"] == "changes_requested"
        other = pending(world)
        r = post_json(world.full, f"{URL}{other.id}/reject", {"comment": "Off brand"})
        assert r.status_code == 200 and r.json()["status"] == "rejected"

    def test_nothing_waiting_is_409(self, world):
        post = pending(world, status="draft")
        assert post_json(world.full, f"{URL}{post.id}/approve").status_code == 409

    def test_allowlist_and_workspace_isolation(self, world):
        outside = pending(world, world.account2)
        foreign = make_post(world.other_workspace, world.other_owner, [world.other_account], status="pending_review")
        assert post_json(world.narrow, f"{URL}{outside.id}/approve").status_code == 404
        assert post_json(world.full, f"{URL}{foreign.id}/approve").status_code == 404
        assert statuses(outside) == ["pending_review"]

    def test_issuer_without_approve_posts_cannot_decide_even_with_a_broad_key(self, world):
        from apps.members.models import WorkspaceMembership

        post = pending(world)
        WorkspaceMembership.objects.filter(user=world.owner, workspace=world.workspace).update(
            workspace_role=WorkspaceMembership.WorkspaceRole.EDITOR
        )
        # The key still lists approve_posts but its issuer no longer holds it.
        assert post_json(world.full, f"{URL}{post.id}/approve").status_code == 403
        assert statuses(post) == ["pending_review"]


@pytest.mark.django_db
class TestComments:
    def test_requires_a_key(self, world):
        post = pending(world)
        assert world.anonymous.get(f"{URL}{post.id}/comments").status_code == 401
        assert post_json(world.anonymous, f"{URL}{post.id}/comments", {"body": "x"}).status_code == 401

    def test_needs_a_comment_capable_permission(self, world):
        post = pending(world)
        assert world.bare.get(f"{URL}{post.id}/comments").status_code == 403
        assert post_json(world.bare, f"{URL}{post.id}/comments", {"body": "x"}).status_code == 403

    def test_add_and_list_with_replies(self, world):
        post = pending(world)
        top = post_json(world.full, f"{URL}{post.id}/comments", {"body": "First", "visibility": "internal"})
        assert top.status_code == 201, top.content
        assert top.json()["visibility"] == "internal" and top.json()["author_id"] == str(world.owner.id)
        reply = post_json(world.full, f"{URL}{post.id}/comments", {"body": "Reply", "parent_id": top.json()["id"]})
        assert reply.status_code == 201 and reply.json()["parent_id"] == top.json()["id"]
        listed = world.full.get(f"{URL}{post.id}/comments").json()["comments"]
        assert [c["body"] for c in listed] == ["First"]
        assert [r["body"] for r in listed[0]["replies"]] == ["Reply"]

    def test_validation(self, world):
        post = pending(world)
        assert post_json(world.full, f"{URL}{post.id}/comments", {"body": ""}).status_code == 422
        assert (
            post_json(world.full, f"{URL}{post.id}/comments", {"body": "x", "visibility": "secret"}).status_code == 422
        )
        missing = "00000000-0000-4000-8000-000000000000"
        assert post_json(world.full, f"{URL}{post.id}/comments", {"body": "x", "parent_id": missing}).status_code == 404

    def test_clients_only_see_and_write_external_comments(self, world):
        post = pending(world, status="pending_client")
        PostComment.objects.create(post=post, author=world.owner, body="team only", visibility="internal")
        PostComment.objects.create(post=post, author=world.owner, body="for client", visibility="external")
        listed = world.client_role.get(f"{URL}{post.id}/comments").json()["comments"]
        assert [c["body"] for c in listed] == ["for client"]
        assert [c["body"] for c in world.full.get(f"{URL}{post.id}/comments").json()["comments"]] == [
            "team only",
            "for client",
        ]
        denied = post_json(world.client_role, f"{URL}{post.id}/comments", {"body": "x", "visibility": "internal"})
        assert denied.status_code == 403
        ok = post_json(world.client_role, f"{URL}{post.id}/comments", {"body": "Approved by me"})
        assert ok.status_code == 201 and ok.json()["visibility"] == "external"

    def test_client_cannot_reply_to_an_internal_comment(self, world):
        post = pending(world, status="pending_client")
        hidden = PostComment.objects.create(post=post, author=world.owner, body="hidden", visibility="internal")
        r = post_json(world.client_role, f"{URL}{post.id}/comments", {"body": "x", "parent_id": str(hidden.id)})
        assert r.status_code == 404

    def test_allowlist_and_workspace_isolation(self, world):
        outside = pending(world, world.account2)
        foreign = make_post(world.other_workspace, world.other_owner, [world.other_account], status="pending_review")
        for post in (outside, foreign):
            client = world.narrow if post is outside else world.full
            assert client.get(f"{URL}{post.id}/comments").status_code == 404
            assert post_json(client, f"{URL}{post.id}/comments", {"body": "x"}).status_code == 404
        assert not PostComment.objects.exists()

    def test_client_signs_off_only_at_the_client_stage(self, world):
        world.workspace.approval_workflow_mode = "required_internal_and_client"
        world.workspace.save(update_fields=["approval_workflow_mode"])
        internal_stage = pending(world)
        client_stage = pending(world, status="pending_client")
        assert post_json(world.client_role, f"{URL}{internal_stage.id}/approve").status_code == 409
        r = post_json(world.client_role, f"{URL}{client_stage.id}/approve")
        assert r.status_code == 200, r.content
        assert r.json()["status"] == "approved"
