"""The holes closed in review: write paths that skipped the policy, evasions, races and held repeats."""

from __future__ import annotations

import datetime as dt
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from apps.api.tests.world import make_post
from apps.api_keys.models import ApiKey
from apps.autonomy import gate, release
from apps.autonomy.actors import agent
from apps.autonomy.guardrails import blocked_phrase_hits, check_classifiers, normalize
from apps.autonomy.models import AgentAction, AutonomyPolicy
from apps.calendar.models import PostingSlot, Queue, RecurrenceRule
from apps.composer.models import PlatformPost, Post
from apps.inbox.models import InboxMessage, InboxReply

from .conftest import patch_json, post_json, schedule_via_rest, set_policy

pytestmark = pytest.mark.django_db


def the_action(**filters) -> AgentAction:
    return AgentAction.objects.filter(**filters).latest("created_at")


class TestNormalisedMatching:
    @pytest.mark.parametrize(
        "text",
        [
            "Get f​ree money now",  # zero-width space
            "Get frее mоney now",  # Cyrillic е and о
            "Get 𝐅𝐑𝐄𝐄 𝐌𝐎𝐍𝐄𝐘 now",  # mathematical bold
            "Get ｆｒｅｅ ｍｏｎｅｙ now",  # full width
            "Get fr̶ee money now",  # combining strike-through
            "Get FREE\n money now",
        ],
    )
    def test_look_alike_spellings_still_hit_a_blocked_phrase(self, text):
        assert blocked_phrase_hits(["free money"], text) == ["free money"]

    def test_whole_words_only_in_spaced_scripts(self):
        assert blocked_phrase_hits(["free"], "a carefree day") == []

    def test_arabic_phrases_ignore_diacritics_tatweel_and_prefixes(self):
        assert blocked_phrase_hits(["حرام"], "والحَرَامُ") == ["حرام"]
        assert blocked_phrase_hits(["إعلان"], "اعـــلان جديد") == ["إعلان"]

    def test_folding_is_the_same_for_phrase_and_text(self):
        assert normalize("Café") == normalize("CAFÉ") == "cafe"

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("خصم ٥٠٪ على كل شيء", {"prices"}),
            ("هذا المنتج يعالج الصداع", {"health_claims"}),
            ("أرباح مضمونة كل شهر", {"financial_claims"}),
            ("نقدم استشارة قانونية مجانية", {"legal_claims"}),
            ("💲50 only today", {"prices"}),
            ("visit example。com", {"links"}),
            ("Good morning ☀️ have a lovely day 😊", set()),
            ("صباح الخير يا أصدقاء 🌸", set()),
            ("Dog treats for our furry friends", set()),
            ("Thanks Sue for coming by", set()),
        ],
    )
    def test_classifiers_read_arabic_and_emoji_without_crying_wolf(self, text, expected):
        policy = AutonomyPolicy()
        assert {r.name for r in check_classifiers(policy, text) if not r.passed} == expected


class TestEverythingThePostSaysIsChecked:
    def test_a_blocked_phrase_in_the_first_comment_is_refused(self, world):
        set_policy(world.workspace, publish_level="full_auto", blocked_phrases=["guaranteed cure"])
        response = post_json(
            world.full,
            "/api/v1/posts/",
            {
                "social_account_id": str(world.account1.id),
                "caption": "A calm update",
                "first_comment": "It's a guaranteed cure",
                "action": "schedule",
                "scheduled_at": (timezone.now() + timedelta(hours=2)).isoformat(),
            },
        )
        assert response.status_code == 403 and not Post.objects.exists()

    def test_a_blocked_phrase_in_a_thread_part_is_refused(self, world):
        set_policy(world.workspace, publish_level="auto_guardrails", blocked_phrases=["guaranteed cure"])
        response = post_json(
            world.full,
            "/api/v1/posts/",
            {
                "social_account_id": str(world.account1.id),
                "caption": "A calm update",
                "parts": [{"caption": "Part two"}, {"caption": "a guaranteed cure"}],
                "action": "schedule",
                "scheduled_at": (timezone.now() + timedelta(hours=2)).isoformat(),
            },
        )
        assert response.status_code == 403

    def test_editing_the_first_comment_of_a_scheduled_post_is_checked(self, world):
        set_policy(world.workspace, publish_level="full_auto", undo_delay_minutes=0)
        body = schedule_via_rest(world.full, world.account1).json()
        set_policy(world.workspace, blocked_phrases=["guaranteed cure"])
        response = patch_json(world.full, f"/api/v1/posts/{body['id']}", {"first_comment": "a guaranteed cure"})
        assert response.status_code == 403
        assert Post.objects.get(pk=body["id"]).first_comment == ""


