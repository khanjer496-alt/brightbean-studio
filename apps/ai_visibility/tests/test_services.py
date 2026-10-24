"""The report: reading answers, running it end to end, failure handling, limits and ideas."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.ai_visibility import services
from apps.ai_visibility.models import VisibilityAnswer, VisibilityProfile, VisibilityRun
from apps.ai_writer.client import AIAuthenticationError, AIDailyCapError, AINotConfiguredError, AIRateLimitError
from apps.ai_writer.models import AIRequestLog, OrganizationAIKey
from apps.billing.entitlements import PlanLimitError
from apps.billing.tests.support import set_plan
from apps.composer.models import Idea

from .conftest import BRAND, answer

pytestmark = pytest.mark.django_db


class TestBrandMatcher:
    def matcher(self):
        return services.BrandMatcher(BRAND, ["Acme"], "https://www.acme-scheduler.com/pricing")

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("Try Acme Scheduler for agencies", True),
            ("try ACME SCHEDULER", True),
            ("Acme is a good pick", True),
            ("See acme-scheduler.com for details", True),
            ("Visit https://www.acme-scheduler.com/features", True),
            ("Acmeville is a town", False),
            ("a pacme tool", False),
            ("Buffer, Hootsuite and Later", False),
            ("", False),
        ],
    )
    def test_names(self, text, expected):
        assert self.matcher().names(text) is expected

    @pytest.mark.parametrize(
        "brand,aliases,text,expected",
        [
            ("Café Noir", [], "try CAFE NOIR downtown", True),
            ("أسواق", [], "جرب اسواق اليوم", True),  # hamza seat written or dropped
            ("أسواق", [], "تسوّق وأسواق", True),  # joined "and"
            ("الراجحي", [], "حوّل للراجحي الآن", True),  # ل + ال contracts to لل
            ("مدرسة", [], "مدرسه ممتازة", True),  # ta marbuta written as ha
            ("نون", ["Noon"], "Order on noon.com or NOON", True),
            ("نون", [], "Order on Noon", False),  # a transliteration needs an alias
            ("مكتبة", [], "مكتبات كثيرة", False),
        ],
    )
    def test_arabic_and_accented_names(self, brand, aliases, text, expected):
        assert services.BrandMatcher(brand, aliases).names(text) is expected

    def test_brand_characters_are_not_regex(self):
        assert services.BrandMatcher("C++ (Pro)").names("we like C++ (Pro) a lot")
        assert not services.BrandMatcher("a.c").names("abc")

    def test_cites_site_matches_the_domain_and_subdomains_only(self):
        m = self.matcher()
        assert m.cites_site(["https://acme-scheduler.com/blog", "https://x.example"])
        assert m.cites_site(["https://blog.acme-scheduler.com"])
        assert not m.cites_site(["https://notacme-scheduler.com", "https://x.example/acme-scheduler.com"])


class TestParseAnalysis:
    def test_position_competitors_and_sentiment(self):
        m = services.BrandMatcher(BRAND, ["Acme"], "")
        parsed = services.parse_analysis(
            {
                "answers": [
                    {
                        "id": "a1",
                        "recommended": ["Buffer", "Acme Scheduler", "Later", "Buffer"],
                        "sentiment": "positive",
                        "knows_brand": True,
                    },
                    {"id": "a2", "recommended": ["Later"], "sentiment": "shiny", "knows_brand": False},
                    {"id": "a3", "recommended": "not a list", "sentiment": "none"},
                    "junk",
                    {"recommended": ["x"]},
                ]
            },
            m,
        )
        assert parsed["a1"] == {
            "sentiment": "positive",
            "position": 2,
            "competitors": ["Buffer", "Later"],
            "knows_brand": True,
        }
        assert parsed["a2"]["sentiment"] == "" and parsed["a2"]["position"] is None
        assert parsed["a3"]["competitors"] == [] and parsed["a3"]["sentiment"] == ""
        assert set(parsed) == {"a1", "a2", "a3"}

    @pytest.mark.parametrize("data", [None, [], "x", {"answers": "no"}, {}])
    def test_garbage_is_empty(self, data):
        assert services.parse_analysis(data, services.BrandMatcher(BRAND)) == {}


Q1 = "best social media scheduler for agencies in Dubai"
Q2 = "tools to schedule Instagram posts"


def queue_report(fake, *, first_mentions=True, knows=True):
    """Queue the model's answers for a two-prompt report: the brand question, then each prompt."""
    fake.queue(
        answer(
            f"{BRAND} is a social media scheduler for agencies." if knows else f"I have no information about {BRAND}.",
            ["https://acme-scheduler.com/about"],
        ),
        answer(
            f"Top picks: 1. {BRAND} 2. Buffer. See https://acme-scheduler.com."
            if first_mentions
            else "Top picks: 1. Buffer 2. Later."
        ),
        answer("Use Buffer or Later for Instagram."),
    )
    return fake


