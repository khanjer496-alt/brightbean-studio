"""Adding competitors, plan limits, daily snapshots and the growth math."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.analytics.ranges import parse_range
from apps.analytics.tests.helpers import fill_days, make_account
from apps.billing.tests.support import set_plan
from apps.competitors import services
from apps.competitors.models import Competitor, CompetitorSnapshot
from apps.competitors.services import CompetitorError
from providers.exceptions import PublicProfileNotFoundError, RateLimitError, TokenExpiredError

from .conftest import days_ago, metrics

pytestmark = pytest.mark.django_db


class TestAdd:
    def test_adds_and_stores_the_first_snapshot(self, workspace, bsky, read):
        competitor = services.add_competitor(workspace, "bluesky", "@Rival.Bsky.Social")
        assert competitor.handle == "rival.bsky.social"
        assert competitor.display_name == "Rival"
        snap = competitor.snapshots.get()
        assert (snap.followers, snap.posts_per_week, snap.avg_engagement) == (5000, 3.5, 12.0)
        assert read.calls == ["rival.bsky.social"]

    def test_youtube_channel_ids_keep_their_case(self, workspace):
        make_account(workspace, "youtube", "YT")
        assert services.canonical_handle("youtube", "UC" + "aB" * 11) == "UC" + "aB" * 11
        assert services.canonical_handle("youtube", "AcmeTV") == "acmetv"

    def test_unsupported_network_is_refused_with_the_reason(self, workspace):
        make_account(workspace, "linkedin_personal", "Me")
        with pytest.raises(CompetitorError) as exc:
            services.add_competitor(workspace, "linkedin_personal", "someone")
        assert exc.value.code == "competitor_unsupported"
        assert "official" in exc.value.message

    def test_bad_handle(self, workspace, bsky, read):
        with pytest.raises(CompetitorError) as exc:
            services.add_competitor(workspace, "bluesky", "not a handle!")
        assert exc.value.code == "competitor_bad_handle"

    def test_needs_a_connected_account_on_the_network(self, workspace, read):
        with pytest.raises(CompetitorError) as exc:
            services.add_competitor(workspace, "bluesky", "rival.bsky.social")
        assert exc.value.code == "competitor_no_account"
        assert Competitor.objects.count() == 0

    def test_account_outside_the_allowlist_cannot_be_used(self, workspace, bsky, read):
        with pytest.raises(CompetitorError) as exc:
            services.add_competitor(workspace, "bluesky", "rival.bsky.social", allowed_account_ids=set())
        assert exc.value.code == "competitor_no_account"
        services.add_competitor(workspace, "bluesky", "rival.bsky.social", allowed_account_ids={bsky.id})

    def test_unknown_profile_creates_nothing(self, workspace, bsky, read):
        read.results["ghost.bsky.social"] = CompetitorError("No Bluesky profile found.", code="competitor_not_found")
        with pytest.raises(CompetitorError) as exc:
            services.add_competitor(workspace, "bluesky", "ghost.bsky.social")
        assert exc.value.code == "competitor_not_found"
        assert not Competitor.objects.exists() and not CompetitorSnapshot.objects.exists()

    def test_duplicate(self, workspace, bsky, read):
        services.add_competitor(workspace, "bluesky", "rival.bsky.social")
        with pytest.raises(CompetitorError) as exc:
            services.add_competitor(workspace, "bluesky", "RIVAL.bsky.social")
        assert exc.value.code == "competitor_duplicate" and exc.value.status == 409

    def test_the_same_profile_can_be_tracked_by_another_workspace(self, workspace, bsky, read):
        from apps.workspaces.models import Workspace

        other = Workspace.objects.create(organization=workspace.organization, name="Other")
        make_account(other, "bluesky", "Other Bluesky")
        services.add_competitor(workspace, "bluesky", "rival.bsky.social")
        services.add_competitor(other, "bluesky", "rival.bsky.social")
        assert Competitor.objects.count() == 2


class TestFetchProfileErrors:
    """The real ``fetch_profile`` turns provider failures into sentences for the person, without provider text."""

    @pytest.mark.parametrize(
        "error,code",
        [
            (PublicProfileNotFoundError("No Bluesky profile found for x.", platform="Bluesky"), "competitor_not_found"),
            (RateLimitError("secret detail", platform="Bluesky"), "competitor_rate_limited"),
            (TokenExpiredError("secret detail", platform="Bluesky"), "competitor_reconnect"),
            (RuntimeError("secret detail"), "competitor_error"),
        ],
    )
    def test_mapping(self, workspace, bsky, monkeypatch, error, code):
        class Provider:
            def public_profile_metrics(self, handle, account, token):
                raise error

        monkeypatch.setattr("apps.analytics.tasks._analytics_provider_and_token", lambda account: (Provider(), "t"))
        with pytest.raises(CompetitorError) as exc:
            services.fetch_profile("bluesky", "x", bsky)
        assert exc.value.code == code
        assert "secret detail" not in exc.value.message


class TestPlanLimits:
    @pytest.mark.parametrize("plan,limit", [("free", 0), ("creator", 3), ("team", 10), ("agency", 25)])
    def test_limit_per_network(self, workspace, bsky, read, plan, limit):
        set_plan(workspace.organization, plan)
        for i in range(limit):
            services.add_competitor(workspace, "bluesky", f"rival{i}.bsky.social")
        with pytest.raises(CompetitorError) as exc:
            services.add_competitor(workspace, "bluesky", "one-too-many.bsky.social")
        assert exc.value.code == "competitor_limit" and exc.value.status == 403
        if limit:
            assert str(limit) in exc.value.message
        assert Competitor.objects.count() == limit

    def test_limit_is_per_network(self, workspace, bsky, read):
        set_plan(workspace.organization, "creator")
        make_account(workspace, "mastodon", "Masto")
        for i in range(3):
            services.add_competitor(workspace, "bluesky", f"rival{i}.bsky.social")
        services.add_competitor(workspace, "mastodon", "bob")
        assert Competitor.objects.filter(platform="mastodon").count() == 1

    def test_removing_frees_a_slot(self, workspace, bsky, read):
        set_plan(workspace.organization, "creator")
        added = [services.add_competitor(workspace, "bluesky", f"r{i}.bsky.social") for i in range(3)]
        services.remove_competitor(workspace, added[0].id)
        services.add_competitor(workspace, "bluesky", "new.bsky.social")

    def test_self_hosted_has_no_limit(self, workspace, bsky, read, settings):
        settings.BILLING_ENABLED = False
        for i in range(12):
            services.add_competitor(workspace, "bluesky", f"r{i}.bsky.social")

    def test_remove_is_scoped_to_the_workspace(self, workspace, bsky, read):
        from apps.workspaces.models import Workspace

        competitor = services.add_competitor(workspace, "bluesky", "rival.bsky.social")
        other = Workspace.objects.create(organization=workspace.organization, name="Other")
        with pytest.raises(CompetitorError) as exc:
            services.remove_competitor(other, competitor.id)
        assert exc.value.status == 404
        assert Competitor.objects.filter(pk=competitor.pk).exists()


class TestSnapshots:
    def _tracked(self, workspace, read, handle="rival.bsky.social"):
        competitor = Competitor.objects.create(workspace=workspace, platform="bluesky", handle=handle)
        return competitor

    def test_idempotent_within_a_day(self, workspace, bsky, read):
        self._tracked(workspace, read)
        first = services.snapshot_all()
        second = services.snapshot_all()
        assert first == {"read": 1, "skipped": 0, "failed": 0}
        assert second == {"read": 0, "skipped": 1, "failed": 0}
        assert read.calls == ["rival.bsky.social"]
        assert CompetitorSnapshot.objects.count() == 1

    def test_force_overwrites_todays_row_instead_of_adding(self, workspace, bsky, read):
        self._tracked(workspace, read)
        services.snapshot_all()
        read.results["rival.bsky.social"] = metrics(followers=5100)
        services.snapshot_all(force=True)
        assert CompetitorSnapshot.objects.get().followers == 5100

    def test_a_new_day_adds_a_row(self, workspace, bsky, read):
        self._tracked(workspace, read)
        services.snapshot_all(today=days_ago(1))
        services.snapshot_all()
        assert CompetitorSnapshot.objects.count() == 2

    def test_failure_is_recorded_and_the_rest_continue(self, workspace, bsky, read):
        a = self._tracked(workspace, read, "gone.bsky.social")
        self._tracked(workspace, read, "fine.bsky.social")
        read.results["gone.bsky.social"] = CompetitorError("No Bluesky profile found.", code="competitor_not_found")
        stats = services.snapshot_all()
        assert stats == {"read": 1, "skipped": 0, "failed": 1}
        a.refresh_from_db()
        assert a.last_error == "No Bluesky profile found."
        assert CompetitorSnapshot.objects.get().competitor.handle == "fine.bsky.social"

    def test_rate_limit_stops_that_network_for_the_workspace(self, workspace, bsky, read):
        self._tracked(workspace, read, "a.bsky.social")
        self._tracked(workspace, read, "b.bsky.social")
        read.results["a.bsky.social"] = CompetitorError("busy", code="competitor_rate_limited", status=429)
        read.results["b.bsky.social"] = CompetitorError("busy", code="competitor_rate_limited", status=429)
        stats = services.snapshot_all()
        assert stats["read"] == 0 and len(read.calls) == 1

    def test_success_clears_a_previous_error(self, workspace, bsky, read):
        c = self._tracked(workspace, read)
        Competitor.objects.filter(pk=c.pk).update(last_error="old")
        services.snapshot_all()
        c.refresh_from_db()
        assert c.last_error == "" and c.last_synced_at is not None

    def test_skips_a_workspace_without_an_account_or_entitlement(self, workspace, read):
        self._tracked(workspace, read)
        assert services.snapshot_all()["skipped"] == 1  # no connected Bluesky account
        make_account(workspace, "bluesky", "Acme Bluesky")
        set_plan(workspace.organization, "free")
        assert services.snapshot_all()["skipped"] == 1  # downgraded: kept, not read
        assert read.calls == [] and Competitor.objects.count() == 1

    def test_task_runs_the_snapshot(self, workspace, bsky, read):
        from apps.competitors.tasks import snapshot_competitors

        self._tracked(workspace, read)
        snapshot_competitors.now()
        assert CompetitorSnapshot.objects.count() == 1

    def test_task_is_registered(self, db):
        from background_task.models import Task

        from apps.competitors.apps import CompetitorsConfig

        CompetitorsConfig._register_tasks(None)
        assert Task.objects.filter(verbose_name="snapshot_competitors", repeat=86400).count() == 1
        CompetitorsConfig._register_tasks(None)
        assert Task.objects.filter(verbose_name="snapshot_competitors").count() == 1

    def test_prune(self, workspace, bsky, read):
        c = self._tracked(workspace, read)
        CompetitorSnapshot.objects.create(competitor=c, date=days_ago(900), followers=1)
        CompetitorSnapshot.objects.create(competitor=c, date=days_ago(1), followers=2)
        assert services.prune_snapshots() == 1


class TestGrowthMath:
    def test_growth_pct(self):
        assert services.growth_pct(100, 110) == 10.0
        assert services.growth_pct(200, 150) == -25.0
        assert services.growth_pct(0, 10) is None
        assert services.growth_pct(None, 10) is None
        assert services.growth_pct(10, None) is None
        assert services.growth_pct(3, 4) == 33.33

    def _rng(self, workspace, days=30):
        return parse_range({"range": str(days) if days in (7, 30, 90) else "30"}, workspace)

    def test_window_growth_uses_the_baseline_before_the_range(self, workspace):
        rng = self._rng(workspace)
        points = [(rng.start - timedelta(days=3), 1000), (rng.start + timedelta(days=5), 1050), (rng.end, 1100)]
        assert services._window_growth(points, rng) == 10.0

    def test_window_growth_without_earlier_history_starts_at_the_first_point_inside(self, workspace):
        rng = self._rng(workspace)
        points = [(rng.start + timedelta(days=10), 2000), (rng.end, 2200)]
        assert services._window_growth(points, rng) == 10.0

    def test_one_reading_is_not_growth(self, workspace):
        rng = self._rng(workspace)
        assert services._window_growth([(rng.end, 500)], rng) is None
        assert services._window_growth([], rng) is None

    def test_hidden_counts_are_skipped(self, workspace):
        rng = self._rng(workspace)
        points = [(rng.start, None), (rng.start + timedelta(days=1), 100), (rng.end, 120)]
        assert services._window_growth(points, rng) == 20.0

    def test_points_after_the_range_are_ignored(self, workspace):
        rng = self._rng(workspace)
        points = [(rng.start, 100), (rng.end, 110), (rng.end + timedelta(days=1), 999)]
        assert services._window_growth(points, rng) == 10.0


class TestComparison:
    def test_your_rows_sit_beside_competitors_sorted_by_size(self, workspace, bsky, read):
        today = timezone.now().date()
        fill_days(bsky, "followers", today - timedelta(days=30), today, 1000)
        small = Competitor.objects.create(workspace=workspace, platform="bluesky", handle="small.bsky.social")
        big = Competitor.objects.create(workspace=workspace, platform="bluesky", handle="big.bsky.social")
        for comp, (start, end) in ((small, (100, 110)), (big, (9000, 9900))):
            CompetitorSnapshot.objects.create(
                competitor=comp, date=today - timedelta(days=28), followers=start, posts_per_week=2.0
            )
            CompetitorSnapshot.objects.create(
                competitor=comp, date=today, followers=end, posts_per_week=4.0, avg_engagement=7.5
            )
        rng = parse_range({}, workspace)
        data = services.comparison(workspace, rng, [bsky])
        (group,) = data["groups"]
        assert [r["handle"] for r in group["competitors"]] == ["big.bsky.social", "small.bsky.social"]
        assert [r["growth"] for r in group["competitors"]] == [10.0, 10.0]
        assert group["competitors"][0]["posts_per_week"] == 4.0
        assert group["competitors"][0]["avg_engagement"] == 7.5
        assert group["you"][0]["label"] == "Acme Bluesky" and group["you"][0]["followers"] == 1000

    def test_unsupported_connected_networks_are_listed_with_a_reason(self, workspace, bsky):
        make_account(workspace, "linkedin_personal", "Me")
        data = services.comparison(workspace, parse_range({}, workspace), [bsky])
        assert [n.platform for n in data["unsupported"]] == ["linkedin_personal"]
        assert data["unsupported"][0].reason

    def test_trend_shows_you_and_at_most_three_competitors(self, workspace):
        # Bluesky reports no analytics of its own, so "you" needs a network that does.
        bsky = make_account(workspace, "instagram", "Acme IG", followers=1000)
        today = timezone.now().date()
        rng = parse_range({}, workspace)
        add = {d: 1000 + i * 10 for i, d in enumerate(rng.day_list())}
        from apps.analytics.models import AccountInsightsSnapshot

        AccountInsightsSnapshot.objects.bulk_create(
            AccountInsightsSnapshot(social_account=bsky, metric_key="followers", date=d, value=v)
            for d, v in add.items()
        )
        for i in range(5):
            comp = Competitor.objects.create(workspace=workspace, platform="instagram", handle=f"c{i}.bsky.social")
            CompetitorSnapshot.objects.create(competitor=comp, date=rng.start, followers=1000 * (i + 1))
            CompetitorSnapshot.objects.create(competitor=comp, date=today, followers=1100 * (i + 1))
        chart = services.trend(workspace, rng, "instagram", [bsky])
        assert [d["label"] for d in chart["datasets"]] == ["You", "c4.bsky.social", "c3.bsky.social", "c2.bsky.social"]
        assert chart["kind"] == "percent" and len(chart["labels"]) == len(rng.day_list())
        assert chart["datasets"][1]["values"][-1] == 10.0
        assert services.trend_summary(chart).startswith("Follower growth since")

    def test_trend_is_none_without_two_readings(self, workspace, bsky):
        assert services.trend(workspace, parse_range({}, workspace), "bluesky", [bsky]) is None
