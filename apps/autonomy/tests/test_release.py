"""Undo, hold, review and delete-from-platform; crisis pause holding queued items."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.api.tests.world import make_post
from apps.autonomy import release, service
from apps.autonomy.actors import human, system
from apps.autonomy.models import ActionType, AgentAction
from apps.composer.models import PlatformPost
from apps.inbox.models import InboxMessage, InboxReply
from apps.publisher.engine import PublishEngine
from apps.social_accounts.models import SocialAccount

from .conftest import schedule_via_rest, set_policy

pytestmark = pytest.mark.django_db


def due_ids():
    return {pp.id for pp in PublishEngine()._get_due_platform_posts()}


def scheduled_by_agent(world, *, minutes_ago=1):
    """A post an agent scheduled under auto_guardrails whose time has come but whose window hasn't ended."""
    set_policy(world.workspace, publish_level="auto_guardrails")
    when = (timezone.now() - timedelta(minutes=minutes_ago)).isoformat()
    body = schedule_via_rest(world.full, world.account1, when=when).json()
    return (
        PlatformPost.objects.get(post_id=body["id"]),
        AgentAction.objects.get(post_id=body["id"]),
    )


class TestUndo:
    def test_undo_cancels_the_publish_and_is_recorded(self, world):
        pp, action = scheduled_by_agent(world)
        release.undo(action, world.owner)
        pp.refresh_from_db()
        action.refresh_from_db()
        assert pp.status == "draft" and pp.id not in due_ids()
        assert action.undone_at is not None and action.undone_by == world.owner

    def test_undo_is_idempotent(self, world):
        pp, action = scheduled_by_agent(world)
        release.undo(action, world.owner)
        first = AgentAction.objects.get(pk=action.pk).undone_at
        release.undo(action, world.owner)
        assert AgentAction.objects.get(pk=action.pk).undone_at == first
        assert PlatformPost.objects.get(pk=pp.pk).status == "draft"

    def test_an_item_that_already_went_out_cannot_be_undone(self, world):
        pp, action = scheduled_by_agent(world)
        PlatformPost.objects.filter(pk=pp.pk).update(status="published")
        with pytest.raises(release.ReleaseError, match="already went out"):
            release.undo(action, world.owner)

    def test_a_held_review_item_is_not_something_to_undo(self, world):
        body = schedule_via_rest(world.full, world.account1).json()
        with pytest.raises(release.ReleaseError, match="executed"):
            release.undo(AgentAction.objects.get(post_id=body["id"]), world.owner)

    def test_an_undone_reply_is_never_sent(self, world):
        from apps.autonomy.tasks import send_due_replies

        set_policy(world.workspace, reply_level="auto_guardrails")
        message = InboxMessage.objects.create(
            workspace=world.workspace,
            social_account=world.account1,
            platform_message_id="m",
            message_type="comment",
            body="hi",
            received_at=timezone.now(),
        )
        response = world.full.post(
            f"/api/v1/inbox/{message.id}/replies",
            data='{"body": "hello", "send": true}',
            content_type="application/json",
        )
        assert response.status_code == 201
        action = AgentAction.objects.get(reply_id=response.json()["id"])
        release.undo(action, world.owner)
        with patch("apps.inbox.services._dispatch_to_platform") as dispatch:
            assert send_due_replies(now=timezone.now() + timedelta(hours=1)) == 0
        dispatch.assert_not_called()
        assert InboxReply.objects.get().status == "draft"

    def test_the_tray_lists_what_can_still_be_stopped(self, world):
        pp, action = scheduled_by_agent(world)
        assert [a.pk for a in release.going_out_soon(world.workspace)] == [action.pk]
        release.undo(action, world.owner)
        assert list(release.going_out_soon(world.workspace)) == []


