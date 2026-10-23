"""The holes closed in review: write paths that skipped the policy, evasions, races and held repeats."""

from __future__ import annotations

import datetime as dt
from datetime import timedelta
from unittest.mock import patch

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
