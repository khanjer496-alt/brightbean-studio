"""Team notes over REST and MCP, and comment-only reviewers over the same surfaces."""

from __future__ import annotations

import uuid

import pytest
from django.utils import timezone

from apps.accounts.models import User
from apps.api.tests.world import bearer, call_tool, make_post, tool_error, tool_result
from apps.api_keys import services
from apps.api_keys.models import ApiKey
from apps.approvals.models import PostComment, PostNote
from apps.autonomy.models import ActionType, AgentAction
from apps.members.models import CustomRole, WorkspaceMembership
from apps.notifications.models import EventType, Notification

SECRET = "SECRET-NOTE-BODY"


def notes_url(post):
    return f"/api/v1/posts/{post.id}/notes"


def post_json(client, url, payload=None):
    return client.post(url, data=payload or {}, content_type="application/json")


def key_for(world, user, permissions, name):
    """A key that acts as ``user`` (issued by the owner, then handed over, as the world's client key is)."""
    issued = services.issue_api_key(
        workspace=world.workspace,
        social_accounts=[world.account1, world.account2],
        issued_by=world.owner,
        name=name,
        permissions=permissions,
    )
    ApiKey.objects.filter(pk=issued.api_key.pk).update(issued_by=user)
    return bearer(issued.plaintext_token)


def comment_only_client(world):
    role = CustomRole.objects.create(
        organization=world.workspace.organization, name="Comment only", permissions={"comment_on_posts": True}
    )
    user = User.objects.create_user(
        email=f"commenter-{uuid.uuid4().hex[:6]}@example.com", password="x", name="Cam", tos_accepted_at=timezone.now()
    )
    WorkspaceMembership.objects.create(user=user, workspace=world.workspace, workspace_role="viewer", custom_role=role)
    return user, key_for(world, user, ["comment_on_posts"], "comment-only")


@pytest.mark.django_db
class TestNotesRest:
    def test_requires_a_key(self, world):
        post = make_post(world.workspace, world.owner, [world.account1])
        assert world.anonymous.get(notes_url(post)).status_code == 401

    def test_add_then_list(self, world):
        post = make_post(world.workspace, world.owner, [world.account1])
        r = post_json(world.full, notes_url(post), {"body": "Needs the new logo"})
        assert r.status_code == 201, r.content
        assert r.json()["body"] == "Needs the new logo"
        assert r.json()["author_id"] == str(world.owner.id)
        listed = world.full.get(notes_url(post)).json()["notes"]
        assert [n["body"] for n in listed] == ["Needs the new logo"]

    def test_needs_create_posts(self, world):
        post = make_post(world.workspace, world.owner, [world.account1])
        assert world.bare.get(notes_url(post)).status_code == 403
        assert post_json(world.bare, notes_url(post), {"body": "x"}).status_code == 403

    def test_clients_get_nothing_and_write_nothing(self, world):
        post = make_post(world.workspace, world.owner, [world.account1])
        PostNote.objects.create(post=post, author=world.owner, body=SECRET)
        r = world.client_role.get(notes_url(post))
        assert r.status_code == 403 and SECRET not in r.content.decode()
        assert post_json(world.client_role, notes_url(post), {"body": "x"}).status_code == 403
        assert PostNote.objects.count() == 1

    def test_a_comment_only_reviewer_has_no_notes(self, world):
        post = make_post(world.workspace, world.owner, [world.account1])
        _, client = comment_only_client(world)
        assert client.get(notes_url(post)).status_code == 403

    def test_a_drafter_whose_role_has_no_notes_is_refused(self, world):
        role = CustomRole.objects.create(
            organization=world.workspace.organization,
            name="Drafts only",
            permissions={"create_posts": True, "use_internal_notes": False},
        )
        user = User.objects.create_user(
            email="drafter@example.com", password="x", name="Dee", tos_accepted_at=timezone.now()
        )
        WorkspaceMembership.objects.create(
            user=user, workspace=world.workspace, workspace_role="contributor", custom_role=role
        )
        client = key_for(world, user, ["create_posts"], "drafter")
        post = make_post(world.workspace, world.owner, [world.account1])
        r = client.get(notes_url(post))
        assert r.status_code == 403 and "use_internal_notes" in r.content.decode()

    def test_workspace_and_allowlist_isolation(self, world):
        mine = make_post(world.workspace, world.owner, [world.account2])
        theirs = make_post(world.other_workspace, world.other_owner, [world.other_account])
        assert world.narrow.get(notes_url(mine)).status_code == 404  # account 2 is outside the narrow key
        assert world.full.get(notes_url(theirs)).status_code == 404
        assert post_json(world.full, notes_url(theirs), {"body": "x"}).status_code == 404

    def test_validation(self, world):
        post = make_post(world.workspace, world.owner, [world.account1])
        assert post_json(world.full, notes_url(post), {"body": ""}).status_code == 422
        assert post_json(world.full, notes_url(post), {"body": "x" * 5001}).status_code == 422
        assert post_json(world.full, notes_url(post), {"body": "   "}).status_code == 422

    def test_a_mention_notifies_the_teammate(self, world):
        post = make_post(world.workspace, world.owner, [world.account1])
        mate = User.objects.create_user(
            email="mate@example.com", password="x", name="Mia Mate", tos_accepted_at=timezone.now()
        )
        WorkspaceMembership.objects.create(user=mate, workspace=world.workspace, workspace_role="editor")
        post_json(world.full, notes_url(post), {"body": "@miamate please check"})
        assert Notification.objects.filter(user=mate, event_type=EventType.COMMENT_MENTION).count() == 1

    def test_an_agents_note_is_recorded_in_the_autonomy_trail(self, world):
        post = make_post(world.workspace, world.owner, [world.account1])
        post_json(world.full, notes_url(post), {"body": "Drafted from the brief"})
        entry = AgentAction.objects.get(operation="note.create")
        assert entry.actor_type == "agent"
        assert entry.api_key is not None
        assert entry.post_id == post.id
        assert entry.action_type == ActionType.APPROVAL
        assert entry.decision == "executed"

    def test_a_note_never_reaches_a_client_through_the_autonomy_feed(self, world):
        from apps.autonomy.service import client_visible_actions

        post = make_post(world.workspace, world.owner, [world.account1])
        post_json(world.full, notes_url(post), {"body": SECRET})
        assert not any(SECRET in (a.final_text or "") for a in client_visible_actions(world.workspace))

    def test_an_idempotency_key_replays_the_first_note(self, world):
        post = make_post(world.workspace, world.owner, [world.account1])
        headers = {"HTTP_IDEMPOTENCY_KEY": "note-1"}
        first = world.full.post(
            notes_url(post), data={"body": "once"}, content_type="application/json", **headers
        ).json()
        second = world.full.post(
            notes_url(post), data={"body": "once"}, content_type="application/json", **headers
        ).json()
        assert first["id"] == second["id"]
        assert PostNote.objects.count() == 1

    def test_notes_do_not_appear_in_post_responses(self, world):
        post = make_post(world.workspace, world.owner, [world.account1])
        PostNote.objects.create(post=post, author=world.owner, body=SECRET)
        assert SECRET not in world.full.get(f"/api/v1/posts/{post.id}").content.decode()