class TestCrisisPause:
    def test_it_holds_queued_autonomous_posts_and_resuming_releases_them(self, world):
        pp, action = scheduled_by_agent(world)
        AgentAction.objects.filter(pk=action.pk).update(release_at=timezone.now())
        assert pp.id in due_ids()
        set_policy(world.workspace, crisis_paused=True)
        assert pp.id not in due_ids()
        set_policy(world.workspace, crisis_paused=False)
        assert pp.id in due_ids()

    def test_it_holds_what_an_automation_queued_but_not_what_a_person_scheduled(self, world):
        automated = make_post(world.workspace, world.owner, [world.account1], status="scheduled")
        by_hand = make_post(world.workspace, world.owner, [world.account2], status="scheduled")
        PlatformPost.objects.update(scheduled_at=timezone.now() - timedelta(minutes=1))
        service.record_scheduled(
            workspace=world.workspace,
            actor=system("Repeating post"),
            operation="recurrence.copy",
            post=automated,
            platform_posts=list(automated.platform_posts.all()),
            text="x",
            summary="Queued as part of a repeating series.",
        )
        assert len(due_ids()) == 2
        set_policy(world.workspace, crisis_paused=True)
        assert due_ids() == {by_hand.platform_posts.get().id}

    def test_it_holds_retries_too(self, world):
        pp, action = scheduled_by_agent(world)
        AgentAction.objects.filter(pk=action.pk).update(release_at=timezone.now())
        PlatformPost.objects.filter(pk=pp.pk).update(retry_count=1, next_retry_at=timezone.now() - timedelta(minutes=1))
        set_policy(world.workspace, crisis_paused=True)
        with patch.object(PublishEngine, "_publish_platform_post") as publish:
            PublishEngine()._process_retries()
        publish.assert_not_called()

    def test_crisis_pause_holds_queued_replies(self, world):
        from apps.autonomy.tasks import send_due_replies

        set_policy(world.workspace, reply_level="auto_guardrails")
        message = InboxMessage.objects.create(
            workspace=world.workspace,
            social_account=world.account1,
            platform_message_id="m2",
            message_type="comment",
            body="hi",
            received_at=timezone.now(),
        )
        world.full.post(
            f"/api/v1/inbox/{message.id}/replies",
            data='{"body": "hello", "send": true}',
            content_type="application/json",
        )
        set_policy(world.workspace, crisis_paused=True)
        with patch("apps.inbox.services._dispatch_to_platform") as dispatch:
            assert send_due_replies(now=timezone.now() + timedelta(hours=1)) == 0
        dispatch.assert_not_called()

    def test_turning_it_on_and_off_is_recorded_with_who_did_it(self, world):
        from apps.autonomy import configure

        configure.update_policy(world.workspace, human(world.owner), crisis_paused=True)
        policy = AgentAction.objects.get(operation="autonomy.crisis_pause")
        assert policy.actor_type == "human" and policy.actor_user == world.owner
        configure.update_policy(world.workspace, human(world.owner), crisis_paused=False)
        assert AgentAction.objects.filter(operation="autonomy.crisis_pause").count() == 2