class TestQueueing:
    @pytest.fixture
    def queue(self, world):
        for day in range(7):
            PostingSlot.objects.create(social_account=world.account1, day_of_week=day, time=dt.time(10, 0))
        return Queue.objects.create(workspace=world.workspace, name="Main", social_account=world.account1)

    def test_queueing_a_held_draft_is_held_again_not_scheduled(self, world, queue):
        post = make_post(world.workspace, world.owner, [world.account1])
        response = post_json(world.full, f"/api/v1/queues/{queue.id}/entries", {"post_id": str(post.id)})
        assert response.status_code == 403 and "Held for review" in response.json()["detail"]
        assert PlatformPost.objects.get(post=post).status == "draft"
        held = the_action(workspace=world.workspace, operation="queue.add")
        assert held.decision == "held" and held.requested_at is not None

    def test_queueing_is_allowed_when_the_policy_allows_publishing(self, world, queue):
        set_policy(world.workspace, publish_level="full_auto")
        post = make_post(world.workspace, world.owner, [world.account1])
        response = post_json(world.full, f"/api/v1/queues/{queue.id}/entries", {"post_id": str(post.id)})
        assert response.status_code == 201
        assert PlatformPost.objects.get(post=post).status == "scheduled"
        assert the_action(workspace=world.workspace, operation="queue.add").decision == "executed"


class TestResumeThread:
    def test_an_agent_that_may_not_publish_cannot_resume_a_thread(self, world):
        post = make_post(world.workspace, world.owner, [world.account1], status="published")
        pp = PlatformPost.objects.get(post=post)
        refusal = gate.gate_resume(
            workspace=world.workspace,
            actor=agent(ApiKey.objects.get(workspace=world.workspace, name="full")),
            post=post,
            platform_posts=[pp],
        )
        assert refusal is not None and refusal.message.startswith("Not resumed because")
        entry = the_action(workspace=world.workspace, operation="post.resume_thread")
        assert entry.decision == "blocked"

    def test_crisis_pause_stops_a_resume_even_at_full_auto(self, world):
        set_policy(world.workspace, publish_level="full_auto", crisis_paused=True)
        post = make_post(world.workspace, world.owner, [world.account1], status="published")
        refusal = gate.gate_resume(
            workspace=world.workspace,
            actor=agent(ApiKey.objects.get(workspace=world.workspace, name="full")),
            post=post,
            platform_posts=list(post.platform_posts.all()),
        )
        assert refusal is not None and "crisis pause" in refusal.message


