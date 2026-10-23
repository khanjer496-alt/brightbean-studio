"""Plans as data, and the one helper that decides what an organization may do."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.billing import entitlements
from apps.billing.entitlements import PlanLimitError
from apps.billing.models import Subscription
from apps.billing.plans import PLANS, Feature, Interval, plan_for_price_id
from apps.composer.models import PlatformPost
from apps.composer.services import create_post, transition_platform_post
from apps.workspaces.models import Workspace

from .support import expire_trial, make_channels, set_plan


def _schedule(account, when=None, status="scheduled"):
    return create_post(
        workspace=account.workspace,
        social_account=account,
        caption="hello",
        scheduled_at=when or timezone.now() + timedelta(hours=1),
        status=status,
    )


class TestPlans:
    def test_prices_and_annual_discount(self):
        assert [PLANS[s].monthly_price_cents for s in ("free", "creator", "team", "agency")] == [0, 1200, 2900, 7900]
        # Annual billing is ten months: two months free.
        assert PLANS["team"].price_cents(Interval.YEAR) == 29000

    def test_limits_and_features(self):
        free, creator, team, agency = (PLANS[s] for s in ("free", "creator", "team", "agency"))
        assert (free.max_channels, free.max_workspaces, free.max_scheduled_posts_per_month) == (3, 1, 30)
        assert (creator.max_channels, creator.max_workspaces, creator.max_scheduled_posts_per_month) == (10, 2, None)
        assert (team.max_channels, team.max_workspaces) == (25, 5)
        assert (agency.max_channels, agency.max_workspaces) == (100, None)
        assert team.features == {Feature.APPROVALS, Feature.INBOX, Feature.ANALYTICS}
        assert {Feature.CLIENT_PORTAL, Feature.BRANDED_REPORTS, Feature.API} <= agency.features
        assert not free.features and not creator.features

    def test_no_plan_limits_seats(self):
        for plan in PLANS.values():
            assert not any("member" in name or "seat" in name for name in vars(plan))

    def test_price_ids_map_back_to_plans(self, billing):
        assert plan_for_price_id("price_team_y") == (PLANS["team"], Interval.YEAR)
        assert plan_for_price_id("price_unknown") is None
        assert plan_for_price_id("") is None


@pytest.mark.django_db
class TestBillingDisabled:
    def test_everything_unlimited_and_no_rows_written(self, organization, workspace):
        ent = entitlements.get_entitlements(organization)
        assert ent.unlimited and ent.has(Feature.API)
        make_channels(workspace, 5)
        entitlements.check_can_add_channel(organization)
        entitlements.check_can_add_workspace(organization)
        entitlements.check_can_schedule_posts(organization, count=1000)
        entitlements.require_feature(organization, Feature.CLIENT_PORTAL)
        assert not Subscription.objects.exists()

    def test_model_guard_is_inert(self, organization, workspace):
        account = make_channels(workspace, 1)[0]
        for _ in range(35):
            _schedule(account)
        assert PlatformPost.objects.filter(status="scheduled").count() == 35


@pytest.mark.django_db
class TestSubscriptionState:
    def test_new_org_trials_team(self, billing, organization):
        sub = entitlements.get_subscription(organization)
        assert sub.status == Subscription.Status.TRIALING
        assert sub.is_trialing and sub.trial_days_left == 14
        assert entitlements.get_entitlements(organization).plan.slug == "team"

    def test_expired_trial_is_free(self, billing, organization):
        expire_trial(organization)
        assert entitlements.get_entitlements(organization).plan.slug == "free"

    def test_grace_period_keeps_the_paid_plan_then_lapses(self, billing, organization):
        sub = set_plan(
            organization,
            "agency",
            status=Subscription.Status.PAST_DUE,
            stripe_subscription_id="sub_1",
            grace_until=timezone.now() + timedelta(days=2),
        )
        assert entitlements.get_entitlements(organization).plan.slug == "agency"
        sub.grace_until = timezone.now() - timedelta(minutes=1)
        sub.save()
        assert entitlements.get_entitlements(organization).plan.slug == "free"

    def test_canceled_is_free(self, billing, organization):
        set_plan(organization, "agency", status=Subscription.Status.CANCELED)
        assert entitlements.get_entitlements(organization).plan.slug == "free"


@pytest.mark.django_db
class TestLimits:
    def test_channel_limit_explains_what_to_remove(self, billing, organization, workspace):
        expire_trial(organization)
        make_channels(workspace, 5)
        with pytest.raises(PlanLimitError) as exc:
            entitlements.check_can_add_channel(organization)
        assert exc.value.code == "channel_limit"
        assert "Free plan includes 3 channels" in exc.value.message
        assert "Disconnect 3 channels" in exc.value.message
        assert exc.value.upgrade_url == "/organizations/billing/"

    def test_archived_workspaces_do_not_count(self, billing, organization, workspace):
        expire_trial(organization)
        with pytest.raises(PlanLimitError, match="Archive 1 workspace"):
            entitlements.check_can_add_workspace(organization)
        workspace.is_archived = True
        workspace.save()
        entitlements.check_can_add_workspace(organization)

    def test_unlimited_workspaces_on_agency(self, billing, organization):
        set_plan(organization, "agency")
        for i in range(8):
            Workspace.objects.create(organization=organization, name=f"W{i}")
        entitlements.check_can_add_workspace(organization)

    def test_feature_names_the_cheapest_plan_with_it(self, billing, organization):
        set_plan(organization, "creator")
        with pytest.raises(PlanLimitError, match="Upgrade to Team or higher") as exc:
            entitlements.require_feature(organization, Feature.INBOX)
        assert exc.value.feature == "inbox"
        with pytest.raises(PlanLimitError, match="Upgrade to Agency"):
            entitlements.require_feature(organization, Feature.API)

    def test_usage_summary_flags_overage(self, billing, organization, workspace):
        expire_trial(organization)
        make_channels(workspace, 4)
        rows = {row["key"]: row for row in entitlements.usage_summary(organization)}
        assert rows["channels"]["used"] == 4 and rows["channels"]["over_by"] == 1
        assert rows["workspaces"]["at_limit"] and not rows["workspaces"]["over_by"]
        assert rows["posts"]["limit"] == 30


@pytest.mark.django_db
class TestScheduledPostAllowance:
    @pytest.fixture
    def account(self, billing, organization, workspace):
        expire_trial(organization)
        return make_channels(workspace, 1)[0]

    def test_thirty_first_post_of_the_month_is_refused(self, account):
        when = timezone.now() + timedelta(minutes=5)
        for _ in range(30):
            _schedule(account, when)
        with pytest.raises(PlanLimitError, match="30 scheduled posts per month"):
            _schedule(account, when)
        assert PlatformPost.objects.count() == 30

    def test_other_months_have_their_own_allowance(self, account):
        next_month = entitlements.month_bounds()[1] + timedelta(days=2)
        month_after = entitlements.month_bounds(next_month)[1] + timedelta(days=2)
        for _ in range(30):
            _schedule(account, next_month)
        with pytest.raises(PlanLimitError):
            _schedule(account, next_month)
        _schedule(account, month_after)

    def test_drafts_are_not_counted(self, account):
        for _ in range(31):
            _schedule(account, status="draft")

    def test_promoting_a_draft_goes_through_the_guard(self, account):
        when = timezone.now() + timedelta(minutes=5)
        for _ in range(30):
            _schedule(account, when)
        pp = _schedule(account, status="draft").platform_posts.get()
        with pytest.raises(PlanLimitError):
            transition_platform_post(pp, "scheduled", scheduled_at=when)
        pp.refresh_from_db()
        assert pp.status == "draft"

    def test_a_counted_post_is_never_stopped(self, account):
        """The publisher moving a post between publishing and scheduled must not trip the limit."""
        when = timezone.now() + timedelta(minutes=5)
        posts = [_schedule(account, when).platform_posts.get() for _ in range(30)]
        pp = posts[0]
        pp.transition_to("publishing")
        pp.save()
        pp.transition_to("scheduled")
        pp.save()
        pp.transition_to("publishing")
        pp.save()
        pp.transition_to("failed")
        pp.save()
        assert PlatformPost.objects.get(pk=pp.pk).status == "failed"


@pytest.mark.django_db
def test_recurring_clones_past_the_allowance_wait_as_drafts(billing, organization, workspace):
    import zoneinfo
    from datetime import datetime
    from unittest.mock import patch

    from apps.calendar.models import RecurrenceRule
    from apps.calendar.tasks import generate_recurring_posts

    expire_trial(organization)
    account = make_channels(workspace, 1)[0]
    utc = zoneinfo.ZoneInfo("UTC")
    source = _schedule(account, datetime(2027, 1, 1, 9, 0, tzinfo=utc))
    RecurrenceRule.objects.create(post=source, frequency=RecurrenceRule.Frequency.DAILY, interval=1)

    with (
        patch("apps.calendar.tasks.timezone.now", return_value=datetime(2027, 1, 1, 10, 0, tzinfo=utc)),
        patch("apps.calendar.tasks.LOOKAHEAD_DAYS", 40),
    ):
        generate_recurring_posts()

    january = PlatformPost.objects.filter(scheduled_at__month=1, scheduled_at__year=2027)
    assert january.filter(status="scheduled").count() == 30
    assert january.filter(status="draft").count() == 1
    # February has its own allowance.
    assert not PlatformPost.objects.filter(scheduled_at__month=2, status="draft").exists()