class TestReview:
    def test_approving_a_held_post_schedules_it_as_that_person(self, world):
        body = schedule_via_rest(world.full, world.account1).json()
        held = AgentAction.objects.get(post_id=body["id"])
        release.approve_held(held, world.owner)
        assert PlatformPost.objects.get(post_id=body["id"]).status == "scheduled"
        held.refresh_from_db()
        assert held.review_outcome == "approved" and held.reviewed_by == world.owner
        approval = AgentAction.objects.get(operation="autonomy.approve_held")
        assert approval.actor_type == "human" and approval.actor_user == world.owner
        with pytest.raises(release.ReleaseError):
            release.approve_held(held, world.owner)  # only once

    def test_dismissing_leaves_the_draft_and_discards_a_held_reply(self, world):
        body = schedule_via_rest(world.full, world.account1).json()
        held = AgentAction.objects.get(post_id=body["id"])
        release.dismiss_held(held, world.owner)
        assert PlatformPost.objects.get(post_id=body["id"]).status == "draft"
        assert AgentAction.objects.get(pk=held.pk).review_outcome == "dismissed"

    def test_approving_a_held_reply_sends_it(self, world):
        message = InboxMessage.objects.create(
            workspace=world.workspace,
            social_account=world.account1,
            platform_message_id="m3",
            message_type="comment",
            body="hi",
            received_at=timezone.now(),
        )
        body = world.full.post(
            f"/api/v1/inbox/{message.id}/replies",
            data='{"body": "hello", "send": true}',
            content_type="application/json",
        ).json()
        held = AgentAction.objects.get(reply_id=body["id"])
        with patch("apps.inbox.services._dispatch_to_platform", return_value="p"):
            release.approve_held(held, world.owner)
        assert InboxReply.objects.get().status == "sent"


class TestDeleteFromPlatform:
    def published(self, world, platform):
        account = SocialAccount.objects.create(
            workspace=world.workspace,
            platform=platform,
            account_platform_id=f"{platform}-1",
            account_name=platform,
            connection_status="connected",
            oauth_access_token="tok",
        )
        post = make_post(world.workspace, world.owner, [account], status="published")
        PlatformPost.objects.filter(post=post).update(platform_post_id="remote-1")
        pp = post.platform_posts.get()
        action = service.record(
            workspace=world.workspace,
            actor=system("Evergreen recycling"),
            action_type=ActionType.PUBLISH_POSTS,
            operation="evergreen.recycle",
            decision="executed",
            account=account,
            post=post,
            platform_post=pp,
        )
        return action

    def test_a_provider_that_supports_delete_removes_the_post_and_the_result_is_recorded(self, world):
        action = self.published(world, "x")
        assert release.can_delete_from_platform(action)
        provider = SimpleNamespace(delete_post=lambda token, post_id: post_id == "remote-1")
        with patch("apps.publisher.engine._provider_and_access_token", return_value=(provider, "tok")):
            release.delete_from_platform(action, world.owner)
        action.refresh_from_db()
        assert action.platform_deleted_at is not None and action.platform_delete_result == "Deleted from the platform."
        entry = AgentAction.objects.get(operation="post.delete_from_platform")
        assert entry.actor_type == "human" and entry.decision == "executed"
        assert not release.can_delete_from_platform(action)
        release.delete_from_platform(action, world.owner)  # a second call changes nothing
        assert AgentAction.objects.filter(operation="post.delete_from_platform").count() == 1

    def test_a_platform_refusal_is_recorded_as_a_failure(self, world):
        action = self.published(world, "x")
        provider = SimpleNamespace(delete_post=lambda token, post_id: (_ for _ in ()).throw(RuntimeError("nope")))
        with (
            patch("apps.publisher.engine._provider_and_access_token", return_value=(provider, "tok")),
            pytest.raises(release.ReleaseError, match="refused"),
        ):
            release.delete_from_platform(action, world.owner)
        action.refresh_from_db()
        assert action.platform_deleted_at is None and "refused" in action.platform_delete_result
        assert AgentAction.objects.get(operation="post.delete_from_platform").decision == "blocked"

    def test_a_provider_without_delete_support_is_offered_no_button_and_says_so(self, world):
        action = self.published(world, "bluesky")
        assert not release.can_delete_from_platform(action)
        with pytest.raises(release.ReleaseError, match="does not allow deleting"):
            release.delete_from_platform(action, world.owner)
        action.refresh_from_db()
        assert "does not allow deleting" in action.platform_delete_result

    def test_an_unpublished_post_has_nothing_to_delete(self, world):
        pp, action = scheduled_by_agent(world)
        assert not release.can_delete_from_platform(action)
        with pytest.raises(release.ReleaseError):
            release.delete_from_platform(action, world.owner)
