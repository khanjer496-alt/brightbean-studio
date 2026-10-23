"""Definition validation and the period maths."""

from __future__ import annotations

from datetime import UTC, date, datetime, time

import pytest
from django.core.exceptions import ValidationError

from apps.reports import periods
from apps.reports.models import ReportDefinition
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db


def make(workspace, **kwargs):
    return ReportDefinition(workspace=workspace, name=kwargs.pop("name", "R"), **kwargs)


class TestValidation:
    def test_valid_definition(self, workspace):
        d = make(workspace, recipients=["A@Example.com", "a@example.com"])
        d.full_clean()
        assert d.recipients == ["a@example.com"]

    @pytest.mark.parametrize("sections", [[], ["nope"], ["summary", "summary"], "summary"])
    def test_sections_must_be_known_and_unique(self, workspace, sections):
        with pytest.raises(ValidationError) as exc:
            make(workspace, sections=sections).full_clean()
        assert "sections" in exc.value.message_dict

    def test_bad_email_rejected(self, workspace):
        with pytest.raises(ValidationError) as exc:
            make(workspace, recipients=["not-an-email"]).full_clean()
        assert "recipients" in exc.value.message_dict

    def test_too_many_recipients(self, workspace):
        with pytest.raises(ValidationError):
            make(workspace, recipients=[f"u{i}@example.com" for i in range(11)]).full_clean()

    def test_custom_range_needs_ordered_dates(self, workspace):
        with pytest.raises(ValidationError):
            make(workspace, range_type="custom").full_clean()
        with pytest.raises(ValidationError):
            make(
                workspace, range_type="custom", custom_start=date(2026, 5, 2), custom_end=date(2026, 5, 1)
            ).full_clean()
        make(workspace, range_type="custom", custom_start=date(2026, 5, 1), custom_end=date(2026, 5, 31)).full_clean()

    def test_custom_range_cannot_be_scheduled(self, workspace):
        d = make(
            workspace,
            range_type="custom",
            custom_start=date(2026, 5, 1),
            custom_end=date(2026, 5, 31),
            frequency="weekly",
            recipients=["a@example.com"],
        )
        with pytest.raises(ValidationError) as exc:
            d.full_clean()
        assert "frequency" in exc.value.message_dict

    def test_schedule_needs_recipients_and_valid_day(self, workspace):
        with pytest.raises(ValidationError) as exc:
            make(workspace, frequency="weekly").full_clean()
        assert "recipients" in exc.value.message_dict
        with pytest.raises(ValidationError) as exc:
            make(workspace, frequency="monthly", day_of_month=31, recipients=["a@example.com"]).full_clean()
        assert "day_of_month" in exc.value.message_dict

    def test_accent_must_be_hex(self, workspace):
        with pytest.raises(ValidationError):
            make(workspace, accent_color="red").full_clean()


class TestPeriods:
    def test_last_n_days_end_yesterday(self, workspace):
        d = make(workspace, range_type="last_7")
        rng = periods.resolve_range(d, now=datetime(2026, 10, 14, 12, tzinfo=UTC))
        assert (rng.start, rng.end) == (date(2026, 10, 7), date(2026, 10, 13))

    def test_last_month(self, workspace):
        d = make(workspace, range_type="last_month")
        rng = periods.resolve_range(d, now=datetime(2026, 3, 1, 0, 30, tzinfo=UTC))
        assert (rng.start, rng.end) == (date(2026, 2, 1), date(2026, 2, 28))

    def test_workspace_timezone_decides_today(self, workspace):
        workspace.timezone = "Pacific/Auckland"  # UTC+13 in October
        d = make(workspace, range_type="last_month")
        # 2026-10-31 20:00 UTC is already 1 Nov 09:00 in Auckland, so "last month" is October.
        rng = periods.resolve_range(d, now=datetime(2026, 10, 31, 20, tzinfo=UTC))
        assert (rng.start, rng.end) == (date(2026, 10, 1), date(2026, 10, 31))
        # The same instant in UTC is still October, so last month is September.
        workspace.timezone = "UTC"
        rng = periods.resolve_range(d, now=datetime(2026, 10, 31, 20, tzinfo=UTC))
        assert (rng.start, rng.end) == (date(2026, 9, 1), date(2026, 9, 30))

    def test_previous_period_is_equal_length(self, workspace):
        rng = periods.resolve_range(make(workspace, range_type="last_30"), now=datetime(2026, 10, 14, tzinfo=UTC))
        assert rng.days == 30 and rng.previous_end == date(2026, 9, 13)


class TestSlots:
    def weekly(self, workspace, **kw):
        return make(workspace, frequency="weekly", weekday=0, send_time=time(9), recipients=["a@example.com"], **kw)

    def test_not_due_before_fire_time(self, workspace):
        assert periods.due_slot(self.weekly(workspace), datetime(2026, 10, 19, 8, 59, tzinfo=UTC)) is None

    def test_due_after_and_late_in_same_week(self, workspace):
        d = self.weekly(workspace)
        key, _ = periods.due_slot(d, datetime(2026, 10, 19, 9, 1, tzinfo=UTC))
        assert key == "W2026-43"
        assert periods.due_slot(d, datetime(2026, 10, 22, 9, tzinfo=UTC))[0] == key

    def test_fire_time_is_in_workspace_tz(self, workspace):
        workspace.timezone = "America/New_York"
        d = self.weekly(workspace)
        # 09:00 EDT on Monday 19 Oct is 13:00 UTC.
        assert periods.due_slot(d, datetime(2026, 10, 19, 12, 59, tzinfo=UTC)) is None
        assert periods.due_slot(d, datetime(2026, 10, 19, 13, 0, tzinfo=UTC)) is not None

    def test_monthly(self, workspace):
        d = make(workspace, frequency="monthly", day_of_month=5, send_time=time(7), recipients=["a@example.com"])
        assert periods.due_slot(d, datetime(2026, 10, 4, 23, tzinfo=UTC)) is None
        assert periods.due_slot(d, datetime(2026, 10, 5, 7, tzinfo=UTC))[0] == "M2026-10"

    def test_slot_before_anchor_is_skipped(self, workspace):
        d = self.weekly(workspace, schedule_anchor=datetime(2026, 10, 21, tzinfo=UTC))
        assert periods.due_slot(d, datetime(2026, 10, 22, tzinfo=UTC)) is None

    def test_next_fire(self, workspace):
        d = self.weekly(workspace)
        assert periods.next_fire(d, datetime(2026, 10, 19, 10, tzinfo=UTC)) == datetime(2026, 10, 26, 9, tzinfo=UTC)
        m = make(workspace, frequency="monthly", day_of_month=1, recipients=["a@example.com"])
        assert periods.next_fire(m, datetime(2026, 12, 15, tzinfo=UTC)) == datetime(2027, 1, 1, 9, tzinfo=UTC)
        assert periods.next_fire(make(workspace), datetime(2026, 10, 1, tzinfo=UTC)) is None


def test_workspace_fixture_is_ours(workspace):
    assert isinstance(workspace, Workspace)
