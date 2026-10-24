"""Policy resolution, each level for each action type, and each guardrail."""

from __future__ import annotations

import datetime as dt
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.autonomy import guardrails, service
from apps.autonomy.actors import agent, human, system
from apps.autonomy.models import ActionType, AgentAction, Decision
from apps.autonomy.policy import get_policy, resolve_level

from .conftest import set_override, set_policy

pytestmark = pytest.mark.django_db

AGENT = system("Test agent")


def evaluate(world, action_type, text="A calm update", account=None, **kwargs):
    return service.evaluate(
        workspace=world.workspace,
        actor=kwargs.pop("actor", AGENT),
        action_type=action_type,
        account=account or world.account1,
        text=text,
        **kwargs,
    )


class TestResolution:
    def test_a_new_workspace_gets_the_conservative_defaults(self, world):
        policy = get_policy(world.workspace)
        assert (policy.draft_level, policy.publish_level, policy.reply_level) == ("draft_only",) * 3
        assert policy.recycle_level == "per_item"
        assert policy.undo_delay_minutes == 5 and not policy.crisis_paused
        assert not AgentAction.objects.exists()  # reading never writes

    def test_workspace_level_applies_without_an_override(self, world):
        set_policy(world.workspace, publish_level="auto_guardrails")
        assert resolve_level(world.workspace, world.account1, ActionType.PUBLISH_POSTS) == (
            "auto_guardrails",
            "workspace",
        )

    def test_an_account_override_wins_for_that_account_only(self, world):
        set_policy(world.workspace, publish_level="draft_only", reply_level="off")
        set_override(world.workspace, world.account1, publish_level="full_auto")
        assert resolve_level(world.workspace, world.account1, ActionType.PUBLISH_POSTS) == ("full_auto", "account")
        assert resolve_level(world.workspace, world.account2, ActionType.PUBLISH_POSTS) == ("draft_only", "workspace")
        # A blank field in the override still follows the workspace.
        assert resolve_level(world.workspace, world.account1, ActionType.REPLY) == ("off", "workspace")

    def test_a_person_is_never_held_back(self, world):
        set_policy(world.workspace, publish_level="off", crisis_paused=True)
        verdict = evaluate(world, ActionType.PUBLISH_POSTS, actor=human(world.owner), text="buy now $5 off")
        assert verdict.executes and verdict.results == []


class TestLevels:
    @pytest.mark.parametrize(
        ("action_type", "off", "draft_only"),
        [
            (ActionType.DRAFT_CONTENT, "block", "execute"),
            (ActionType.PUBLISH_POSTS, "block", "hold"),
            (ActionType.REPLY, "block", "hold"),
            (ActionType.RECYCLE_EVERGREEN, "block", "hold"),
        ],
    )
    def test_off_and_draft_only(self, world, action_type, off, draft_only):
        field = {
            ActionType.DRAFT_CONTENT: "draft_level",
            ActionType.PUBLISH_POSTS: "publish_level",
            ActionType.REPLY: "reply_level",
            ActionType.RECYCLE_EVERGREEN: "recycle_level",
        }[action_type]
        set_policy(world.workspace, **{field: "off"})
        assert evaluate(world, action_type).outcome == off
        set_policy(world.workspace, **{field: "draft_only"})
        assert evaluate(world, action_type).outcome == draft_only

    @pytest.mark.parametrize("action_type", [ActionType.PUBLISH_POSTS, ActionType.REPLY, ActionType.RECYCLE_EVERGREEN])
    def test_auto_with_guardrails_executes_clean_text_and_holds_flagged_text(self, world, action_type):
        field = {
            ActionType.PUBLISH_POSTS: "publish_level",
            ActionType.REPLY: "reply_level",
            ActionType.RECYCLE_EVERGREEN: "recycle_level",
        }[action_type]
        set_policy(world.workspace, **{field: "auto_guardrails"})
        clean = evaluate(world, action_type)
        assert clean.executes and clean.undo_delay == dt.timedelta(minutes=5)
        flagged = evaluate(world, action_type, text="Details at https://example.com/offer")
        assert flagged.held and "contains a link" in flagged.reason_text
        assert flagged.agent_message.startswith("Held for review because")

    def test_full_auto_skips_the_guardrails_but_not_the_blocked_phrases(self, world):
        set_policy(world.workspace, publish_level="full_auto", blocked_phrases=["lawsuit-bait"])
        assert evaluate(world, ActionType.PUBLISH_POSTS, text="50% off at https://x.com @everyone").executes
        blocked = evaluate(world, ActionType.PUBLISH_POSTS, text="Pure LAWSUIT-BAIT here")
        assert blocked.blocked and "lawsuit-bait" in blocked.reason_text

    def test_a_draft_that_trips_a_guardrail_is_flagged_not_refused(self, world):
        set_policy(world.workspace, draft_level="auto_guardrails")
        verdict = evaluate(world, ActionType.DRAFT_CONTENT, text="Save $20 today")
        assert verdict.held and "price" in verdict.reason_text

    def test_recycling_follows_each_items_own_setting_by_default(self, world):
        verdict = evaluate(world, ActionType.RECYCLE_EVERGREEN)
        assert verdict.executes and verdict.defer_to_item


