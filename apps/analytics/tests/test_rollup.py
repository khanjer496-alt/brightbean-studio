"""Workspace rollups: aggregation with partial data, comparison math, timezones, query counts."""

from datetime import UTC, date, datetime, timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.analytics import rollup
from apps.analytics.ranges import parse_range
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace

from .helpers import add_daily, fill_days, make_account, make_post

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)


@pytest.fixture
def workspace(db):
    return Workspace.objects.create(organization=Organization.objects.create(name="Org"), name="WS", timezone="UTC")


@pytest.fixture
def rng(workspace):
    return parse_range({"range": "7"}, workspace, now=NOW)  # Oct 17 - Oct 23, previous Oct 10 - Oct 16


def _kpi(kpis, key):
    return next(k for k in kpis if k["key"] == key)


@pytest.fixture
def mixed(workspace, rng):
    """Instagram reports everything, LinkedIn only impressions, TikTok nothing, Bluesky has no API."""
    ig = make_account(workspace, "instagram", "ig", followers=1000)
    li = make_account(workspace, "linkedin_company", "li", followers=500)
    tt = make_account(workspace, "tiktok", "tt")
    bs = make_account(workspace, "bluesky", "bs", followers=40)
    fill_days(ig, "views", rng.start, rng.end, 100)
    fill_days(ig, "likes", rng.start, rng.end, 10)
    fill_days(ig, "comments", rng.start, rng.end, 5)
    fill_days(li, "impressions", rng.start, rng.end, 50)
    return ig, li, tt, bs


@pytest.mark.django_db
def test_totals_sum_only_the_accounts_that_report(mixed, rng):
    rows = rollup.collect(list(mixed), rng)
    kpis = rollup.kpis(rows, compare=False)

    exposure = _kpi(kpis, "exposure")
    assert exposure["value"] == 7 * 100 + 7 * 50
    assert (exposure["reporting"], exposure["total"]) == (2, 4)
    engagements = _kpi(kpis, "engagements")
    assert engagements["value"] == 7 * 15  # LinkedIn reports no engagement parts: absent, not zero
    assert engagements["reporting"] == 1
    # The rate only divides what the same accounts report on both sides.
    assert _kpi(kpis, "engagement_rate")["value"] == pytest.approx(7 * 15 / (7 * 100) * 100)
    assert _kpi(kpis, "followers")["value"] == 1540


@pytest.mark.django_db
def test_an_account_without_data_is_not_available_rather_than_zero(mixed, rng):
    ig, li, tt, bs = mixed
    rows = {r.account.id: r for r in rollup.collect([ig, li, tt, bs], rng)}

    assert rows[tt.id].current.exposure is None
    assert rows[tt.id].current.engagements is None
    assert rows[tt.id].followers is None  # follower_count 0 means "not known", not "no followers"
    assert rows[bs.id].unavailable  # Bluesky exposes no analytics
    assert rows[bs.id].current.exposure is None
    assert rows[li.id].current.engagements is None
    assert rows[ig.id].current.exposure == 700


@pytest.mark.django_db
def test_a_metric_nobody_reports_has_no_value(workspace, rng):
    only_bluesky = [make_account(workspace, "bluesky", "bs")]
    kpis = rollup.kpis(rollup.collect(only_bluesky, rng), compare=True)
    for key in ("exposure", "engagements", "engagement_rate", "new_followers", "followers"):
        assert _kpi(kpis, key)["value"] is None
    assert _kpi(kpis, "posts")["value"] == 0  # a post count is always known


@pytest.mark.django_db
def test_a_real_zero_is_kept(workspace, rng):
    ig = make_account(workspace, "instagram", "ig")
    fill_days(ig, "views", rng.start, rng.end, 0)
    assert _kpi(rollup.kpis(rollup.collect([ig], rng), compare=False), "exposure")["value"] == 0