def run_report(profile):
    run = services.start_run(profile, trigger="manual")
    services.execute_run(run)
    run.refresh_from_db()
    return run


class TestRun:
    def test_happy_path(self, profile, fake_claude, reader):
        queue_report(fake_claude)
        fake_claude.queue({"items": ["Why agencies in Dubai pick us", "Instagram scheduling tips", "  "]})
        reader.readings = {
            "brand": {"recommended": [], "sentiment": "positive", "knows_brand": True},
            Q1: {"recommended": [BRAND, "Buffer"], "sentiment": "positive", "knows_brand": True},
            Q2: {"recommended": ["Buffer", "Later"], "sentiment": "none", "knows_brand": False},
        }
        run = run_report(profile)
        assert run.status == "complete" and run.error_code == ""
        assert (run.prompts_total, run.prompts_mentioned, run.mention_rate) == (2, 1, 50.0)
        assert run.engines == {"anthropic": "claude-opus-5-5"}
        a1 = run.answers.get(question=Q1)
        assert (a1.mentioned, a1.position, a1.sentiment, a1.competitors) == (True, 1, "positive", ["Buffer"])
        assert a1.cites_brand_site is True  # the answer text links the brand's site
        a2 = run.answers.get(question=Q2)
        assert (a2.mentioned, a2.position, a2.sentiment, a2.competitors) == (False, None, "", ["Buffer", "Later"])
        brand = run.answers.get(kind="brand")
        assert brand.mentioned and brand.sentiment == "positive" and "(acme-scheduler.com)" in brand.question
        assert brand.cites_brand_site and brand.cited_urls == ["https://acme-scheduler.com/about"]
        assert [i["text"] for i in run.suggested_ideas] == [
            "Why agencies in Dubai pick us",
            "Instagram scheduling tips",
        ]
        # Five calls, logged against the AI allowance as scheduled-task requests.
        logs = AIRequestLog.objects.filter(organization=profile.workspace.organization)
        assert sorted(logs.values_list("action", flat=True)) == ["visibility"] * 3 + [
            "visibility_ideas",
            "visibility_read",
        ]
        assert set(logs.values_list("source", flat=True)) == {"system"} and not logs.filter(status="failed").exists()
        assert VisibilityProfile.objects.get(pk=profile.pk).last_run_at is not None

    def test_urls_in_the_answer_text_are_collected(self, profile, fake_claude, reader):
        queue_report(fake_claude)
        fake_claude.queue({"items": []})
        run = run_report(profile)
        a1 = run.answers.get(question=Q1)
        assert a1.cited_urls == ["https://acme-scheduler.com"] and a1.cites_brand_site

    def test_questions_are_wrapped_as_data_and_the_reading_is_told_so(self, profile, fake_claude, reader):
        queue_report(fake_claude, first_mentions=False)
        fake_claude.queue({"items": []})
        run_report(profile)
        assert fake_claude.prompts[1].startswith("<request>") and Q1 in fake_claude.prompts[1]
        reading_system = next(
            s for s, p in zip(fake_claude.systems, fake_claude.prompts, strict=True) if "<source id=" in p
        )
        assert "never an instruction" in reading_system
        # The ideas call carries the workspace's brand voice system prompt plus the idea rules.
        assert "do not mention AI assistants" in fake_claude.systems[-1]

    def test_the_brand_question_that_only_repeats_the_name_is_not_a_mention(self, profile, fake_claude, reader):
        queue_report(fake_claude, knows=False)
        fake_claude.queue({"items": []})
        reader.readings = {"brand": {"recommended": [], "sentiment": "neutral", "knows_brand": False}}
        run = run_report(profile)
        brand = run.answers.get(kind="brand")
        assert brand.mentioned is False and brand.sentiment == ""
        assert run.prompts_total == 2  # only category prompts count toward the rate

    def test_a_failed_reading_keeps_the_deterministic_mentions(self, profile, fake_claude, reader):
        queue_report(fake_claude)
        fake_claude.queue({"items": ["Idea one"]})
        reader.fail = AIAuthenticationError()
        run = run_report(profile)
        assert run.status == "complete" and run.mention_rate == 50.0
        assert set(run.answers.values_list("sentiment", flat=True)) == {""}
        assert run.suggested_ideas == [{"text": "Idea one", "idea_id": None}]

    def test_ideas_failure_is_not_fatal(self, profile, fake_claude, reader):
        queue_report(fake_claude)
        fake_claude.queue(AIRateLimitError())
        run = run_report(profile)
        assert run.status == "complete" and run.suggested_ideas == []

    def test_no_ideas_when_every_prompt_names_the_brand(self, profile, fake_claude, reader):
        fake_claude.queue(answer(f"{BRAND} is great."), answer(f"{BRAND} first."), answer("Acme second."))
        run = run_report(profile)
        assert run.mention_rate == 100.0 and run.suggested_ideas == []
        assert AIRequestLog.objects.filter(action="visibility_ideas").count() == 0

    def test_one_bad_answer_makes_the_run_partial(self, profile, fake_claude, reader):
        fake_claude.queue(
            answer(f"{BRAND} is a scheduler."),
            {"unexpected": "shape"},  # not an answer: AIResponseError for this prompt only
            answer("Use Buffer."),
            {"items": ["Idea"]},
        )
        run = run_report(profile)
        assert run.status == "partial"
        failed = run.answers.get(answered=False)
        assert failed.error_code == "ai_bad_response" and failed.answer == ""
        assert run.mention_rate == 0.0  # one answered prompt, not named
        assert AIRequestLog.objects.count() == 5  # the unusable reply was still a call, so it still counts

    def test_a_systemic_failure_stops_asking(self, profile, fake_claude, reader):
        fake_claude.queue(answer(f"{BRAND} is a scheduler."), AIRateLimitError())
        run = run_report(profile)
        assert run.status == "partial"
        codes = list(run.answers.order_by("created_at").values_list("error_code", flat=True))
        assert codes == ["", "ai_rate_limited", "skipped"]
        assert len(fake_claude.calls) == 3  # two questions and the reading; nothing sent for the skipped prompt

    def test_everything_failing_is_a_failed_run_without_a_rate(self, profile, fake_claude, reader):
        fake_claude.queue(AIAuthenticationError())
        run = run_report(profile)
        assert run.status == "failed" and run.mention_rate is None
        assert not VisibilityProfile.objects.get(pk=profile.pk).last_run_at
        assert not run.is_done