class TestGuardrails:
    @pytest.fixture(autouse=True)
    def auto(self, world):
        set_policy(world.workspace, publish_level="auto_guardrails", reply_level="auto_guardrails")

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Read more at www.example.com/blog", "contains a link"),
            ("Thanks @someone for the tip", "mentions another account"),
            ("Everything is $25 this week", "price or discount"),
            ("20% off everything", "price or discount"),
            ("This tea cures insomnia", "health claim"),
            ("Guaranteed returns on your savings", "financial claim"),
            ("Get legal advice from us", "legal claim"),
        ],
    )
    def test_each_content_classifier(self, world, text, expected):
        verdict = evaluate(world, ActionType.PUBLISH_POSTS, text=text)
        assert verdict.held and expected in verdict.reason_text

    def test_an_email_address_is_not_a_mention(self, world):
        assert not guardrails.CLASSIFIERS[1].pattern.search("write to hello@brand.com")

    def test_a_classifier_can_be_switched_off(self, world):
        set_policy(world.workspace, review_links=False)
        assert evaluate(world, ActionType.PUBLISH_POSTS, text="see https://example.com").executes

    def test_classifiers_are_extensible(self, world):
        import re

        original = list(guardrails.CLASSIFIERS)
        try:
            guardrails.register_classifier(
                guardrails.Classifier(
                    "rivals", "Rivals", "review_links", re.compile(r"\bacme\b", re.I), "names a rival"
                )
            )
            verdict = evaluate(world, ActionType.PUBLISH_POSTS, text="Better than Acme")
            assert verdict.held and "names a rival" in verdict.reason_text
        finally:
            guardrails.CLASSIFIERS[:] = original

    def test_blocked_phrases_refuse_whole_words_only(self, world):
        set_policy(world.workspace, blocked_phrases=["cheap", "free trial"])
        assert evaluate(world, ActionType.PUBLISH_POSTS, text="A cheap trick").blocked
        assert evaluate(world, ActionType.PUBLISH_POSTS, text="Start your FREE   trial").executes is False
        assert evaluate(world, ActionType.PUBLISH_POSTS, text="Cheapskate approved").executes

    def test_the_daily_limit_counts_autonomous_posts_for_that_account_and_day(self, world):
        set_policy(world.workspace, max_posts_per_day=2)
        when = timezone.now() + dt.timedelta(days=1)
        for _ in range(2):
            AgentAction.objects.create(
                workspace=world.workspace,
                social_account=world.account1,
                actor_type="agent",
                actor_name="bot",
                action_type=ActionType.PUBLISH_POSTS,
                operation="post.schedule",
                decision=Decision.EXECUTED,
                requested_at=when,
            )
        capped = evaluate(world, ActionType.PUBLISH_POSTS, when=when)
        assert capped.held and "limit 2" in capped.reason_text
        assert evaluate(world, ActionType.PUBLISH_POSTS, account=world.account2, when=when).executes
        assert evaluate(world, ActionType.PUBLISH_POSTS, when=when + dt.timedelta(days=1)).executes
        AgentAction.objects.update(undone_at=timezone.now())  # an undone post no longer counts
        assert evaluate(world, ActionType.PUBLISH_POSTS, when=when).executes

    def test_an_account_can_have_its_own_daily_limit(self, world):
        set_policy(world.workspace, max_posts_per_day=5)
        set_override(world.workspace, world.account1, max_posts_per_day=0)
        assert evaluate(world, ActionType.PUBLISH_POSTS).held

    def test_quiet_hours_hold_what_would_go_out_then_including_across_midnight(self, world):
        set_policy(world.workspace, quiet_hours_enabled=True, quiet_start=dt.time(22, 0), quiet_end=dt.time(7, 0))
        base = (timezone.now() + dt.timedelta(days=2)).replace(minute=0, second=0, microsecond=0)
        night = base.replace(hour=23)
        early = base.replace(hour=3)
        noon = base.replace(hour=12)
        assert evaluate(world, ActionType.PUBLISH_POSTS, when=night).held
        assert evaluate(world, ActionType.PUBLISH_POSTS, when=early).held
        assert "quiet hours" in evaluate(world, ActionType.PUBLISH_POSTS, when=night).reason_text
        assert evaluate(world, ActionType.PUBLISH_POSTS, when=noon).executes

    def test_ai_risk_check_holds_a_risky_score_and_is_counted_by_the_ai_log(self, world, settings):
        from apps.ai_writer.models import AIRequestLog
        from apps.ai_writer.tests.fakes import patch_anthropic

        settings.ANTHROPIC_API_KEY = "sk-ant-platform-key-for-tests"
        set_policy(world.workspace, ai_risk_check=True, ai_risk_threshold=0.5)
        with patch_anthropic() as fake:
            fake.queue(
                {"risk_score": 0.82, "risk_reasons": ["Unverifiable claim"], "voice_drift": 0, "voice_reasons": []},
                {"risk_score": 0.1, "risk_reasons": [], "voice_drift": 0, "voice_reasons": []},
            )
            risky = evaluate(world, ActionType.PUBLISH_POSTS, text="Our product is the best on earth")
            fine = evaluate(world, ActionType.PUBLISH_POSTS, text="Open until six today")
        assert risky.held and risky.ai_used and risky.risk_score == pytest.approx(0.82)
        assert "Unverifiable claim" in risky.reason_text
        assert fine.executes and fine.risk_score == pytest.approx(0.1)
        assert AIRequestLog.objects.filter(action="review").count() == 2  # counted against the AI limits

    def test_brand_voice_drift_holds(self, world):
        set_policy(world.workspace, ai_voice_check=True, ai_voice_threshold=0.4)
        review = {"risk_score": 0.0, "risk_reasons": [], "voice_drift": 0.7, "voice_reasons": ["Too formal"]}
        with patch("apps.ai_writer.services.review_content", return_value=review) as call:
            verdict = evaluate(world, ActionType.PUBLISH_POSTS, text="Kindly be advised of our offerings")
        assert call.call_args.kwargs["check_voice"] is True
        assert verdict.held and "Too formal" in verdict.reason_text

    def test_an_ai_check_that_cannot_run_holds_instead_of_letting_it_through(self, world):
        from apps.ai_writer.client import AINotConfiguredError

        set_policy(world.workspace, ai_risk_check=True)
        with patch("apps.ai_writer.services.review_content", side_effect=AINotConfiguredError()):
            verdict = evaluate(world, ActionType.PUBLISH_POSTS)
        assert verdict.held and "could not run" in verdict.reason_text

    def test_no_ai_is_called_unless_a_check_is_switched_on(self, world):
        with patch("apps.ai_writer.services.review_content") as call:
            assert evaluate(world, ActionType.PUBLISH_POSTS).executes
        call.assert_not_called()