class TestEditingAQueuedReply:
    @pytest.fixture
    def queued(self, world):
        set_policy(world.workspace, reply_level="auto_guardrails", blocked_phrases=["refund guaranteed"])
        message = InboxMessage.objects.create(
            workspace=world.workspace,
            social_account=world.account1,
            platform_message_id="m-1",
            message_type=InboxMessage.MessageType.COMMENT,
            sender_name="Ada",
            sender_handle="ada",
            body="Hi",
            received_at=timezone.now(),
        )
        body = post_json(world.full, f"/api/v1/inbox/{message.id}/replies", {"body": "We open at nine.", "send": True})
        assert body.json()["autonomy"]["undo_until"]
        return InboxReply.objects.get()

    def test_an_edit_into_blocked_text_is_refused_and_the_old_text_stays(self, world, queued):
        response = patch_json(world.full, f"/api/v1/inbox/replies/{queued.id}", {"body": "Refund guaranteed!"})
        assert response.status_code == 403
        queued.refresh_from_db()
        assert queued.body == "We open at nine."

    def test_an_edit_is_a_fresh_send_decision_with_a_new_window(self, world, queued):
        first = the_action(reply=queued)
        response = patch_json(world.full, f"/api/v1/inbox/replies/{queued.id}", {"body": "Book at https://x.io"})
        assert response.status_code == 200 and response.json()["autonomy"]["decision"] == "held"
        first.refresh_from_db()
        assert first.undone_at is not None  # the old approval can't send the new text

        from apps.autonomy.tasks import send_due_replies

        with patch("apps.inbox.services._dispatch_to_platform") as dispatch:
            assert send_due_replies(now=timezone.now() + timedelta(hours=1)) == 0
        dispatch.assert_not_called()


class TestHeldRepeatsAreKept:
    def test_a_held_schedule_keeps_its_repeat_and_approval_starts_the_series(self, world):
        when = (timezone.now() + timedelta(days=1)).isoformat()
        body = post_json(
            world.full,
            "/api/v1/posts/",
            {
                "social_account_id": str(world.account1.id),
                "caption": "Weekly tip",
                "action": "schedule",
                "scheduled_at": when,
                "recurrence": {"frequency": "weekly", "count": 3},
            },
        ).json()
        assert body["autonomy"]["decision"] == "held"
        rule = RecurrenceRule.objects.get(post_id=body["id"])
        assert rule.is_active and rule.count == 3
        assert not Post.objects.filter(recurrence_source_id=body["id"]).exists()

        release.approve_held(the_action(workspace=world.workspace, decision="held"), world.owner)
        assert PlatformPost.objects.get(post_id=body["id"]).status == "scheduled"
        assert Post.objects.filter(recurrence_source_id=body["id"]).count() == 2


class TestUndoRacesThePublisher:
    def test_undo_after_the_publisher_claimed_it_says_it_already_went(self, world):
        set_policy(world.workspace, publish_level="auto_guardrails")
        body = schedule_via_rest(world.full, world.account1).json()
        action = the_action(workspace=world.workspace)
        PlatformPost.objects.filter(post_id=body["id"]).update(status="publishing")
        with pytest.raises(release.ReleaseError):
            release.undo(action, world.owner)
        with pytest.raises(release.ReleaseError):
            release.hold(action, world.owner)
        assert PlatformPost.objects.get(post_id=body["id"]).status == "publishing"


class TestKeysOnlyTighten:
    def test_sending_back_the_policy_unchanged_is_fine(self, world):
        policy = world.full.get("/api/v1/autonomy/").json()
        policy.pop("account_overrides")
        policy["publish_level"] = "off"
        response = patch_json(world.full, "/api/v1/autonomy/", policy)
        assert response.status_code == 200, response.content
        assert response.json()["publish_level"] == "off"

    def test_a_bad_level_is_a_422_not_a_crash(self, world):
        assert patch_json(world.full, "/api/v1/autonomy/", {"publish_level": "yolo"}).status_code == 422

    def test_stricter_checks_are_allowed_looser_ones_are_not(self, world):
        assert patch_json(world.full, "/api/v1/autonomy/", {"undo_delay_minutes": 30}).status_code == 200
        assert patch_json(world.full, "/api/v1/autonomy/", {"undo_delay_minutes": 1}).status_code == 403
        assert patch_json(world.full, "/api/v1/autonomy/", {"max_posts_per_day": 1}).status_code == 200
        assert patch_json(world.full, "/api/v1/autonomy/", {"max_posts_per_day": 50}).status_code == 403
        assert patch_json(world.full, "/api/v1/autonomy/", {"client_visible": True}).status_code == 403


# ---------------------------------------------------------------------------
# Crisis pause and the level are asked again at the moment of sending
# ---------------------------------------------------------------------------