@pytest.mark.django_db
class TestNotesMcp:
    def test_add_and_list(self, world):
        post = make_post(world.workspace, world.owner, [world.account1])
        added = tool_result(world.full, "add_post_note", {"post_id": str(post.id), "body": "Ask legal"})
        assert added["body"] == "Ask legal"
        listed = tool_result(world.full, "list_post_notes", {"post_id": str(post.id)})
        assert [n["body"] for n in listed["notes"]] == ["Ask legal"]
        assert AgentAction.objects.filter(operation="note.create").count() == 1

    def test_clients_are_refused(self, world):
        post = make_post(world.workspace, world.owner, [world.account1])
        assert "Permission denied" in tool_error(world.client_role, "list_post_notes", {"post_id": str(post.id)})
        envelope = call_tool(world.client_role, "add_post_note", {"post_id": str(post.id), "body": "x"})
        assert "error" in envelope
        assert not PostNote.objects.exists()

    def test_tools_are_registered_with_the_right_annotations(self):
        from apps.api_keys import capabilities as caps

        assert caps.tool_capability("list_post_notes") == "read_calendar"
        assert caps.tool_capability("add_post_note") == "draft_posts"
        assert caps.tool_annotations("list_post_notes")["readOnlyHint"] is True
        hints = caps.tool_annotations("add_post_note")
        assert hints["readOnlyHint"] is False and hints["destructiveHint"] is False and hints["openWorldHint"] is False


@pytest.mark.django_db
class TestCommentOnlyReviewerOverTheApi:
    def test_can_comment_and_read_but_not_decide(self, world):
        post = make_post(world.workspace, world.owner, [world.account1], status="pending_review")
        user, client = comment_only_client(world)
        r = post_json(
            client, f"/api/v1/approvals/{post.id}/comments", {"body": "Looks good to me", "visibility": "internal"}
        )
        assert r.status_code == 201, r.content
        assert client.get(f"/api/v1/approvals/{post.id}/comments").status_code == 200
        assert post_json(client, f"/api/v1/approvals/{post.id}/approve").status_code == 403
        comment = PostComment.objects.get()
        assert comment.author == user
        # The post's author hears about it and the activity records it.
        assert Notification.objects.filter(user=world.owner, event_type=EventType.POST_COMMENT).exists()
        from apps.approvals.models import ApprovalAction

        action = ApprovalAction.objects.get(action="commented")
        assert action.channel == "api"

    def test_a_role_denied_comments_cannot_comment_even_with_create_posts(self, world):
        role = CustomRole.objects.create(
            organization=world.workspace.organization,
            name="No comments",
            permissions={"create_posts": True, "comment_on_posts": False},
        )
        user = User.objects.create_user(
            email="nocomment@example.com", password="x", name="Nic", tos_accepted_at=timezone.now()
        )
        WorkspaceMembership.objects.create(
            user=user, workspace=world.workspace, workspace_role="editor", custom_role=role
        )
        client = key_for(world, user, ["create_posts"], "no-comment")
        post = make_post(world.workspace, world.owner, [world.account1])
        assert post_json(client, f"/api/v1/approvals/{post.id}/comments", {"body": "hi"}).status_code == 403
