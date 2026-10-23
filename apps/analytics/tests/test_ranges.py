"""Date-range parsing: presets, custom ranges, comparison and the workspace timezone."""

from datetime import UTC, date, datetime

import pytest

from apps.analytics.ranges import MAX_CUSTOM_DAYS, parse_range
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace

NOW = datetime(2026, 10, 23, 23, 30, tzinfo=UTC)


@pytest.fixture
def workspace(db):
    return Workspace.objects.create(organization=Organization.objects.create(name="Org"), name="WS", timezone="UTC")


def test_defaults_to_30_days_with_comparison(workspace):
    rng = parse_range({}, workspace, now=NOW)
    assert (rng.start, rng.end, rng.days, rng.preset, rng.compare) == (
        date(2026, 9, 24),
        date(2026, 10, 23),
        30,
        "30",
        True,
    )


@pytest.mark.parametrize("value", ["7", "90"])
def test_presets(workspace, value):
    assert parse_range({"range": value}, workspace, now=NOW).days == int(value)


@pytest.mark.parametrize("value", ["14", "abc", "-3", ""])
def test_unknown_preset_falls_back(workspace, value):
    assert parse_range({"range": value}, workspace, now=NOW).days == 30


def test_custom_range_is_inclusive_and_has_an_equal_previous_period(workspace):
    rng = parse_range({"range": "custom", "start": "2026-10-01", "end": "2026-10-10"}, workspace, now=NOW)
    assert rng.days == 10 and rng.is_custom
    assert (rng.previous_start, rng.previous_end) == (date(2026, 9, 21), date(2026, 9, 30))


def test_custom_range_is_clamped_not_rejected(workspace):
    future = parse_range({"range": "custom", "start": "2026-10-20", "end": "2027-01-01"}, workspace, now=NOW)
    assert future.end == date(2026, 10, 23)
    swapped = parse_range({"range": "custom", "start": "2026-10-10", "end": "2026-10-01"}, workspace, now=NOW)
    assert (swapped.start, swapped.end) == (date(2026, 10, 1), date(2026, 10, 10))
    huge = parse_range({"range": "custom", "start": "2020-01-01", "end": "2026-10-23"}, workspace, now=NOW)
    assert huge.days == MAX_CUSTOM_DAYS


def test_custom_with_a_bad_date_falls_back_to_the_default(workspace):
    assert parse_range({"range": "custom", "start": "nope", "end": "2026-10-10"}, workspace, now=NOW).days == 30


def test_compare_can_be_switched_off(workspace):
    assert parse_range({"compare": "none"}, workspace, now=NOW).compare is False


def test_today_is_the_workspace_local_date(workspace):
    workspace.timezone = "Pacific/Auckland"  # UTC+13 in October: 23:30 UTC is already the 24th
    rng = parse_range({"range": "7"}, workspace, now=NOW)
    assert rng.end == date(2026, 10, 24)
    start, stop = rng.bounds(rng.end, rng.end)
    assert start == datetime(2026, 10, 23, 11, 0, tzinfo=UTC) and stop == datetime(2026, 10, 24, 11, 0, tzinfo=UTC)


def test_unknown_timezone_falls_back_to_utc(workspace):
    workspace.timezone = "Mars/Olympus"
    assert parse_range({}, workspace, now=NOW).end == date(2026, 10, 23)


def test_querystring_round_trips(workspace):
    rng = parse_range(
        {"range": "custom", "start": "2026-10-01", "end": "2026-10-10", "compare": "none"}, workspace, now=NOW
    )
    qs = rng.querystring({"accounts": ["a", "b"]}, metric="engagements")
    assert qs == "?range=custom&start=2026-10-01&end=2026-10-10&compare=none&accounts=a&accounts=b&metric=engagements"
    again = parse_range(dict(pair.split("=") for pair in qs[1:].split("&")), workspace, now=NOW)
    assert (again.start, again.end, again.compare) == (rng.start, rng.end, False)