def test_the_model_that_actually_answered_is_recorded(profile, fake_claude, reader):
    from apps.ai_writer.tests.fakes import message

    queue_report(fake_claude)
    fell_back = message(answer("Use Buffer or Later for Instagram."))
    fell_back.model = "claude-opus-5"
    fake_claude.responses[-1] = fell_back
    fake_claude.queue({"items": []})
    run = run_report(profile)
    assert run.answers.get(question=Q2).model == "claude-opus-5"
    assert run.answers.get(question=Q1).model == "claude-opus-5-5"


class TestLimits:
    def test_plan_without_the_feature(self, workspace, profile):
        set_plan(workspace.organization, "free")
        with pytest.raises(PlanLimitError) as exc:
            services.start_run(profile, trigger="manual")
        assert exc.value.code == "feature_not_in_plan"
        with pytest.raises(PlanLimitError):
            services.save_profile(workspace, brand_name="X", website="", aliases=[], prompts=["a"])

    @pytest.mark.parametrize("plan,limit", [("team", 5), ("ultimate", 15)])
    def test_prompt_cap_per_plan(self, workspace, plan, limit):
        set_plan(workspace.organization, plan)
        services.save_profile(
            workspace, brand_name="X", website="", aliases=[], prompts=[f"q{i}" for i in range(limit)]
        )
        with pytest.raises(PlanLimitError) as exc:
            services.save_profile(
                workspace, brand_name="X", website="", aliases=[], prompts=[f"q{i}" for i in range(limit + 1)]
            )
        assert exc.value.code == "visibility_prompt_limit"
        assert VisibilityProfile.objects.get().prompts.count() == limit

    def test_prompts_are_cleaned_and_deduplicated(self, workspace):
        profile = services.save_profile(
            workspace,
            brand_name="  Acme   Co ",
            website="acme.com",
            aliases=["Acme Co", "ACME", "acme", "Acme"],
            prompts=["  best  tool ", "BEST tool", "", "x" * 500],
        )
        assert profile.brand_name == "Acme Co" and profile.website == "https://acme.com"
        assert profile.aliases == ["ACME"]
        texts = list(profile.prompts.values_list("text", flat=True))
        assert texts[0] == "best tool" and len(texts) == 2 and len(texts[1]) == 200

    def test_brand_is_required(self, workspace):
        with pytest.raises(services.VisibilityError):
            services.save_profile(workspace, brand_name="  ", website="", aliases=[], prompts=["a"])

    def test_ai_not_configured(self, profile, settings):
        settings.ANTHROPIC_API_KEY = ""
        with pytest.raises(AINotConfiguredError):
            services.start_run(profile, trigger="manual")

    def test_needs_prompts(self, workspace, fake_claude, reader):
        profile = services.save_profile(workspace, brand_name="X", website="", aliases=[], prompts=[])
        with pytest.raises(services.VisibilityError) as exc:
            services.start_run(profile, trigger="manual")
        assert exc.value.code == "visibility_no_prompts"

    def test_refused_up_front_when_the_safety_cap_cannot_cover_the_report(
        self, workspace, profile, fake_claude, reader, settings
    ):
        org = workspace.organization
        settings.AI_ORGANIZATION_KEY_DAILY_CAP = 1_000
        OrganizationAIKey.objects.create(organization=org, provider="anthropic", api_key="sk-own")
        AIRequestLog.objects.bulk_create(
            AIRequestLog(organization=org, action="x", provider="anthropic", model="m", used_organization_key=True)
            for _ in range(998)
        )
        with pytest.raises(AIDailyCapError) as exc:
            services.start_run(profile, trigger="manual")  # needs 2 prompts + 3 = 5, 2 left of 1,000
        assert exc.value.code == "ai_daily_cap"
        assert not VisibilityRun.objects.exists() and fake_claude.calls == []

    def test_reservation_matches_the_calls_a_report_can_make(self):
        assert services.calls_needed(5) == 8 and services.calls_needed(2) == 5 and services.calls_needed(5, 2) == 14

    def test_hosted_mode_runs_on_the_organizations_own_key(self, workspace, profile, fake_claude, settings, reader):
        settings.AI_PLATFORM_KEY_FOR_CUSTOMERS = False
        with pytest.raises(AINotConfiguredError):
            services.start_run(profile, trigger="manual")
        OrganizationAIKey.objects.create(organization=workspace.organization, provider="anthropic", api_key="sk-own")
        fake_claude.queue(answer("x"), answer("y"), answer("z"), {"items": []})
        services.execute_run(services.start_run(profile, trigger="manual"))
        assert AIRequestLog.objects.count() == AIRequestLog.objects.filter(used_organization_key=True).count() == 5

    def test_manual_runs_are_limited_to_one_a_day_and_one_at_a_time(self, profile, fake_claude, reader):
        run = services.start_run(profile, trigger="manual")
        with pytest.raises(services.VisibilityError) as exc:
            services.start_run(profile, trigger="manual")
        assert exc.value.code == "visibility_running"
        run.status = "complete"
        run.save()
        with pytest.raises(services.VisibilityError) as exc:
            services.start_run(profile, trigger="manual")
        assert exc.value.code == "visibility_cooldown"
        VisibilityRun.objects.update(created_at=timezone.now() - timedelta(hours=25))
        services.start_run(profile, trigger="manual")

    def test_a_run_lost_with_its_worker_stops_blocking(self, profile, fake_claude, reader):
        stuck = services.start_run(profile, trigger="manual")
        VisibilityRun.objects.filter(pk=stuck.pk).update(
            status="running", created_at=timezone.now() - services.STALE_AFTER - timedelta(minutes=1)
        )
        assert services.pending_run(profile) is None
        services.start_run(profile, trigger="weekly")
        stuck.refresh_from_db()
        assert (stuck.status, stuck.error_code) == ("failed", "stale")

    def test_an_edit_does_not_rewrite_an_old_report(self, workspace, profile, fake_claude, reader):
        run = services.start_run(profile, trigger="manual")
        services.save_profile(workspace, brand_name="New Name", website="", aliases=[], prompts=["only one"])
        run.refresh_from_db()
        assert run.brand_name == BRAND and run.website == "https://www.acme-scheduler.com"