@pytest.mark.django_db
def test_comparison_uses_only_accounts_present_in_both_periods(mixed, rng):
    ig, li, tt, bs = mixed
    fill_days(ig, "views", rng.previous_start, rng.previous_end, 50)  # 350 before, 700 now: +100%
    # LinkedIn has nothing before the range: it is in the total but cannot read as growth.
    rows = rollup.collect([ig, li], rng)
    exposure = _kpi(rollup.kpis(rows, compare=True), "exposure")

    assert exposure["value"] == 1050
    assert exposure["previous"] == 350
    assert exposure["delta"] == 100.0


@pytest.mark.django_db
def test_an_account_connected_part_way_through_the_previous_period_cannot_read_as_growth(mixed, rng):
    ig, li, tt, bs = mixed
    # Instagram's history starts on the last day of the previous period: 100 then, 700 now.
    add_daily(ig, "views", {rng.previous_end: 100.0})
    fill_days(li, "impressions", rng.previous_start, rng.previous_end, 50)  # fully reported: flat
    rows = rollup.collect([ig, li], rng)

    assert _kpi(rollup.kpis(rows, compare=True), "exposure")["delta"] == 0.0  # LinkedIn alone
    ig_cells = {c["key"]: c for c in rollup.account_cells(next(r for r in rows if r.account == ig), True)}
    assert ig_cells["exposure"]["delta"] is None


@pytest.mark.django_db
def test_a_lagging_current_period_still_compares(workspace, rng):
    yt = make_account(workspace, "youtube", "yt")
    fill_days(yt, "views", rng.previous_start, rng.previous_end, 10)
    fill_days(yt, "views", rng.start, rng.end - timedelta(days=2), 10)  # last two days not reported yet
    exposure = _kpi(rollup.kpis(rollup.collect([yt], rng), compare=True), "exposure")
    assert exposure["delta"] == pytest.approx((50 - 70) / 70 * 100, abs=0.1)


@pytest.mark.django_db
def test_no_baseline_means_no_delta(mixed, rng):
    exposure = _kpi(rollup.kpis(rollup.collect(list(mixed), rng), compare=True), "exposure")
    assert exposure["delta"] is None


@pytest.mark.django_db
def test_comparison_off_skips_the_previous_period(mixed, workspace):
    rng = parse_range({"range": "7", "compare": "none"}, workspace, now=NOW)
    exposure = _kpi(rollup.kpis(rollup.collect(list(mixed), rng), compare=False), "exposure")
    assert exposure["previous"] is None and exposure["delta"] is None


@pytest.mark.django_db
def test_posts_are_counted_per_local_day_in_the_workspace_timezone(workspace):
    workspace.timezone = "Pacific/Auckland"  # UTC+13
    workspace.save()
    rng = parse_range({"range": "7"}, workspace, now=NOW)  # local today is Oct 24
    ig = make_account(workspace, "instagram", "ig")
    # 23:30 UTC on the 22nd is 12:30 on the 23rd locally: inside the range.
    make_post(workspace, ig, datetime(2026, 10, 22, 23, 30, tzinfo=UTC))
    # 12:30 UTC on the 24th is 01:30 on the 25th locally: after the range ends.
    make_post(workspace, ig, datetime(2026, 10, 24, 12, 30, tzinfo=UTC))

    assert rollup.daily_posts([ig], rng) == {date(2026, 10, 23): 1}
    assert rollup.collect([ig], rng)[0].current.posts == 1


@pytest.mark.django_db
def test_youtube_counts_are_shown_but_never_feed_engagement(workspace, rng):
    yt = make_account(workspace, "youtube", "yt")
    fill_days(yt, "views", rng.start, rng.end, 10)
    fill_days(yt, "likes", rng.start, rng.end, 2)
    row = rollup.collect([yt], rng)[0]
    assert row.current.exposure == 70
    assert row.current.engagements is None and row.current.rate is None


@pytest.mark.django_db
def test_per_post_estimates_fill_in_for_platforms_without_account_series(workspace, rng):
    tt = make_account(workspace, "tiktok", "tt")
    published = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
    make_post(
        workspace,
        tt,
        published,
        snapshots={"views": [(date(2026, 10, 18), 100.0, published), (date(2026, 10, 19), 160.0, published)]},
    )
    row = rollup.collect([tt], rng)[0]
    assert row.current.exposure == 160  # 100 on the publish day + 60 gained the next


