"""Best time to post: scoring, defaults, timezones, caching and the daily task."""

from __future__ import annotations

import zoneinfo
from datetime import UTC, datetime, timedelta

import pytest
from django.utils import timezone

from apps.analytics import best_times
from apps.analytics.models import BestTimeSnapshot
from apps.analytics.tests.helpers import make_account, make_post
from apps.workspaces.models import Workspace

NOW = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)  # a Monday


@pytest.fixture
def workspace(db, organization):
    return Workspace.objects.create(organization=organization, name="Main", timezone="UTC")


@pytest.fixture
def account(workspace):
    return make_account(workspace, "bluesky", "sky", followers=1000)


def published(workspace, account, when, likes=10, views=1000):
    snaps = {
        "likes": [(when.date(), likes, when)],
        "views": [(when.date(), views, when)],
    }
    return make_post(workspace, account, when, snapshots=snaps)


def seed(workspace, account, count, *, hour=9, weekday_offset=0, likes=10):
    for i in range(count):
        when = (NOW - timedelta(days=7 * (i + 1) + weekday_offset)).replace(hour=hour)
        published(workspace, account, when, likes=likes)


class TestEngagementRate:
    def test_uses_views_before_followers(self):
        assert best_times.post_engagement_rate({"likes": 10, "views": 1000}, 50) == pytest.approx(0.01)

    def test_falls_back_to_followers(self):
        assert best_times.post_engagement_rate({"likes": 10}, 500) == pytest.approx(0.02)

    def test_unmeasurable_without_any_denominator(self):
        assert best_times.post_engagement_rate({"likes": 10}, 0) is None


@pytest.mark.django_db
class TestScoring:
    def test_enough_posts_scores_from_the_accounts_own_posts(self, workspace, account):
        # Mondays at 09:00 do much better than Wednesdays at 15:00.
        for i in range(12):
            published(workspace, account, (NOW - timedelta(days=7 * (i + 1))).replace(hour=9), likes=80)
        for i in range(12):
            published(workspace, account, (NOW - timedelta(days=7 * (i + 1) - 2)).replace(hour=15), likes=5)
        result = best_times.compute(account, NOW)
        assert result.basis == BestTimeSnapshot.Basis.POSTS
        assert result.sample_size == 24
        assert result.basis_label == "Based on your 24 posts"
        top = result.top(2)
        assert (top[0].weekday, top[0].hour) == (0, 9)
        assert top[0].score == 100
        assert result.scores[best_times.cell_index(2, 15)] < 20

    def test_too_few_posts_falls_back_to_labelled_defaults(self, workspace, account):
        seed(workspace, account, best_times.MIN_POSTS - 1)
        result = best_times.compute(account, NOW)
        assert result.is_default
        assert result.sample_size == 0
        assert result.basis_label == "Based on industry defaults"
        assert result.top(3)  # defaults still suggest something

    def test_no_posts_uses_the_providers_windows(self, account):
        result = best_times.compute(account, NOW)
        assert result.is_default
        # Bluesky's first window is weekday mornings 08-10.
        assert result.scores[best_times.cell_index(1, 9)] == 100
        assert result.scores[best_times.cell_index(6, 3)] == 0

    def test_posts_without_any_metrics_do_not_count(self, workspace, account):
        for i in range(best_times.MIN_POSTS + 2):
            make_post(workspace, account, NOW - timedelta(days=i + 1))
        assert best_times.compute(account, NOW).is_default

    def test_recent_posts_carry_more_weight_than_old_ones(self):
        tz = zoneinfo.ZoneInfo("UTC")
        old = (NOW - timedelta(days=170)).replace(hour=8)
        new = (NOW - timedelta(days=3)).replace(hour=20)
        filler = (NOW - timedelta(days=4)).replace(hour=14)
        samples = [(old, 0.8)] * 5 + [(new, 0.8)] * 5 + [(filler, 0.2)] * 5
        scores, _ = best_times.score_posts(samples, tz, NOW)
        # Same rate, but five recent posts are firmer evidence than five from six months ago.
        assert scores[best_times.cell_index(new.weekday(), 20)] > scores[best_times.cell_index(old.weekday(), 8)]

    def test_one_lucky_post_is_pulled_towards_the_average(self):
        tz = zoneinfo.ZoneInfo("UTC")
        steady = [((NOW - timedelta(days=7 * i)).replace(hour=9), 0.02) for i in range(1, 15)]
        lucky_at = (NOW - timedelta(days=2)).replace(hour=22)
        scores, counts = best_times.score_posts([*steady, (lucky_at, 0.05)], tz, NOW)
        lucky_cell = best_times.cell_index(lucky_at.weekday(), 22)
        steady_cell = best_times.cell_index(NOW.weekday(), 9)
        assert counts[lucky_cell] == 1
        # Unshrunk the lucky slot would be 2.5x the steady one; with the prior it is
        # nowhere near that, so the steady slot (14 posts) keeps a comparable score.
        assert scores[steady_cell] > 100 / 2.5

    def test_published_hour_follows_the_workspace_timezone(self, workspace, account):
        workspace.timezone = "America/New_York"
        workspace.save()
        # 13:00 UTC in June is 09:00 in New York.
        for i in range(best_times.MIN_POSTS):
            published(workspace, account, (NOW - timedelta(days=7 * (i + 1))).replace(hour=13))
        result = best_times.compute(account, NOW)
        assert result.timezone == "America/New_York"
        assert result.scores[best_times.cell_index(0, 9)] == 100
        assert result.scores[best_times.cell_index(0, 13)] == 0

    def test_dst_change_keeps_the_same_local_hour_in_one_cell(self):
        ny = zoneinfo.ZoneInfo("America/New_York")
        now = datetime(2026, 4, 30, 12, tzinfo=UTC)
        before = datetime(2026, 3, 2, 9, 0, tzinfo=ny)  # EST, Monday
        after = datetime(2026, 3, 16, 9, 0, tzinfo=ny)  # EDT, Monday
        assert before.astimezone(UTC).hour != after.astimezone(UTC).hour
        scores, counts = best_times.score_posts([(before, 0.1), (after, 0.1)], ny, now)
        assert counts[best_times.cell_index(0, 9)] == 2
        assert sum(counts) == 2