def _due_agent_post(world, **policy):
    set_policy(world.workspace, publish_level="full_auto", undo_delay_minutes=0)
    when = (timezone.now() - timedelta(minutes=1)).isoformat()
    body = schedule_via_rest(world.full, world.account1, when=when).json()
    assert body["autonomy"]["decision"] == "executed"
    if policy:
        set_policy(world.workspace, **policy)
    return PlatformPost.objects.select_related("post__workspace", "social_account").get(post_id=body["id"])


class TestPublisherAsksAgainAtSendTime:
    def test_a_level_lowered_after_scheduling_holds_the_post_for_a_person(self, world):
        from apps.publisher.engine import PublishEngine

        pp = _due_agent_post(world, publish_level="draft_only")
        with patch.object(PublishEngine, "_publish_platform_post") as publish:
            assert PublishEngine()._publish_post_group(pp.post, [pp]) is False
        publish.assert_not_called()
        assert PlatformPost.objects.get(pk=pp.pk).status == "draft"
        held = the_action(workspace=world.workspace, operation="publish.held_at_send")
        assert held.decision == "held" and "Draft only" in held.summary and held.requested_at

        # A person approving it is the latest word: it goes out next time even at Draft only.
        release.approve_held(held, world.owner)
        pp = PlatformPost.objects.select_related("post__workspace", "social_account").get(pk=pp.pk)
        assert pp.status == "scheduled" and release.send_blocker(pp) == ""

    def test_an_account_override_down_to_off_holds_it_too(self, world):
        from apps.autonomy.models import AccountAutonomyOverride

        pp = _due_agent_post(world)
        AccountAutonomyOverride.objects.create(
            workspace=world.workspace, social_account=world.account1, publish_level="off"
        )
        assert "this account" in release.send_blocker(pp)

    def test_a_post_a_person_scheduled_is_never_stopped_by_the_level(self, world):
        post = make_post(world.workspace, world.owner, [world.account1], status="scheduled")
        assert release.send_blocker(PlatformPost.objects.get(post=post)) == ""


class TestRepeatingCopiesFollowTheLevel:
    def series(self, world, client=None):
        set_policy(world.workspace, publish_level="full_auto", undo_delay_minutes=0)
        body = post_json(
            world.full,
            "/api/v1/posts/",
            {
                "social_account_id": str(world.account1.id),
                "caption": "Weekly tip",
                "action": "schedule",
                "scheduled_at": (timezone.now() + timedelta(hours=2)).isoformat(),
                "recurrence": {"frequency": "weekly", "count": 3},
            },
        ).json()
        copy = PlatformPost.objects.filter(post__recurrence_source_id=body["id"]).select_related("post").first()
        assert copy is not None and copy.status == "scheduled"
        return copy

    def test_a_copy_of_an_agents_series_is_held_once_the_level_drops(self, world):
        copy = self.series(world)
        assert release.send_blocker(copy) == ""
        set_policy(world.workspace, publish_level="draft_only")
        assert "Draft only" in release.send_blocker(copy)

    def test_a_copy_of_a_persons_series_keeps_going(self, world):
        from apps.calendar.services import save_recurrence

        post = make_post(world.workspace, world.owner, [world.account1], status="scheduled")
        save_recurrence(post, frequency="weekly", count=3)
        copy = PlatformPost.objects.filter(post__recurrence_source=post).first()
        assert copy is not None
        assert release.send_blocker(copy) == ""  # the default Draft only doesn't stop a person's series
        set_policy(world.workspace, crisis_paused=True)
        assert PlatformPost.objects.filter(pk=copy.pk).filter(release.hold_exclusion()).exists()


