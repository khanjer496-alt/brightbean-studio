"""Every REST and MCP write by an agent goes through the policy, and the agent is told why."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.api.tests.world import make_post, tool_error, tool_result
from apps.autonomy.models import ActionType, AgentAction
from apps.composer.models import PlatformPost, Post
from apps.inbox.models import InboxMessage, InboxReply
from apps.notifications.models import Notification
from apps.publisher.engine import PublishEngine
from apps.webhooks.models import WebhookDelivery, WebhookEndpoint

from .conftest import patch_json, post_json, schedule_via_rest, set_override, set_policy

pytestmark = pytest.mark.django_db


def due_ids():
    return {pp.id for pp in PublishEngine()._get_due_platform_posts()}


def the_action(**filters) -> AgentAction:
    return AgentAction.objects.filter(**filters).latest("created_at")


class TestScheduleOverRest:
    def test_draft_only_turns_a_schedule_into_a_draft_and_says_why(self, world):
        response = schedule_via_rest(world.full, world.account1)
        assert response.status_code == 201
        body = response.json()
        assert body["autonomy"]["decision"] == "held"
        assert body["autonomy"]["message"].startswith("Held for review because")
        assert "Draft only" in body["autonomy"]["reasons"][0]
        assert [pp["status"] for pp in body["platform_posts"]] == ["draft"]
        assert body["proposed_publish_at"] is not None  # the requested time is kept as the proposal
        action = the_action(workspace=world.workspace)
        assert (action.actor_type, action.actor_name, action.decision) == ("agent", "full", "held")
        assert action.action_type == ActionType.PUBLISH_POSTS and str(action.post_id) == body["id"]

    def test_auto_with_guardrails_schedules_and_opens_an_undo_window(self, world):
        set_policy(world.workspace, publish_level="auto_guardrails")
        body = schedule_via_rest(world.full, world.account1).json()
        assert body["autonomy"]["decision"] == "executed" and body["autonomy"]["undo_until"]
        assert [pp["status"] for pp in body["platform_posts"]] == ["scheduled"]
        action = the_action(workspace=world.workspace)
        assert action.release_at is not None and action.level == "auto_guardrails"

    def test_a_guardrail_hit_holds_and_lists_the_reasons(self, world):
        set_policy(world.workspace, publish_level="auto_guardrails")
        body = schedule_via_rest(world.full, world.account1, "Sale! 30% off at https://shop.example.com").json()
        assert body["autonomy"]["decision"] == "held"
        assert any("link" in r for r in body["autonomy"]["reasons"])
        assert any("price" in r for r in body["autonomy"]["reasons"])
        stored = the_action(workspace=world.workspace)
        assert {c["name"] for c in stored.guardrail_results if not c["passed"]} == {"links", "prices"}

    def test_off_refuses_with_a_403_and_logs_the_refusal_without_saving_anything(self, world):
        set_policy(world.workspace, publish_level="off")
        response = schedule_via_rest(world.full, world.account1)
        assert response.status_code == 403
        assert "Blocked because" in response.json()["detail"] and "Nothing was saved" in response.json()["detail"]
        assert not Post.objects.exists()
        assert the_action(workspace=world.workspace).decision == "blocked"

    def test_off_for_drafts_also_refuses_a_held_schedule(self, world):
        set_policy(world.workspace, draft_level="off")
        response = schedule_via_rest(world.full, world.account1)
        assert response.status_code == 403 and not Post.objects.exists()

    def test_a_blocked_phrase_is_refused_even_at_full_auto(self, world):
        set_policy(world.workspace, publish_level="full_auto", blocked_phrases=["guaranteed cure"])
        response = schedule_via_rest(world.full, world.account1, "A guaranteed cure!")
        assert response.status_code == 403 and "blocked phrase" in response.json()["detail"]

    def test_an_account_override_applies_to_that_account_only(self, world):
        set_override(world.workspace, world.account1, publish_level="full_auto")
        assert schedule_via_rest(world.full, world.account1).json()["autonomy"]["decision"] == "executed"
        assert schedule_via_rest(world.full, world.account2).json()["autonomy"]["decision"] == "held"

    def test_a_plain_draft_is_recorded_as_drafting(self, world):
        response = post_json(
            world.full, "/api/v1/posts/", {"social_account_id": str(world.account1.id), "caption": "Draft idea"}
        )
        assert response.json()["autonomy"]["decision"] == "executed"
        assert the_action(workspace=world.workspace).action_type == ActionType.DRAFT_CONTENT

    def test_scheduling_an_existing_draft_is_gated_per_account(self, world):
        post = make_post(world.workspace, world.owner, [world.account1, world.account2], caption="Hello there")
        set_override(world.workspace, world.account1, publish_level="full_auto")
        response = post_json(world.full, f"/api/v1/posts/{post.id}/schedule", {"scheduled_at": _soon()})
        assert response.status_code == 200
        statuses = dict(PlatformPost.objects.filter(post=post).values_list("social_account_id", "status"))
        assert statuses == {world.account1.id: "scheduled", world.account2.id: "draft"}
        assert response.json()["autonomy"]["decision"] == "held"  # the most restrictive outcome is reported
        assert AgentAction.objects.filter(post=post).count() == 2

    def test_editing_a_scheduled_post_is_a_publish_decision_again(self, world):
        post = make_post(world.workspace, world.owner, [world.account1], status="scheduled", caption="Original")
        response = patch_json(world.full, f"/api/v1/posts/{post.id}", {"caption": "Changed text"})
        assert response.status_code == 200
        assert response.json()["autonomy"]["decision"] == "held"
        assert PlatformPost.objects.get(post=post).status == "draft"

    def test_a_refused_edit_changes_nothing_and_is_still_logged(self, world):
        post = make_post(world.workspace, world.owner, [world.account1], status="scheduled", caption="Original")
        set_policy(world.workspace, publish_level="off")
        response = patch_json(world.full, f"/api/v1/posts/{post.id}", {"caption": "Changed"})
        assert response.status_code == 403
        post.refresh_from_db()
        assert post.caption == "Original"
        assert the_action(workspace=world.workspace).decision == "blocked"  # survives the rolled-back request

    def test_duplicating_needs_drafting_to_be_allowed(self, world):
        post = make_post(world.workspace, world.owner, [world.account1])
        set_policy(world.workspace, draft_level="off")
        assert post_json(world.full, f"/api/v1/posts/{post.id}/duplicate", {}).status_code == 403
        set_policy(world.workspace, draft_level="draft_only")
        assert (
            post_json(world.full, f"/api/v1/posts/{post.id}/duplicate", {}).json()["autonomy"]["decision"] == "executed"
        )


def _soon():
    from datetime import timedelta

    return (timezone.now() + timedelta(hours=3)).isoformat()


class TestScheduleOverMcp:
    def test_schedule_post_is_held_with_a_reason_the_agent_can_act_on(self, world):
        result = tool_result(
            world.full,
            "schedule_post",
            {"social_account_id": str(world.account1.id), "caption": "Hello", "scheduled_at": _soon()},
        )
        assert result["autonomy"]["decision"] == "held"
        assert "Held for review because" in result["autonomy"]["message"]
        assert result["platform_posts"][0]["status"] == "draft"
        assert the_action(workspace=world.workspace).actor_name == "full"

    def test_schedule_post_runs_when_the_policy_allows(self, world):
        set_policy(world.workspace, publish_level="full_auto", undo_delay_minutes=0)
        result = tool_result(
            world.full,
            "schedule_post",
            {"social_account_id": str(world.account1.id), "caption": "Hello", "scheduled_at": _soon()},
        )
        assert result["autonomy"]["decision"] == "executed" and result["platform_posts"][0]["status"] == "scheduled"
        assert result["autonomy"]["undo_until"] is None  # a zero-minute window means no wait

    def test_a_refusal_is_an_error_naming_the_cause(self, world):
        set_policy(world.workspace, publish_level="off")
        message = tool_error(
            world.full,
            "schedule_post",
            {"social_account_id": str(world.account1.id), "caption": "Hello", "scheduled_at": _soon()},
        )
        assert "Blocked because" in message and "Off" in message
        assert the_action(workspace=world.workspace).decision == "blocked"

    def test_a_refusal_is_logged_even_when_the_call_is_idempotent_and_rolled_back(self, world):
        set_policy(world.workspace, publish_level="off")
        arguments = {
            "social_account_id": str(world.account1.id),
            "caption": "Hello",
            "scheduled_at": _soon(),
            "idempotency_key": "k-1",
        }
        assert "Blocked" in tool_error(world.full, "schedule_post", arguments)
        assert AgentAction.objects.filter(decision="blocked").count() == 1

    def test_create_draft_off(self, world):
        set_policy(world.workspace, draft_level="off")
        message = tool_error(world.full, "create_draft", {"social_account_id": str(world.account1.id), "caption": "x"})
        assert "Blocked because" in message and not Post.objects.exists()

    def test_create_draft_is_recorded(self, world):
        result = tool_result(world.full, "create_draft", {"social_account_id": str(world.account1.id), "caption": "x"})
        assert result["autonomy"]["decision"] == "executed"

    def test_schedule_draft_is_gated(self, world):
        post = make_post(world.workspace, world.owner, [world.account1], caption="Ready")
        result = tool_result(world.full, "schedule_draft", {"post_id": str(post.id), "scheduled_at": _soon()})
        assert result["autonomy"]["decision"] == "held"
        assert PlatformPost.objects.get(post=post).status == "draft"

    def test_policy_and_actions_tools(self, world):
        policy = tool_result(world.bare, "get_autonomy_policy")  # any key may read the policy
        assert policy["publish_level"] == "draft_only" and policy["undo_delay_minutes"] == 5
        schedule_via_rest(world.full, world.account1)
        actions = tool_result(world.full, "list_agent_actions", {"decision": "held"})["actions"]
        assert [a["decision"] for a in actions] == ["held"]
        assert "Permission denied" in tool_error(world.bare, "list_agent_actions")


class TestReplies:
    @pytest.fixture
    def message(self, world):
        return InboxMessage.objects.create(
            workspace=world.workspace,
            social_account=world.account1,
            platform_message_id="m-1",
            message_type=InboxMessage.MessageType.COMMENT,
            sender_name="Ada",
            sender_handle="ada",
            body="What time do you open?",
            received_at=timezone.now(),
        )

    def reply(self, world, message, send=True, body="We open at nine."):
        return post_json(world.full, f"/api/v1/inbox/{message.id}/replies", {"body": body, "send": send})

    def test_draft_only_leaves_a_send_request_as_a_draft(self, world, message):
        with patch("apps.inbox.services._dispatch_to_platform") as dispatch:
            response = self.reply(world, message)
        assert response.status_code == 201
        body = response.json()
        assert body["status"] == "draft" and body["autonomy"]["decision"] == "held"
        dispatch.assert_not_called()
        assert the_action(workspace=world.workspace).action_type == ActionType.REPLY

    def test_a_plain_reply_draft_is_allowed_at_draft_only(self, world, message):
        body = self.reply(world, message, send=False).json()
        assert body["autonomy"]["decision"] == "executed" and body["status"] == "draft"

    def test_off_refuses_even_a_draft(self, world, message):
        set_policy(world.workspace, reply_level="off")
        assert self.reply(world, message, send=False).status_code == 403
        assert not InboxReply.objects.exists()

    def test_auto_waits_out_the_undo_window_then_the_sweep_sends(self, world, message):
        from datetime import timedelta

        from apps.autonomy.tasks import send_due_replies

        set_policy(world.workspace, reply_level="auto_guardrails")
        with patch("apps.inbox.services._dispatch_to_platform", return_value="p-1") as dispatch:
            body = self.reply(world, message).json()
            assert body["status"] == "draft" and body["autonomy"]["undo_until"]
            dispatch.assert_not_called()
            assert send_due_replies() == 0  # still inside the window
            action = the_action(workspace=world.workspace)
            assert send_due_replies(now=action.release_at + timedelta(seconds=1)) == 1
        assert InboxReply.objects.get().status == "sent"

    def test_zero_minute_window_sends_straight_away(self, world, message):
        set_policy(world.workspace, reply_level="full_auto", undo_delay_minutes=0)
        with patch("apps.inbox.services._dispatch_to_platform", return_value="p-1"):
            assert self.reply(world, message).json()["status"] == "sent"

    def test_a_flagged_reply_is_held(self, world, message):
        set_policy(world.workspace, reply_level="auto_guardrails", undo_delay_minutes=0)
        with patch("apps.inbox.services._dispatch_to_platform") as dispatch:
            body = self.reply(world, message, body="Book at https://example.com/now").json()
        assert body["autonomy"]["decision"] == "held" and body["status"] == "draft"
        dispatch.assert_not_called()

    def test_mcp_send_reply_is_held_and_explains(self, world, message):
        with patch("apps.inbox.services._dispatch_to_platform") as dispatch:
            result = tool_result(world.full, "send_reply", {"message_id": str(message.id), "body": "Hi"})
        assert result["status"] == "draft" and "Held for review" in result["autonomy"]["message"]
        dispatch.assert_not_called()

    def test_mcp_send_reply_refused_does_not_keep_the_throwaway_draft(self, world, message):
        set_policy(world.workspace, reply_level="off")
        assert "Blocked because" in tool_error(world.full, "send_reply", {"message_id": str(message.id), "body": "Hi"})
        assert not InboxReply.objects.exists()

    def test_sending_an_existing_draft_over_rest(self, world, message):
        draft = InboxReply.objects.create(inbox_message=message, body="ready")
        response = world.full.post(f"/api/v1/inbox/replies/{draft.id}/send")
        assert response.status_code == 200 and response.json()["autonomy"]["decision"] == "held"


class TestPublisherRespectsTheWindow:
    def schedule_in_the_past(self, world):
        set_policy(world.workspace, publish_level="auto_guardrails")
        from datetime import timedelta

        when = (timezone.now() - timedelta(minutes=1)).isoformat()
        body = schedule_via_rest(world.full, world.account1, when=when).json()
        return PlatformPost.objects.get(post_id=body["id"]), the_action(workspace=world.workspace)

    def test_a_due_autonomous_post_waits_until_the_window_ends(self, world):
        pp, action = self.schedule_in_the_past(world)
        assert pp.id not in due_ids()
        AgentAction.objects.filter(pk=action.pk).update(release_at=timezone.now())
        assert pp.id in due_ids()

    def test_a_post_a_person_scheduled_is_not_delayed(self, world):
        post = make_post(world.workspace, world.owner, [world.account1], status="scheduled")
        PlatformPost.objects.filter(post=post).update(
            scheduled_at=timezone.now() - __import__("datetime").timedelta(minutes=1)
        )
        assert PlatformPost.objects.get(post=post).id in due_ids()

    def test_hold_keeps_it_back_until_released(self, world):
        from apps.autonomy import release

        pp, action = self.schedule_in_the_past(world)
        AgentAction.objects.filter(pk=action.pk).update(release_at=timezone.now())
        release.hold(action, world.owner)
        assert pp.id not in due_ids()
        release.release_hold(action, world.owner)
        assert pp.id in due_ids()

    def test_the_engine_rechecks_under_its_lock(self, world):
        pp, action = self.schedule_in_the_past(world)
        pp = PlatformPost.objects.select_related("post__workspace", "social_account").get(pk=pp.pk)
        with patch.object(PublishEngine, "_publish_platform_post") as publish:
            assert PublishEngine()._publish_post_group(pp.post, [pp]) is False
        publish.assert_not_called()
        assert PlatformPost.objects.get(pk=pp.pk).status == "scheduled"


class TestNotificationsAndWebhooks:
    def test_held_and_blocked_actions_notify_the_owner_and_emit_webhooks(self, world):
        endpoint = WebhookEndpoint.objects.create(
            workspace=world.workspace,
            url="https://hooks.example.com/x",
            event_types=["agent.action.held", "agent.action.blocked", "agent.action.executed"],
            secret="s3cret",
        )
        schedule_via_rest(world.full, world.account1)  # held
        set_policy(world.workspace, publish_level="off")
        schedule_via_rest(world.full, world.account1)  # blocked
        set_policy(world.workspace, publish_level="full_auto")
        schedule_via_rest(world.full, world.account1)  # executed
        events = list(WebhookDelivery.objects.filter(endpoint=endpoint).values_list("event_type", flat=True))
        assert sorted(events) == ["agent.action.blocked", "agent.action.executed", "agent.action.held"]
        payload = WebhookDelivery.objects.get(endpoint=endpoint, event_type="agent.action.held").payload["data"]
        assert payload["actor_name"] == "full" and payload["decision"] == "held" and payload["reasons"]
        kinds = list(Notification.objects.filter(user=world.owner).values_list("event_type", flat=True))
        assert kinds.count("agent_action_held") == 1 and kinds.count("agent_action_blocked") == 1

    def test_notification_preferences_are_respected(self, world):
        from apps.notifications.models import Channel, NotificationDelivery, NotificationPreference

        NotificationPreference.objects.create(
            user=world.owner, event_type="agent_action_held", channel=Channel.EMAIL, is_enabled=False
        )
        schedule_via_rest(world.full, world.account1)
        channels = list(
            NotificationDelivery.objects.filter(notification__event_type="agent_action_held").values_list(
                "channel", flat=True
            )
        )
        assert channels == ["in_app"]

    def test_a_person_acting_does_not_notify_anyone(self, world):
        from apps.autonomy import service
        from apps.autonomy.actors import human

        service.record(
            workspace=world.workspace,
            actor=human(world.owner),
            action_type=ActionType.PUBLISH_POSTS,
            operation="post.schedule",
            decision="executed",
        )
        assert not Notification.objects.exists()


class TestPolicyApi:
    def test_any_key_reads_the_policy(self, world):
        response = world.bare.get("/api/v1/autonomy/")
        assert response.status_code == 200 and response.json()["draft_level"] == "draft_only"

    def test_the_policy_needs_manage_workspace_settings_to_change(self, world):
        assert patch_json(world.bare, "/api/v1/autonomy/", {"crisis_paused": True}).status_code == 403
        assert patch_json(world.full, "/api/v1/autonomy/", {"crisis_paused": True}).status_code == 200

    def test_a_key_can_tighten_but_never_loosen(self, world):
        set_policy(world.workspace, publish_level="auto_guardrails")
        ok = patch_json(world.full, "/api/v1/autonomy/", {"publish_level": "draft_only", "blocked_phrases": ["spam"]})
        assert ok.status_code == 200 and ok.json()["publish_level"] == "draft_only"
        assert patch_json(world.full, "/api/v1/autonomy/", {"publish_level": "full_auto"}).status_code == 403
        assert patch_json(world.full, "/api/v1/autonomy/", {"undo_delay_minutes": 0}).status_code == 403
        assert patch_json(world.full, "/api/v1/autonomy/", {"blocked_phrases": []}).status_code == 403
        patch_json(world.full, "/api/v1/autonomy/", {"crisis_paused": True})
        assert patch_json(world.full, "/api/v1/autonomy/", {"crisis_paused": False}).status_code == 403

    def test_the_change_lands_in_the_audit_trail(self, world):
        patch_json(world.full, "/api/v1/autonomy/", {"crisis_paused": True})
        action = the_action(workspace=world.workspace)
        assert action.operation == "autonomy.crisis_pause" and action.actor_name == "full"

    def test_list_filters_and_allowlist(self, world):
        schedule_via_rest(world.full, world.account1)
        schedule_via_rest(world.full, world.account2)
        everything = world.full.get("/api/v1/autonomy/actions").json()["actions"]
        assert len(everything) == 2
        narrow = world.narrow.get("/api/v1/autonomy/actions").json()["actions"]
        assert [a["social_account_id"] for a in narrow] == [str(world.account1.id)]
        assert world.full.get("/api/v1/autonomy/actions?decision=blocked").json()["actions"] == []
        assert world.full.get("/api/v1/autonomy/actions?actor_type=agent").json()["actions"]
        assert world.full.get("/api/v1/autonomy/actions?actor_type=robot").status_code == 422
        assert world.bare.get("/api/v1/autonomy/actions").status_code == 403

    def test_other_workspaces_see_nothing(self, world):
        schedule_via_rest(world.full, world.account1)
        assert world.other.get("/api/v1/autonomy/actions").json()["actions"] == []

    def test_mcp_update_policy(self, world):
        result = tool_result(world.full, "update_autonomy_policy", {"crisis_paused": True})
        assert result["crisis_paused"] is True
        assert "can lower" in tool_error(world.full, "update_autonomy_policy", {"publish_level": "full_auto"})