class TestTop:
    def test_top_spreads_across_days_and_hours(self):
        scores = [0.0] * best_times.CELLS
        for hour in (9, 10, 11):
            scores[best_times.cell_index(1, hour)] = 100 - hour  # adjacent hours on Tuesday
        scores[best_times.cell_index(3, 14)] = 50
        result = best_times.BestTimes("posts", 30, "UTC", scores, [1] * best_times.CELLS)
        picked = [(t.weekday, t.hour) for t in result.top(5)]
        assert (1, 9) in picked and (1, 10) not in picked and (1, 11) in picked and (3, 14) in picked

    def test_upcoming_returns_future_datetimes_in_the_workspace_zone(self):
        ny = zoneinfo.ZoneInfo("America/New_York")
        scores = [0.0] * best_times.CELLS
        scores[best_times.cell_index(0, 9)] = 100  # Monday 09:00
        result = best_times.BestTimes("posts", 30, "America/New_York", scores, [1] * best_times.CELLS)
        # NOW is Monday 08:00 in New York: this morning's 09:00, then next Monday's.
        found = best_times.upcoming(result, 2, after=NOW)
        assert [when for when, _ in found] == [
            datetime(2026, 6, 15, 9, tzinfo=ny),
            datetime(2026, 6, 22, 9, tzinfo=ny),
        ]
        assert all(when > NOW for when, _ in found)


@pytest.mark.django_db
class TestSnapshotCache:
    def test_refresh_is_idempotent_and_stores_the_basis(self, account):
        first = best_times.refresh(account, NOW)
        second = best_times.refresh(account, NOW)
        assert BestTimeSnapshot.objects.filter(social_account=account).count() == 1
        assert first.scores == second.scores
        assert BestTimeSnapshot.objects.get(social_account=account).basis == "defaults"

    def test_get_reads_the_cache_without_recomputing(self, account, monkeypatch):
        best_times.refresh(account, NOW)

        def boom(*args, **kwargs):
            raise AssertionError("recomputed")

        monkeypatch.setattr(best_times, "compute", boom)
        assert best_times.get_best_times(account).is_default

    def test_get_recomputes_when_missing_stale_or_timezone_changed(self, workspace, account):
        assert best_times.get_best_times(account).timezone == "UTC"
        assert BestTimeSnapshot.objects.filter(social_account=account).exists()
        workspace.timezone = "Europe/Paris"
        workspace.save()
        account.refresh_from_db()
        assert best_times.get_best_times(account).timezone == "Europe/Paris"
        BestTimeSnapshot.objects.filter(social_account=account).update(computed_at=timezone.now() - timedelta(days=2))
        before = BestTimeSnapshot.objects.get(social_account=account).computed_at
        best_times.get_best_times(account)
        assert BestTimeSnapshot.objects.get(social_account=account).computed_at > before

    def test_refresh_all_covers_connected_accounts_and_survives_one_failure(self, workspace, account, monkeypatch):
        other = make_account(workspace, "mastodon", "masto")
        make_account(workspace, "x", "gone", status="disconnected")
        real = best_times.refresh

        def flaky(acc, now=None):
            if acc.pk == account.pk:
                raise RuntimeError("boom")
            return real(acc, now)

        monkeypatch.setattr(best_times, "refresh", flaky)
        assert best_times.refresh_all() == 1
        assert BestTimeSnapshot.objects.filter(social_account=other).exists()
        assert BestTimeSnapshot.objects.count() == 1

    def test_the_daily_task_is_registered(self, db):
        from background_task.models import Task

        from apps.analytics.apps import AnalyticsConfig

        AnalyticsConfig._register_sync_task(None)
        AnalyticsConfig._register_sync_task(None)
        assert Task.objects.filter(verbose_name="refresh_best_times").count() == 1


def test_every_provider_declares_or_inherits_windows():
    from providers import PROVIDER_REGISTRY, best_time_windows_for

    for platform in PROVIDER_REGISTRY:
        windows = best_time_windows_for(platform)
        assert windows, platform
        for window in windows:
            assert all(0 <= d <= 6 for d in window.days)
            assert all(0 <= h <= 23 for h in window.hours)