@pytest.mark.django_db
def test_new_followers_from_daily_gains_and_from_lifetime_totals(workspace, rng):
    fb = make_account(workspace, "facebook", "fb")
    ig = make_account(workspace, "instagram", "ig")
    fill_days(fb, "follows", rng.start, rng.end, 3)
    add_daily(ig, "followers", {rng.start: 100.0, rng.start + timedelta(days=3): 110.0, rng.end: 125.0})
    rows = {r.account.id: r for r in rollup.collect([fb, ig], rng)}
    assert rows[fb.id].current.new_followers == 21
    assert rows[ig.id].current.new_followers == 25


@pytest.mark.django_db
def test_series_leave_unreported_days_empty(workspace, rng):
    ig = make_account(workspace, "instagram", "ig")
    add_daily(ig, "views", {rng.start + timedelta(days=2): 5.0, rng.start + timedelta(days=4): 7.0})
    values = rollup.collect([ig], rng)[0].series("exposure", rng.day_list())
    assert values == [None, None, 5.0, 0.0, 7.0, None, None]


@pytest.mark.django_db
def test_trend_combines_accounts_and_aligns_the_previous_period(mixed, rng):
    ig, li, _, _ = mixed
    fill_days(ig, "views", rng.previous_start, rng.previous_end, 10)
    trend = rollup.trend(rollup.collect([ig, li], rng), rng, "exposure")
    current, previous = trend["datasets"]
    assert current["values"] == [150.0] * 7
    assert previous["values"] == [10.0] * 7
    assert len(trend["labels"]) == 7


@pytest.mark.django_db
def test_query_count_does_not_grow_with_accounts(workspace, rng):
    def build(n, prefix):
        accounts = []
        for i in range(n):
            account = make_account(workspace, "instagram", f"{prefix}{i}", followers=10)
            fill_days(account, "views", rng.start, rng.end, 1)
            make_post(workspace, account, datetime(2026, 10, 20, 9, 0, tzinfo=UTC))
            accounts.append(account)
        return accounts

    def queries(accounts):
        with CaptureQueriesContext(connection) as ctx:
            rollup.overview(accounts, rng)
        return len(ctx)

    few = queries(build(2, "a"))
    many = queries(build(8, "b") + [make_account(workspace, "facebook", "fb"), make_account(workspace, "tiktok", "tt")])
    assert many == few


@pytest.mark.django_db
def test_top_posts_skip_posts_without_the_figure_and_formats_group(workspace, rng):
    ig = make_account(workspace, "instagram", "ig")
    yt = make_account(workspace, "youtube", "yt")
    when = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
    big = make_post(
        workspace,
        ig,
        when,
        "big",
        snapshots={"views": [(date(2026, 10, 20), 900.0, when)], "likes": [(date(2026, 10, 20), 90.0, when)]},
    )
    small = make_post(
        workspace,
        ig,
        when,
        "small",
        snapshots={"views": [(date(2026, 10, 20), 100.0, when)], "likes": [(date(2026, 10, 20), 5.0, when)]},
    )
    make_post(workspace, ig, when, "unmeasured")
    video = make_post(workspace, yt, when, "yt", snapshots={"views": [(date(2026, 10, 20), 5000.0, when)]})

    data = rollup.overview([ig, yt], rng)
    assert [r["post"].id for r in data["top_posts"]] == [video.id, big.id, small.id]

    by_engagement = rollup.overview([ig, yt], rng, top_sort="engagements")["top_posts"]
    assert [r["post"].id for r in by_engagement] == [big.id, small.id]  # YouTube posts carry no calculated engagement
    assert by_engagement[0]["rate"] == pytest.approx(10.0)
    text = next(f for f in data["formats"] if f["format"] == "Text")
    assert text["posts"] == 4 and text["avg_engagements"] == pytest.approx(47.5)
