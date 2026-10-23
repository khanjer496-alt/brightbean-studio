"""Audit entries for people, agents and automations: evergreen, repeating posts, approvals, replies."""

from __future__ import annotations

import datetime as dt
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.test import Client
from django.utils import timezone

from apps.api.tests.world import make_post
from apps.autonomy.models import ActionType, AgentAction
from apps.calendar import evergreen
from apps.calendar.models import EvergreenRepeat, PostingSlot, Queue, RecurrenceRule
from apps.calendar.tasks import generate_occurrences
from apps.composer.models import PlatformPost, Post
from apps.inbox.models import InboxMessage

from .conftest import set_override, set_policy

pytestmark = pytest.mark.django_db

NOW = timezone.now()


@pytest.fixture
def queue(world):
    for day in range(7):
        for hour in (10, 15, 19):
            PostingSlot.objects.create(social_account=world.account1, day_of_week=day, time=dt.time(hour, 0))
    return Queue.objects.create(workspace=world.workspace, name="Main", social_account=world.account1)


def evergreen_item(world, caption="Ten tips for better sleep", **rules):
    post = Post.objects.create(workspace=world.workspace, caption=caption, author=world.owner)
    PlatformPost.objects.create(
        post=post,
        social_account=world.account1,
        status="published",
        published_at=timezone.now() - timedelta(days=60),
        platform_post_id="p-1",
    )
    return evergreen.add_from_platform_post(post.platform_posts.get(), user=world.owner, **rules)


class TestEvergreen:
    def test_default_follows_each_items_own_setting_and_is_audited_as_the_system(self, world, queue):
        item = evergreen_item(world)
        assert evergreen.fill_account(world.account1, now=NOW).scheduled == 1
        action = AgentAction.objects.get(operation="evergreen.recycle")
        assert (action.actor_type, action.actor_name, action.decision) == ("system", "Evergreen recycling", "executed")
        assert action.source_text == item.caption == action.final_text
        repeat = EvergreenRepeat.objects.get()
        assert action.platform_post_id == repeat.platform_post_id and action.requested_at == repeat.scheduled_for
        assert action.release_at is not None  # the undo window applies to recycled posts as well

    def test_ai_rewording_is_shown_as_a_diff_source_and_final(self, world, queue, settings):
        from apps.ai_writer.tests.fakes import patch_anthropic

        settings.ANTHROPIC_API_KEY = "sk-ant-platform-key-for-tests"
        evergreen_item(world, vary_with_ai=True)
        with patch_anthropic() as fake:
            fake.queue({"text": "Ten sleep tips, freshly worded"})
            evergreen.fill_account(world.account1, now=NOW)
        action = AgentAction.objects.get(operation="evergreen.recycle")
        assert action.ai_used and action.source_text == "Ten tips for better sleep"
        assert action.final_text == "Ten sleep tips, freshly worded"

    def test_draft_only_sends_every_repeat_for_review(self, world, queue):
        set_policy(world.workspace, recycle_level="draft_only")
        evergreen_item(world)
        assert evergreen.fill_account(world.account1, now=NOW).awaiting_approval == 1
        action = AgentAction.objects.get(operation="evergreen.recycle")
        assert action.decision == "held" and "Draft only" in action.summary
        assert action.release_at is None
        assert EvergreenRepeat.objects.get().outcome == "awaiting_approval"

    def test_off_recycles_nothing(self, world, queue):
        set_policy(world.workspace, recycle_level="off")
        evergreen_item(world)
        assert evergreen.fill_account(world.account1, now=NOW).scheduled == 0
        assert not EvergreenRepeat.objects.exists()

    def test_an_account_override_can_turn_recycling_off_for_one_account(self, world, queue):
        set_policy(world.workspace, recycle_level="full_auto")
        set_override(world.workspace, world.account1, recycle_level="off")
        evergreen_item(world)
        assert evergreen.fill_account(world.account1, now=NOW).scheduled == 0

    def test_crisis_pause_stops_recycling(self, world, queue):
        set_policy(world.workspace, crisis_paused=True)
        evergreen_item(world)
        assert evergreen.fill_account(world.account1, now=NOW).scheduled == 0

    def test_guardrails_hold_a_repeat_with_a_link(self, world, queue):
        set_policy(world.workspace, recycle_level="auto_guardrails")
        evergreen_item(world, caption="Read https://example.com/guide")
        assert evergreen.fill_account(world.account1, now=NOW).awaiting_approval == 1
        assert "contains a link" in AgentAction.objects.get(operation="evergreen.recycle").summary

    def test_auto_with_guardrails_queues_clean_text(self, world, queue):
        set_policy(world.workspace, recycle_level="auto_guardrails")
        evergreen_item(world)
        assert evergreen.fill_account(world.account1, now=NOW).scheduled == 1

    def test_a_blocked_phrase_skips_the_item_and_is_logged_once(self, world, queue):
        set_policy(world.workspace, recycle_level="full_auto", blocked_phrases=["sleep"])
        item = evergreen_item(world)
        for _ in range(2):
            evergreen.fill_account(world.account1, now=NOW)
        item.refresh_from_db()
        assert "Autonomy policy" in item.last_skip_reason and not EvergreenRepeat.objects.exists()
        assert AgentAction.objects.filter(operation="evergreen.recycle", decision="blocked").count() == 1

    def test_the_dial_never_skips_a_required_approval(self, world, queue):
        from apps.workspaces.models import Workspace

        Workspace.objects.filter(pk=world.workspace.pk).update(approval_workflow_mode="required_internal")
        set_policy(world.workspace, recycle_level="full_auto")
        evergreen_item(world)
        assert evergreen.fill_account(world.account1, now=NOW).awaiting_approval == 1
        assert AgentAction.objects.get(operation="evergreen.recycle").decision == "held"