class TestThreadsPauseBetweenParts:
    def thread(self, world):
        from apps.composer.models import PostPart

        set_policy(world.workspace, publish_level="full_auto", undo_delay_minutes=0)
        body = schedule_via_rest(world.full, world.account1).json()
        pp = PlatformPost.objects.get(post_id=body["id"])
        PlatformPost.objects.filter(pk=pp.pk).update(status="published", platform_post_id="root-1")
        parts = [PostPart.objects.create(post=pp.post, position=i, caption=f"part {i}") for i in (1, 2)]
        return PlatformPost.objects.get(pk=pp.pk), parts

    def run_part(self, pp, part):
        from apps.publisher.engine import _publish_thread_part_task

        provider = MagicMock()
        with (
            patch("apps.publisher.engine._provider_and_access_token", return_value=(provider, "token")),
            patch("apps.publisher.engine._publish_thread_part_task"),
        ):
            _publish_thread_part_task.task_function(str(pp.id), str(part.id))
        return provider

    def test_crisis_pause_stops_the_remaining_parts_and_a_person_can_resume(self, world):
        pp, parts = self.thread(world)
        set_policy(world.workspace, crisis_paused=True)
        provider = self.run_part(pp, parts[0])
        provider.publish_thread_part.assert_not_called()
        pp.refresh_from_db()
        assert pp.thread_status == PlatformPost.ThreadStatus.PARTIAL and "paused" in pp.thread_error
        paused = the_action(workspace=world.workspace, operation="thread.paused")
        assert paused.decision == "held" and "crisis pause" in paused.summary

        with patch("apps.publisher.engine._publish_thread_part_task") as queued:
            release.approve_held(paused, world.owner)
        queued.assert_called_once()
        pp.refresh_from_db()
        assert pp.thread_status == PlatformPost.ThreadStatus.PENDING

    def test_resuming_from_the_composer_resolves_the_paused_entry(self, world):
        from django.test import Client
        from django.urls import reverse

        pp, parts = self.thread(world)
        set_policy(world.workspace, crisis_paused=True)
        self.run_part(pp, parts[0])
        paused = the_action(workspace=world.workspace, operation="thread.paused")
        assert paused.decision == "held" and paused.review_outcome == ""

        set_policy(world.workspace, crisis_paused=False)
        client = Client()
        client.force_login(world.owner)
        url = reverse(
            "composer:resume_thread",
            kwargs={"workspace_id": world.workspace.id, "post_id": pp.post_id, "platform_post_id": pp.id},
        )
        with patch("apps.publisher.engine._publish_thread_part_task"):
            assert client.post(url, HTTP_HX_REQUEST="true").status_code == 204
        paused.refresh_from_db()
        assert paused.review_outcome == "approved" and paused.reviewed_by == world.owner and paused.reviewed_at

    def test_resuming_through_the_rest_api_resolves_the_paused_entry(self, world):
        pp, parts = self.thread(world)
        set_policy(world.workspace, crisis_paused=True)
        self.run_part(pp, parts[0])
        paused = the_action(workspace=world.workspace, operation="thread.paused")

        set_policy(world.workspace, crisis_paused=False)
        with patch("apps.publisher.engine._publish_thread_part_task"):
            response = world.full.post(f"/api/v1/posts/{pp.post_id}/resume-thread")
        assert response.status_code == 200, response.content
        paused.refresh_from_db()
        assert paused.review_outcome == "approved" and paused.reviewed_at
        assert paused.reviewed_by == world.owner  # the person who issued the key

    def test_resuming_through_mcp_resolves_the_paused_entry(self, world):
        import json

        pp, parts = self.thread(world)
        set_policy(world.workspace, crisis_paused=True)
        self.run_part(pp, parts[0])
        paused = the_action(workspace=world.workspace, operation="thread.paused")

        set_policy(world.workspace, crisis_paused=False)
        call = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "resume_thread", "arguments": {"post_id": str(pp.post_id)}},
        }
        with patch("apps.publisher.engine._publish_thread_part_task"):
            response = post_json(world.full, "/api/v1/mcp/", call)
        assert response.status_code == 200 and "error" not in response.json(), response.content
        paused.refresh_from_db()
        assert paused.review_outcome == "approved" and paused.reviewed_by == world.owner
        assert json.loads(response.json()["result"]["content"][0]["text"])["id"] == str(pp.post_id)

    def test_a_refused_api_resume_leaves_the_paused_entry_waiting(self, world):
        pp, parts = self.thread(world)
        set_policy(world.workspace, crisis_paused=True)
        self.run_part(pp, parts[0])
        paused = the_action(workspace=world.workspace, operation="thread.paused")
        # Still in a crisis pause: the agent may not publish, so the gate refuses.
        assert world.full.post(f"/api/v1/posts/{pp.post_id}/resume-thread").status_code == 403
        paused.refresh_from_db()
        assert paused.review_outcome == ""

    def test_a_refused_composer_resume_leaves_the_paused_entry_waiting(self, world):
        from django.test import Client
        from django.urls import reverse

        pp, parts = self.thread(world)
        set_policy(world.workspace, crisis_paused=True)
        self.run_part(pp, parts[0])
        paused = the_action(workspace=world.workspace, operation="thread.paused")
        PlatformPost.objects.filter(pk=pp.pk).update(thread_status=PlatformPost.ThreadStatus.COMPLETE)
        client = Client()
        client.force_login(world.owner)
        url = reverse(
            "composer:resume_thread",
            kwargs={"workspace_id": world.workspace.id, "post_id": pp.post_id, "platform_post_id": pp.id},
        )
        assert client.post(url, HTTP_HX_REQUEST="true").status_code == 400
        paused.refresh_from_db()
        assert paused.review_outcome == ""

    def test_a_level_lowered_mid_thread_pauses_it(self, world):
        pp, parts = self.thread(world)
        set_policy(world.workspace, publish_level="draft_only")
        self.run_part(pp, parts[0]).publish_thread_part.assert_not_called()
        assert the_action(workspace=world.workspace, operation="thread.paused").decision == "held"