class TestCrisisPause:
    def test_it_stops_drafting_and_recycling_and_holds_publishing_and_replies(self, world):
        set_policy(
            world.workspace,
            draft_level="full_auto",
            publish_level="full_auto",
            reply_level="full_auto",
            recycle_level="full_auto",
            crisis_paused=True,
        )
        assert evaluate(world, ActionType.DRAFT_CONTENT).blocked
        assert evaluate(world, ActionType.RECYCLE_EVERGREEN).blocked
        publish = evaluate(world, ActionType.PUBLISH_POSTS)
        assert publish.held and "crisis pause" in publish.reason_text
        assert evaluate(world, ActionType.REPLY).held
        assert service.recycling_stopped(world.workspace, world.account1) == "crisis pause is on"

    def test_agents_are_told_what_to_do_next(self, world):
        set_policy(world.workspace, publish_level="auto_guardrails")
        verdict = evaluate(world, ActionType.PUBLISH_POSTS, text="$5 off")
        assert "saved as a draft" in verdict.agent_message
        set_policy(world.workspace, publish_level="off")
        assert "Nothing was saved" in evaluate(world, ActionType.PUBLISH_POSTS).agent_message


def test_actor_names_identify_keys_and_oauth_sessions(world):
    from apps.api_keys.models import ApiKey

    key = ApiKey.objects.get(name="full")
    info = agent(key)
    assert (info.type, info.name, info.api_key) == ("agent", "full", key)

    class Oauth:
        is_oauth = True
        issued_by = world.owner

    session = agent(Oauth())
    assert session.type == "agent" and session.name.startswith("MCP session") and session.api_key is None