class TestRecurrence:
    def test_each_scheduled_copy_is_logged_as_the_system(self, world):
        source = make_post(world.workspace, world.owner, [world.account1], status="scheduled")
        rule = RecurrenceRule.objects.create(post=source, frequency="daily", count=3)
        generate_occurrences(rule, now=timezone.now())
        copies = AgentAction.objects.filter(operation="recurrence.copy")
        assert copies.count() == 2
        assert {(a.actor_type, a.actor_name) for a in copies} == {("system", "Repeating post")}
        assert all(a.platform_post.status == "scheduled" for a in copies)


class TestPeople:
    @pytest.fixture
    def signed_in(self, world):
        client = Client()
        client.force_login(world.owner)
        return client

    def test_a_reply_sent_from_the_inbox_is_recorded_as_the_person(self, world, signed_in):
        message = InboxMessage.objects.create(
            workspace=world.workspace,
            social_account=world.account1,
            platform_message_id="mm",
            message_type="comment",
            body="hi",
            received_at=timezone.now(),
        )
        with patch("apps.inbox.services._dispatch_to_platform", return_value="p-9"):
            response = signed_in.post(
                f"/workspace/{world.workspace.id}/inbox/{message.id}/reply/", {"body": "Thanks!"}, secure=True
            )
        assert response.status_code == 200
        action = AgentAction.objects.get(operation="reply.send")
        assert (action.actor_type, action.actor_user) == ("human", world.owner)
        assert action.action_type == ActionType.REPLY and action.final_text == "Thanks!" and action.level == ""

    def test_approving_a_post_is_recorded_as_the_person(self, world, signed_in):
        post = make_post(world.workspace, world.owner, [world.account1], status="pending_review")
        response = signed_in.post(f"/workspace/{world.workspace.id}/approvals/{post.id}/approve/", secure=True)
        assert response.status_code == 204, response.content  # the toast is carried in headers
        action = AgentAction.objects.get(operation="approval.approve")
        assert action.actor_type == "human" and action.post_id == post.id

    def test_an_agent_recording_an_approval_is_attributed_to_the_agent(self, world):
        post = make_post(world.workspace, world.owner, [world.account1], status="pending_review")
        response = world.full.post(f"/api/v1/approvals/{post.id}/approve", data="{}", content_type="application/json")
        assert response.status_code == 200, response.content
        action = AgentAction.objects.get(operation="approval.approve")
        assert (action.actor_type, action.actor_name) == ("agent", "full")

    def test_scheduling_in_the_composer_is_recorded_as_the_person(self, world):
        from apps.composer.views import _transition_post_children

        post = make_post(world.workspace, world.owner, [world.account1], caption="By hand")
        PlatformPost.objects.filter(post=post).update(scheduled_at=timezone.now() + timedelta(days=1))
        post.refresh_from_db()
        _transition_post_children(post, "scheduled", audit_user=world.owner)
        _transition_post_children(post, "scheduled", audit_user=world.owner)  # a re-save logs nothing new
        action = AgentAction.objects.get(operation="post.schedule")
        assert (action.actor_type, action.actor_user, action.final_text) == ("human", world.owner, "By hand")