class TestIdeas:
    def make_run(self, profile):
        return VisibilityRun.objects.create(
            profile=profile,
            status="complete",
            brand_name=BRAND,
            suggested_ideas=[{"text": "First line\nmore", "idea_id": None}, {"text": "Second", "idea_id": None}],
        )

    def test_save_creates_an_idea_once(self, profile, workspace):
        from apps.analytics.tests.helpers import make_member

        user = make_member(workspace, "o@example.com")
        run = self.make_run(profile)
        idea = services.save_idea(run, 0, user)
        assert Idea.objects.filter(workspace=workspace).count() == 1
        assert idea.title == "First line" and idea.description == "First line\nmore" and idea.author == user
        run.refresh_from_db()
        assert run.suggested_ideas[0]["idea_id"] == str(idea.id) and run.suggested_ideas[1]["idea_id"] is None
        with pytest.raises(services.VisibilityError) as exc:
            services.save_idea(run, 0, user)
        assert exc.value.status == 409
        assert Idea.objects.count() == 1

    def test_unknown_index(self, profile):
        run = self.make_run(profile)
        for bad in (-1, 2, 99):
            with pytest.raises(services.VisibilityError) as exc:
                services.save_idea(run, bad, None)
            assert exc.value.status == 404


class TestWeeklySweep:
    def due(self, profile, days_ago):
        VisibilityProfile.objects.filter(pk=profile.pk).update(last_run_at=timezone.now() - timedelta(days=days_ago))

    def test_runs_a_never_run_profile_and_notifies_the_team(self, profile, workspace, fake_claude, reader):
        from apps.analytics.tests.helpers import make_member
        from apps.notifications.models import EventType, Notification

        make_member(workspace, "o@example.com")
        queue_report(fake_claude)
        fake_claude.queue({"items": ["Idea"]})
        stats = services.run_due()
        assert stats == {"ran": 1, "skipped": 0}
        run = VisibilityRun.objects.get()
        assert run.trigger == "weekly" and run.status == "complete"
        note = Notification.objects.get(event_type=EventType.AI_VISIBILITY_REPORT)
        assert "50%" in note.title and "not search rankings" in note.body

    def test_idempotent_within_a_week(self, profile, fake_claude, reader):
        queue_report(fake_claude)
        fake_claude.queue({"items": []})
        services.run_due()
        assert services.run_due() == {"ran": 0, "skipped": 0}
        assert VisibilityRun.objects.count() == 1

    def test_due_again_after_a_week(self, profile, fake_claude, reader):
        self.due(profile, 8)
        queue_report(fake_claude)
        fake_claude.queue({"items": []})
        assert services.run_due()["ran"] == 1
        self.due(profile, 3)
        assert services.run_due()["ran"] == 0

    def test_skips_and_says_why_when_ai_is_not_configured(self, profile, settings, fake_claude, reader):
        settings.ANTHROPIC_API_KEY = ""
        assert services.run_due() == {"ran": 0, "skipped": 1}
        assert "Connect your AI" in VisibilityProfile.objects.get().skip_reason
        assert not VisibilityRun.objects.exists() and fake_claude.calls == []

    def test_skips_when_the_safety_cap_is_spent_and_clears_the_reason_after_a_run(
        self, profile, workspace, fake_claude, reader, settings
    ):
        org = workspace.organization
        settings.AI_ORGANIZATION_KEY_DAILY_CAP = 1_000
        OrganizationAIKey.objects.create(organization=org, provider="anthropic", api_key="sk-own")
        logs = [
            AIRequestLog(organization=org, action="x", provider="anthropic", model="m", used_organization_key=True)
            for _ in range(999)
        ]
        AIRequestLog.objects.bulk_create(logs)
        assert services.run_due()["skipped"] == 1
        assert VisibilityProfile.objects.get().skip_reason
        AIRequestLog.objects.all().delete()
        queue_report(fake_claude)
        fake_claude.queue({"items": []})
        services.run_due()
        assert VisibilityProfile.objects.get().skip_reason == ""

    def test_inactive_profiles_and_plans_without_the_feature_are_left_alone(
        self, profile, workspace, fake_claude, reader
    ):
        VisibilityProfile.objects.update(is_active=False)
        assert services.run_due() == {"ran": 0, "skipped": 0}
        VisibilityProfile.objects.update(is_active=True)
        set_plan(workspace.organization, "free")
        assert services.run_due() == {"ran": 0, "skipped": 0}
        assert fake_claude.calls == []

    def test_manual_runs_do_not_notify(self, profile, workspace, fake_claude, reader):
        from apps.analytics.tests.helpers import make_member
        from apps.notifications.models import Notification

        make_member(workspace, "o@example.com")
        queue_report(fake_claude)
        fake_claude.queue({"items": []})
        services.execute_run(services.start_run(profile, trigger="manual"))
        assert not Notification.objects.exists()

    def test_tasks_are_registered_and_runnable(self, profile, fake_claude, reader):
        from background_task.models import Task

        from apps.ai_visibility.apps import AIVisibilityConfig
        from apps.ai_visibility.tasks import execute_visibility_run, run_due_visibility_reports

        AIVisibilityConfig._register_tasks(None)
        AIVisibilityConfig._register_tasks(None)
        assert Task.objects.filter(verbose_name="run_due_visibility_reports", repeat=86400).count() == 1
        queue_report(fake_claude)
        fake_claude.queue({"items": []})
        run_due_visibility_reports.now()
        assert VisibilityRun.objects.get().status == "complete"
        execute_visibility_run.now(str(VisibilityRun.objects.get().id))  # not queued any more: a no-op
        assert VisibilityRun.objects.count() == 1


