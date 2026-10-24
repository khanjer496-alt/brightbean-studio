"""Rule validation, the checker (thresholds, windows, backoff), idempotent firing and the autonomy outcomes."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.autonomy.models import AgentAction, Decision
from apps.plugs import services
from apps.plugs.models import PlugRule, PlugRun
from apps.plugs.services import PlugError
from providers.exceptions import APIError, PublishError, QuotaExceededError

from .conftest import make_account, publish_post, set_policy

pytestmark = pytest.mark.django_db


def rule_for(published, **overrides):
    fields = {
        "trigger": "likes",
        "threshold": 10,
        "window_hours": 24,
        "action": "repost",
    }
    fields.update(overrides)
    return services.create_rule(
        workspace=published.social_account.workspace,
        account=published.social_account,
        platform_post=published,
        **fields,
    )


class TestValidation:
    def test_network_without_numbers_is_refused(self, workspace):
        personal = make_account(workspace, "linkedin_personal", "Me")
        with pytest.raises(PlugError, match="offers no way to watch"):
            services.clean_rule(
                workspace=workspace, account=personal, platform_post=None, trigger="likes", threshold=5,
                window_hours=24, action="reply", message="x",
            )  # fmt: skip

    def test_trigger_the_network_does_not_report(self, published):
        with pytest.raises(PlugError, match="does not report views"):
            rule_for(published, trigger="views")

    def test_x_cannot_quote(self, workspace):
        x = make_account(workspace, "x", "Ex")
        with pytest.raises(PlugError, match="cannot quote"):
            services.clean_rule(
                workspace=workspace, account=x, platform_post=None, trigger="likes", threshold=5,
                window_hours=24, action="quote", message="hi",
            )  # fmt: skip

    def test_reply_needs_a_message_within_the_limit(self, published):
        with pytest.raises(PlugError, match="Write the message"):
            rule_for(published, action="reply")
        with pytest.raises(PlugError, match="allows 300"):
            rule_for(published, action="reply", message="x" * 301)

    def test_repost_drops_the_message(self, published):
        assert rule_for(published, message="ignored").message == ""

    def test_numbers_are_bounded(self, published):
        with pytest.raises(PlugError, match="at least 1"):
            rule_for(published, threshold=0)
        with pytest.raises(PlugError, match="between 1 and 168"):
            rule_for(published, window_hours=500)
        with pytest.raises(PlugError, match="whole numbers"):
            rule_for(published, threshold="many")

    def test_duplicate_and_cap(self, published):
        rule_for(published)
        with pytest.raises(PlugError, match="already exists"):
            rule_for(published)
        for threshold in range(11, 15):
            rule_for(published, threshold=threshold)
        with pytest.raises(PlugError, match="At most 5"):
            rule_for(published, threshold=99)

    def test_window_already_over_for_a_published_post(self, account):
        old = publish_post(account, hours_ago=30)
        with pytest.raises(PlugError, match="could never fire"):
            rule_for(old, window_hours=24)

    def test_post_must_belong_to_the_account(self, published, booster):
        with pytest.raises(PlugError, match="not on this account"):
            services.create_rule(
                workspace=booster.workspace, account=booster, platform_post=published, trigger="likes",
                threshold=5, window_hours=24, action="repost",
            )  # fmt: skip


class TestCrossAccountScoping:
    def test_reposter_must_share_workspace_and_network(self, published, booster, organization):
        from apps.workspaces.models import Workspace

        other_ws = Workspace.objects.create(organization=organization, name="Other")
        foreign = make_account(other_ws, "bluesky", "Foreign")
        wrong_network = make_account(published.social_account.workspace, "mastodon", "Toot")
        with pytest.raises(PlugError, match="same workspace"):
            rule_for(published, actor_account=foreign)
        with pytest.raises(PlugError, match="same network"):
            rule_for(published, actor_account=wrong_network)
        assert rule_for(published, actor_account=booster).is_cross_account

    def test_reposter_must_be_connected(self, published, booster):
        booster.connection_status = "disconnected"
        booster.save()
        with pytest.raises(PlugError, match="not connected"):
            rule_for(published, actor_account=booster)

    def test_reply_cannot_come_from_another_account(self, published, booster):
        with pytest.raises(PlugError, match="cannot reply"):
            rule_for(published, action="reply", message="hi", actor_account=booster)

    def test_choices_list_only_same_workspace_same_network(self, account, booster, workspace, organization):
        from apps.workspaces.models import Workspace

        make_account(workspace, "mastodon", "Toot")
        make_account(Workspace.objects.create(organization=organization, name="Other"), "bluesky", "Foreign")
        assert list(services.reposter_choices(account)) == [booster]

    def test_a_rule_edited_into_another_workspace_is_refused_at_firing(self, published, booster, full_auto, network):
        rule = rule_for(published, actor_account=booster, threshold=1)
        from apps.workspaces.models import Workspace

        other = Workspace.objects.create(organization=booster.workspace.organization, name="Moved")
        booster.workspace = other
        booster.save()
        network.counts = {"likes": 5}
        services.check_rules()
        run = PlugRun.objects.get(rule=rule)
        assert run.status == PlugRun.Status.FAILED
        assert network.calls == []


class TestThresholdAndWindow:
    def test_fires_when_the_count_is_reached(self, published, full_auto, network):
        rule = rule_for(published, threshold=10)
        network.counts = {"likes": 9}
        result = services.check_rules()
        assert (result.read, result.fired) == (1, 0)
        assert network.calls == []

        PlugRun.objects.update(next_check_at=None)
        network.counts = {"likes": 10}
        assert services.check_rules().fired == 1
        run = PlugRun.objects.get(rule=rule)
        assert run.status == PlugRun.Status.DONE and run.reached_value == 10
        assert network.calls[0][0] == "repost"

    def test_other_counts_do_not_trigger(self, published, full_auto, network):
        rule_for(published, trigger="reposts", threshold=3)
        network.counts = {"likes": 500}
        assert services.check_rules().fired == 0

    def test_window_ends_the_watch(self, account, full_auto, network):
        post = publish_post(account, hours_ago=5)
        rule_for(post, window_hours=6)
        type(post).objects.filter(pk=post.pk).update(published_at=timezone.now() - timedelta(hours=7))
        # Created while still inside the window, then the window passes.
        run = PlugRun.objects.create(rule=PlugRule.objects.get(), platform_post=post)
        network.counts = {"likes": 999}
        result = services.check_rules()
        run.refresh_from_db()
        assert run.status == PlugRun.Status.EXPIRED and result.expired == 1
        assert network.calls == [] and network.reads == 0

    def test_inactive_rules_are_not_read(self, published, full_auto, network):
        rule_for(published, is_active=False)
        assert services.check_rules().read == 0 and network.reads == 0

    def test_disconnected_account_is_not_read(self, published, full_auto, network):
        rule_for(published)
        account = published.social_account
        account.connection_status = "disconnected"
        account.save()
        services.check_rules()
        assert network.reads == 0

    def test_two_rules_on_one_post_read_it_once(self, published, full_auto, network):
        rule_for(published, threshold=50)
        rule_for(published, threshold=80)
        network.counts = {"likes": 1}
        services.check_rules()
        assert network.reads == 1

    def test_reads_are_spaced_by_the_providers_interval(self, published, full_auto, network):
        rule_for(published, threshold=50)
        services.check_rules()
        services.check_rules()
        assert network.reads == 1
        run = PlugRun.objects.get()
        assert run.next_check_at > timezone.now() + timedelta(minutes=10)

    def test_per_account_read_cap(self, account, full_auto, network, monkeypatch):
        monkeypatch.setattr(services, "MAX_READS_PER_ACCOUNT", 2)
        for n in range(4):
            rule_for(publish_post(account, remote_id=f"at://x/{n}"), threshold=50)
        services.check_rules()
        assert network.reads == 2


class TestDefaultRules:
    def default_rule(self, account, **fields):
        base = {"trigger": "likes", "threshold": 5, "window_hours": 24, "action": "repost"}
        base.update(fields)
        return services.create_rule(workspace=account.workspace, account=account, **base)

    def test_applies_to_posts_published_after_it_was_set(self, account, full_auto, network):
        old = publish_post(account, hours_ago=2, remote_id="at://old")
        rule = self.default_rule(account)
        PlugRule.objects.filter(pk=rule.pk).update(created_at=timezone.now() - timedelta(minutes=30))
        new = publish_post(account, hours_ago=0, remote_id="at://new")
        network.counts = {"likes": 6}
        services.check_rules()
        runs = PlugRun.objects.filter(rule=rule)
        assert [r.platform_post_id for r in runs] == [new.pk]
        assert not PlugRun.objects.filter(platform_post=old).exists()

    def test_each_post_gets_its_own_single_run(self, account, full_auto, network):
        rule = self.default_rule(account)
        PlugRule.objects.filter(pk=rule.pk).update(created_at=timezone.now() - timedelta(hours=3))
        publish_post(account, hours_ago=1, remote_id="at://a")
        publish_post(account, hours_ago=1, remote_id="at://b")
        network.counts = {"likes": 9}
        services.check_rules()
        services.check_rules()
        assert PlugRun.objects.filter(rule=rule).count() == 2
        assert [c[0] for c in network.calls] == ["repost", "repost"]


class TestIdempotency:
    def test_fires_once_however_often_it_runs(self, published, full_auto, network):
        rule_for(published, threshold=1)
        network.counts = {"likes": 50}
        for _ in range(3):
            PlugRun.objects.update(next_check_at=None)
            services.check_rules()
        assert [c[0] for c in network.calls] == ["repost"]

    def test_firing_a_run_already_claimed_does_nothing(self, published, full_auto, network):
        rule_for(published, threshold=1)
        network.counts = {"likes": 5}
        services.check_rules()
        run = PlugRun.objects.get()
        services.fire(run, value=5)
        assert len(network.calls) == 1

    def test_ambiguous_outcome_is_never_repeated(self, published, full_auto, network):
        rule_for(published, threshold=1)
        network.counts = {"likes": 5}
        network.error = ("act", APIError("timed out", ambiguous_write=True))
        services.check_rules()
        run = PlugRun.objects.get()
        assert run.status == PlugRun.Status.UNCONFIRMED
        services.release_due(now=timezone.now() + timedelta(days=1))
        assert len(network.calls) == 1

    def test_transient_failure_retries_then_gives_up(self, published, full_auto, network):
        rule_for(published, threshold=1)
        network.counts = {"likes": 5}
        network.error = ("act", PublishError("busy", retryable=True))
        services.check_rules()
        run = PlugRun.objects.get()
        assert run.status == PlugRun.Status.RETRY
        later = timezone.now()
        for _ in range(5):
            later += timedelta(hours=2)
            services.release_due(now=later)
        run.refresh_from_db()
        assert run.status == PlugRun.Status.FAILED and run.attempts == services.MAX_ATTEMPTS

    def test_permanent_failure_ends_at_once(self, published, full_auto, network):
        rule_for(published, threshold=1)
        network.counts = {"likes": 5}
        network.error = ("act", PublishError("nope", retryable=False))
        services.check_rules()
        assert PlugRun.objects.get().status == PlugRun.Status.FAILED

    def test_a_worker_that_died_mid_request_is_recorded_as_unknown(self, published, full_auto, network):
        rule = rule_for(published)
        run = PlugRun.objects.create(rule=rule, platform_post=published, status=PlugRun.Status.FIRING)
        PlugRun.objects.filter(pk=run.pk).update(updated_at=timezone.now() - timedelta(hours=2))
        services.release_due()
        run.refresh_from_db()
        assert run.status == PlugRun.Status.UNCONFIRMED and network.calls == []


class TestBackoff:
    def test_errors_back_off_exponentially_then_stop(self, published, full_auto, network):
        rule_for(published, threshold=50)
        network.error = ("read", RuntimeError("boom"))
        gaps = []
        for _ in range(services.MAX_CHECK_FAILURES):
            PlugRun.objects.filter(status="watching").update(next_check_at=None)
            services.check_rules()
            run = PlugRun.objects.get()
            if run.next_check_at:
                gaps.append(round((run.next_check_at - timezone.now()).total_seconds()))
        assert gaps == sorted(gaps) and gaps[-1] > gaps[0]
        assert all(gap <= services.BACKOFF_CAP.total_seconds() for gap in gaps)
        assert PlugRun.objects.get().status == PlugRun.Status.FAILED

    def test_rate_limit_pauses_the_whole_account(self, account, full_auto, network):
        for n in range(3):
            rule_for(publish_post(account, remote_id=f"at://x/{n}"), threshold=50)
        network.error = ("read", QuotaExceededError("spent", resets_at=timezone.now() + timedelta(hours=3)))
        services.check_rules()
        assert network.reads == 1
        runs = PlugRun.objects.all()
        assert all(r.next_check_at > timezone.now() + timedelta(hours=2) for r in runs)
        assert all(r.status == PlugRun.Status.WATCHING and r.check_failures == 0 for r in runs)
        services.check_rules()
        assert network.reads == 1


class TestAutonomy:
    def test_draft_only_holds_it_for_approval(self, published, network):
        # The conservative default: automations wait for a person.
        rule_for(published, threshold=1)
        network.counts = {"likes": 5}
        services.check_rules()
        run = PlugRun.objects.get()
        assert run.status == PlugRun.Status.AWAITING_APPROVAL
        assert network.calls == []
        entry = AgentAction.objects.get(plug_run=run)
        assert entry.decision == Decision.HELD
        assert entry.actor_type == "system" and entry.operation == "plug.repost"

    def test_approving_acts_once_as_the_person(self, published, network, owner):
        from apps.autonomy import release

        rule_for(published, threshold=1)
        network.counts = {"likes": 5}
        services.check_rules()
        entry = AgentAction.objects.get()
        release.approve_held(entry, owner)
        assert PlugRun.objects.get().status == PlugRun.Status.DONE
        assert [c[0] for c in network.calls] == ["repost"]
        with pytest.raises(release.ReleaseError):
            release.approve_held(entry, owner)
        assert len(network.calls) == 1

    def test_dismissing_cancels(self, published, network, owner):
        from apps.autonomy import release

        rule_for(published, threshold=1)
        network.counts = {"likes": 5}
        services.check_rules()
        release.dismiss_held(AgentAction.objects.get(), owner)
        assert PlugRun.objects.get().status == PlugRun.Status.CANCELLED
        assert network.calls == []

    def test_off_blocks(self, published, network, workspace):
        set_policy(workspace, publish_level="off")
        rule_for(published, threshold=1)
        network.counts = {"likes": 5}
        services.check_rules()
        run = PlugRun.objects.get()
        assert run.status == PlugRun.Status.BLOCKED and network.calls == []
        assert AgentAction.objects.get(plug_run=run).decision == Decision.BLOCKED

    def test_replies_follow_the_reply_level(self, published, network, workspace):
        set_policy(workspace, publish_level="full_auto", reply_level="draft_only", undo_delay_minutes=0)
        rule_for(published, threshold=1, action="reply", message="More at example.com")
        network.counts = {"likes": 5}
        services.check_rules()
        run = PlugRun.objects.get()
        assert run.status == PlugRun.Status.AWAITING_APPROVAL
        assert AgentAction.objects.get().action_type == "reply"

    def test_crisis_pause_holds(self, published, network, workspace):
        set_policy(workspace, publish_level="full_auto", undo_delay_minutes=0, crisis_paused=True)
        rule_for(published, threshold=1)
        network.counts = {"likes": 5}
        services.check_rules()
        assert PlugRun.objects.get().status == PlugRun.Status.AWAITING_APPROVAL

    def test_executed_waits_out_the_undo_window(self, published, network, workspace):
        set_policy(workspace, publish_level="full_auto", undo_delay_minutes=5)
        rule_for(published, threshold=1)
        network.counts = {"likes": 5}
        services.check_rules()
        run = PlugRun.objects.get()
        assert run.status == PlugRun.Status.QUEUED and network.calls == []
        assert services.release_due() == 0
        assert services.release_due(now=timezone.now() + timedelta(minutes=6)) == 1
        run.refresh_from_db()
        assert run.status == PlugRun.Status.DONE and len(network.calls) == 1

    def test_undo_cancels_before_it_goes_out(self, published, network, workspace, owner):
        from apps.autonomy import release

        set_policy(workspace, publish_level="full_auto", undo_delay_minutes=5)
        rule_for(published, threshold=1)
        network.counts = {"likes": 5}
        services.check_rules()
        release.undo(AgentAction.objects.get(), owner)
        services.release_due(now=timezone.now() + timedelta(hours=1))
        assert PlugRun.objects.get().status == PlugRun.Status.CANCELLED and network.calls == []

    def test_level_lowered_while_queued_holds_it_at_send(self, published, network, workspace):
        set_policy(workspace, publish_level="full_auto", undo_delay_minutes=5)
        rule_for(published, threshold=1)
        network.counts = {"likes": 5}
        services.check_rules()
        set_policy(workspace, publish_level="draft_only")
        services.release_due(now=timezone.now() + timedelta(minutes=6))
        assert PlugRun.objects.get().status == PlugRun.Status.AWAITING_APPROVAL
        assert network.calls == []
        assert AgentAction.objects.filter(decision=Decision.HELD, plug_run__isnull=False).exists()

    def test_crisis_pause_keeps_a_queued_run_waiting(self, published, network, workspace):
        set_policy(workspace, publish_level="full_auto", undo_delay_minutes=5)
        rule_for(published, threshold=1)
        network.counts = {"likes": 5}
        services.check_rules()
        set_policy(workspace, crisis_paused=True)
        services.release_due(now=timezone.now() + timedelta(hours=1))
        assert PlugRun.objects.get().status == PlugRun.Status.QUEUED and network.calls == []


class TestActions:
    def test_cross_account_repost_acts_as_the_other_account(self, published, booster, full_auto, network, monkeypatch):
        used = []
        monkeypatch.setattr(
            "apps.publisher.engine._provider_and_access_token", lambda a: (used.append(a.pk) or network, "t")
        )
        rule_for(published, threshold=1, actor_account=booster)
        network.counts = {"likes": 3}
        services.check_rules()
        assert used == [published.social_account_id, booster.pk]
        target = network.calls[0][1]
        assert target.cross_account and target.platform_post_id == published.platform_post_id
        assert target.permalink == published.permalink

    def test_reply_goes_under_the_post(self, published, full_auto, network):
        rule_for(published, threshold=1, action="reply", message="Read more: https://example.com")
        network.counts = {"likes": 3}
        services.check_rules()
        assert network.calls == [("reply", published.platform_post_id, "Read more: https://example.com")]

    def test_quote_carries_the_message(self, published, booster, full_auto, network):
        rule_for(published, threshold=1, action="quote", message="Worth reading", actor_account=booster)
        network.counts = {"likes": 3}
        services.check_rules()
        assert network.calls[0][0] == "quote" and network.calls[0][2] == "Worth reading"

    def test_deleting_a_rule_drops_its_runs(self, published, full_auto, network):
        rule = rule_for(published, threshold=50)
        services.check_rules()
        services.delete_rule(rule)
        assert not PlugRun.objects.exists()