class TestDraftOnlyFlagsBlockedPhrases:
    def test_a_draft_with_a_blocked_phrase_is_saved_and_flagged(self, world):
        set_policy(world.workspace, blocked_phrases=["guaranteed cure"])
        response = post_json(
            world.full,
            "/api/v1/posts/",
            {"social_account_id": str(world.account1.id), "caption": "A guaranteed cure!", "action": "draft"},
        )
        assert response.status_code == 201
        notice = response.json()["autonomy"]
        assert notice["decision"] == "executed" and "blocked phrase" in notice["message"]
        entry = the_action(workspace=world.workspace)
        assert "flagged" in entry.summary
        assert [c["name"] for c in entry.guardrail_results if not c["passed"]] == ["blocked_phrases"]

    def test_a_held_schedule_carries_the_flag_too(self, world):
        set_policy(world.workspace, blocked_phrases=["guaranteed cure"])
        body = schedule_via_rest(world.full, world.account1, "A guaranteed cure!").json()
        assert body["autonomy"]["decision"] == "held"
        assert any("blocked phrase" in r for r in body["autonomy"]["reasons"])


class TestReviewRepliesFollowTheReplyPolicy:
    def review(self, world):
        return InboxMessage.objects.create(
            workspace=world.workspace,
            social_account=world.account1,
            platform_message_id="r-1",
            message_type=InboxMessage.MessageType.REVIEW,
            sender_name="Ada",
            body="Lovely place",
            received_at=timezone.now(),
        )

    def test_an_unattended_review_reply_is_refused_when_replies_need_a_person(self, world):
        from apps.inbox.reply_policy import review_reply_allowed

        message = self.review(world)
        allowed, reason = review_reply_allowed(message)
        assert not allowed and "Draft only" in reason
        set_policy(world.workspace, reply_level="auto_guardrails")
        assert review_reply_allowed(message) == (True, "")
        set_policy(world.workspace, crisis_paused=True)
        assert "crisis pause" in review_reply_allowed(message)[1]

    def test_a_person_or_the_web_inbox_may_always_reply(self, world):
        from apps.inbox.reply_policy import WEB_INBOX, review_reply_allowed

        message = self.review(world)
        assert review_reply_allowed(message, origin=WEB_INBOX)[0]
        assert review_reply_allowed(message, actor=world.owner)[0]