class TestReadingReports:
    def test_report_for_and_history(self, profile):
        older = VisibilityRun.objects.create(profile=profile, status="complete", brand_name=BRAND, mention_rate=25.0)
        VisibilityRun.objects.filter(pk=older.pk).update(created_at=timezone.now() - timedelta(days=7))
        newer = VisibilityRun.objects.create(profile=profile, status="complete", brand_name=BRAND, mention_rate=50.0)
        VisibilityAnswer.objects.create(
            run=newer,
            kind="category",
            question="q",
            engine="anthropic",
            answered=True,
            mentioned=False,
            competitors=["Buffer", "Later"],
        )
        VisibilityAnswer.objects.create(
            run=newer,
            kind="category",
            question="q2",
            engine="anthropic",
            answered=True,
            mentioned=True,
            competitors=["Buffer"],
        )
        report = services.report_for(newer)
        assert report["previous_rate"] == 25.0
        assert [a.question for a in report["gaps"]] == ["q"]
        assert report["competitors"] == [{"name": "Buffer", "answers": 2}, {"name": "Later", "answers": 1}]
        chart = services.trend_chart(profile)
        assert chart["kind"] == "percent" and [v for v in chart["datasets"][0]["values"]] == [25.0, 50.0]
        assert services.latest_run(profile) == newer
