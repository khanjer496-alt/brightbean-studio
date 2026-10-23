"""Recurring posts: weekdays, end conditions, the composer's Repeat control.

A series is one scheduled post plus a ``RecurrenceRule``; the generator clones
the post for each date the rule produces inside the lookahead window. These
cover the date math (weekdays, intervals, end date, count, timezones and DST),
editing and stopping a series, and the surfaces that expose it.
"""

import json
import zoneinfo
from datetime import date, datetime, time, timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.calendar.models import RecurrenceRule
from apps.calendar.services import (
    clean_recurrence,
    recurrence_summary,
    save_recurrence,
    stop_recurrence,
)
from apps.calendar.tasks import _compute_recurrence_dates, generate_occurrences, generate_recurring_posts
from apps.composer.models import PlatformPost, Post
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

UTC = zoneinfo.ZoneInfo("UTC")
NEW_YORK = zoneinfo.ZoneInfo("America/New_York")

MON, TUE, WED, THU, FRI, SAT, SUN = range(7)


class ComputeRecurrenceDatesTests(TestCase):
    """``_compute_recurrence_dates`` — the date sequence a rule produces."""

    def test_weekly_on_chosen_weekdays(self):
        # 2026-03-03 is a Tuesday; the same week's Thursday is the first copy.
        dates = _compute_recurrence_dates(date(2026, 3, 3), "weekly", 1, date(2026, 3, 17), weekdays=[TUE, THU])
        self.assertEqual(
            dates,
            [date(2026, 3, 5), date(2026, 3, 10), date(2026, 3, 12), date(2026, 3, 17)],
        )

    def test_weekdays_before_the_first_post_in_its_week_are_skipped(self):
        # First post on a Wednesday: that week's Monday has already passed.
        dates = _compute_recurrence_dates(date(2026, 3, 4), "weekly", 1, date(2026, 3, 10), weekdays=[MON, FRI])
        self.assertEqual(dates, [date(2026, 3, 6), date(2026, 3, 9)])

    def test_every_other_week_counts_from_the_first_posts_week(self):
        dates = _compute_recurrence_dates(date(2026, 3, 2), "weekly", 2, date(2026, 3, 31), weekdays=[MON, WED])
        self.assertEqual(
            dates,
            [date(2026, 3, 4), date(2026, 3, 16), date(2026, 3, 18), date(2026, 3, 30)],
        )

    def test_weekly_without_weekdays_keeps_the_first_posts_weekday(self):
        """Rules saved before weekdays existed are unchanged."""
        dates = _compute_recurrence_dates(date(2026, 3, 3), "weekly", 1, date(2026, 3, 24))
        self.assertEqual(dates, [date(2026, 3, 10), date(2026, 3, 17), date(2026, 3, 24)])

    def test_end_date_is_inclusive(self):
        dates = _compute_recurrence_dates(date(2026, 3, 1), "daily", 3, date(2026, 3, 10))
        self.assertEqual(dates, [date(2026, 3, 4), date(2026, 3, 7), date(2026, 3, 10)])

    def test_count_includes_the_first_post(self):
        dates = _compute_recurrence_dates(
            date(2026, 3, 3), "weekly", 1, date(2026, 12, 31), weekdays=[TUE, THU], count=5
        )
        self.assertEqual(
            dates,
            [date(2026, 3, 5), date(2026, 3, 10), date(2026, 3, 12), date(2026, 3, 17)],
        )

    def test_count_is_measured_from_the_start_of_the_series(self):
        """A later run with a later window still stops at the same post."""
        early = _compute_recurrence_dates(date(2026, 1, 1), "daily", 1, date(2026, 1, 3), count=10)
        late = _compute_recurrence_dates(date(2026, 1, 1), "daily", 1, date(2026, 6, 1), count=10)
        self.assertEqual(early, [date(2026, 1, 2), date(2026, 1, 3)])
        self.assertEqual(late[-1], date(2026, 1, 10))
        self.assertEqual(len(late), 9)

    def test_monthly_on_the_31st_returns_to_the_31st_after_a_short_month(self):
        dates = _compute_recurrence_dates(date(2026, 1, 31), "monthly", 1, date(2026, 4, 30))
        self.assertEqual(dates, [date(2026, 2, 28), date(2026, 3, 31), date(2026, 4, 30)])

    def test_a_long_running_daily_series_still_reaches_today(self):
        """The old 180-step cap stopped a daily series half a year in."""
        start = date(2025, 1, 1)
        dates = _compute_recurrence_dates(start, "daily", 1, date(2026, 3, 1))
        self.assertEqual(dates[-1], date(2026, 3, 1))


class CleanRecurrenceTests(TestCase):
    start = datetime(2026, 3, 3, 9, 0, tzinfo=NEW_YORK)  # a Tuesday

    def test_weekly_without_weekdays_takes_the_first_posts_weekday(self):
        fields = clean_recurrence("weekly", start=self.start)
        self.assertEqual(fields["weekdays"], [TUE])

    def test_weekdays_are_deduplicated_and_sorted(self):
        fields = clean_recurrence("weekly", weekdays=["3", 1, 3], start=self.start)
        self.assertEqual(fields["weekdays"], [TUE, THU])

    def test_weekdays_are_dropped_for_other_frequencies(self):
        self.assertEqual(clean_recurrence("daily", weekdays=[1], start=self.start)["weekdays"], [])

    def test_refusals(self):
        cases = {
            "end on a date or after a number": dict(end_date=date(2026, 4, 1), count=3),
            "at least 2 posts": dict(count=1),
            "between 1 and 99": dict(interval=0),
            "whole number": dict(interval="often"),
            "0 (Monday) to 6": dict(weekdays=[7]),
            "can't end before its first post": dict(end_date=date(2026, 3, 2)),
        }
        for message, kwargs in cases.items():
            with self.subTest(message=message), self.assertRaisesMessage(ValueError, message):
                clean_recurrence("weekly", start=self.start, **kwargs)
        with self.assertRaisesMessage(ValueError, "daily, weekly or monthly"):
            clean_recurrence("yearly", start=self.start)


class RecurrenceSummaryTests(TestCase):
    def test_wording(self):
        this_year = timezone.localdate().year
        cases = [
            (dict(frequency="daily"), "Every day"),
            (dict(frequency="daily", interval=3), "Every 3 days"),
            (dict(frequency="weekly", weekdays=[TUE, THU]), "Every Tuesday and Thursday"),
            (
                dict(frequency="weekly", interval=2, weekdays=[MON, WED, FRI]),
                "Every 2 weeks on Monday, Wednesday and Friday",
            ),
            (dict(frequency="weekly"), "Every week"),
            (dict(frequency="monthly", interval=2, count=6), "Every 2 months, 6 times"),
            (
                dict(frequency="weekly", weekdays=[TUE, THU], end_date=date(this_year, 12, 31)),
                "Every Tuesday and Thursday until 31 Dec",
            ),
            (dict(frequency="daily", end_date=date(this_year + 1, 1, 5)), f"Every day until 5 Jan {this_year + 1}"),
        ]
        for kwargs, expected in cases:
            with self.subTest(expected=expected):
                frequency = kwargs.pop("frequency")
                self.assertEqual(recurrence_summary(frequency, **kwargs), expected)


class _SeriesMixin:
    def _workspace(self, tz="America/New_York"):
        self.org = Organization.objects.create(name="Series Org")
        self.ws = Workspace.objects.create(organization=self.org, name="Series WS", timezone=tz)
        self.account = SocialAccount.objects.create(
            workspace=self.ws,
            platform="linkedin_personal",
            account_platform_id="li-series",
            account_name="Series Account",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )

    def _source(self, scheduled_at, *, caption="series", status="scheduled"):
        post = Post.objects.create(workspace=self.ws, caption=caption, title="Weekly tip", scheduled_at=scheduled_at)
        PlatformPost.objects.create(post=post, social_account=self.account, status=status, scheduled_at=scheduled_at)
        return post

    def _copies(self, source):
        return list(Post.objects.filter(recurrence_source=source).order_by("scheduled_at"))


class GenerateOccurrencesTests(_SeriesMixin, TestCase):
    def setUp(self):
        self._workspace()

    def test_weekdays_are_local_to_the_workspace(self):
        """A 23:30 Tuesday post in New York is Wednesday in UTC; Tuesday wins."""
        source = self._source(datetime(2026, 3, 3, 23, 30, tzinfo=NEW_YORK))
        rule = RecurrenceRule.objects.create(
            post=source, frequency="weekly", weekdays=[TUE, THU], end_date=date(2026, 3, 12)
        )

        created = generate_occurrences(rule, now=datetime(2026, 3, 1, 12, tzinfo=UTC))

        self.assertEqual(created, 3)
        local = [c.scheduled_at.astimezone(NEW_YORK) for c in self._copies(source)]
        self.assertEqual([d.date() for d in local], [date(2026, 3, 5), date(2026, 3, 10), date(2026, 3, 12)])
        self.assertTrue(all(d.time() == time(23, 30) for d in local))

    def test_local_time_survives_the_dst_change_on_chosen_weekdays(self):
        # US clocks go forward on 2026-03-08: EST (UTC-5) before, EDT (UTC-4) after.
        source = self._source(datetime(2026, 3, 2, 9, 0, tzinfo=NEW_YORK))
        rule = RecurrenceRule.objects.create(
            post=source, frequency="weekly", weekdays=[MON, FRI], end_date=date(2026, 3, 16)
        )

        generate_occurrences(rule, now=datetime(2026, 3, 1, 12, tzinfo=UTC))

        utc_hours = {
            c.scheduled_at.astimezone(NEW_YORK).date(): c.scheduled_at.astimezone(UTC).hour
            for c in self._copies(source)
        }
        self.assertEqual(utc_hours[date(2026, 3, 6)], 14)  # 09:00 EST
        self.assertEqual(utc_hours[date(2026, 3, 9)], 13)  # 09:00 EDT
        self.assertEqual(utc_hours[date(2026, 3, 16)], 13)

    def test_count_holds_across_runs(self):
        source = self._source(datetime(2026, 3, 2, 9, 0, tzinfo=NEW_YORK))
        rule = RecurrenceRule.objects.create(post=source, frequency="daily", count=4)

        generate_occurrences(rule, now=datetime(2026, 3, 1, 12, tzinfo=UTC))
        generate_occurrences(rule, now=datetime(2026, 3, 20, 12, tzinfo=UTC))

        self.assertEqual(len(self._copies(source)), 3)  # 4 posts, the source included

    def test_end_date_stops_the_series(self):
        source = self._source(datetime(2026, 3, 2, 9, 0, tzinfo=NEW_YORK))
        rule = RecurrenceRule.objects.create(post=source, frequency="weekly", end_date=date(2026, 3, 20))

        generate_occurrences(rule, now=datetime(2026, 3, 1, 12, tzinfo=UTC))

        self.assertEqual(
            [c.scheduled_at.astimezone(NEW_YORK).date() for c in self._copies(source)],
            [date(2026, 3, 9), date(2026, 3, 16)],
        )

    def test_copies_link_to_the_series_and_keep_title_and_status(self):
        source = self._source(datetime(2026, 3, 2, 9, 0, tzinfo=NEW_YORK))
        rule = RecurrenceRule.objects.create(post=source, frequency="daily", count=2)

        generate_occurrences(rule, now=datetime(2026, 3, 1, 12, tzinfo=UTC))

        (copy,) = self._copies(source)
        self.assertEqual(copy.title, "Weekly tip")
        self.assertEqual(copy.series_rule, rule)
        self.assertEqual(copy.platform_posts.get().status, "scheduled")

    def test_generation_is_idempotent(self):
        source = self._source(datetime(2026, 3, 2, 9, 0, tzinfo=NEW_YORK))
        rule = RecurrenceRule.objects.create(post=source, frequency="daily", end_date=date(2026, 3, 6))
        now = datetime(2026, 3, 1, 12, tzinfo=UTC)

        self.assertEqual(generate_occurrences(rule, now=now), 4)
        self.assertEqual(generate_occurrences(rule, now=now), 0)

    def test_copies_made_before_the_link_are_still_recognised(self):
        """Pre-existing unlinked copies match by caption, so none are doubled."""
        source = self._source(datetime(2026, 3, 2, 9, 0, tzinfo=NEW_YORK))
        Post.objects.create(
            workspace=self.ws, caption="series", scheduled_at=datetime(2026, 3, 3, 9, 0, tzinfo=NEW_YORK)
        )
        rule = RecurrenceRule.objects.create(post=source, frequency="daily", end_date=date(2026, 3, 4))

        self.assertEqual(generate_occurrences(rule, now=datetime(2026, 3, 1, 12, tzinfo=UTC)), 1)

    def test_daily_sweep_skips_inactive_rules(self):
        source = self._source(datetime(2026, 3, 2, 9, 0, tzinfo=NEW_YORK))
        RecurrenceRule.objects.create(post=source, frequency="daily", count=3, is_active=False)

        self.assertEqual(generate_recurring_posts(), 0)


class EditAndStopSeriesTests(_SeriesMixin, TestCase):
    def setUp(self):
        self._workspace()
        start = timezone.localtime(timezone.now(), NEW_YORK) + timedelta(days=1)
        self.source = self._source(start.replace(hour=9, minute=0, second=0, microsecond=0))

    def test_save_recurrence_creates_the_rule_and_copies_at_once(self):
        rule = save_recurrence(self.source, frequency="daily", count=3)

        self.assertEqual(rule.weekdays, [])
        self.assertEqual(len(self._copies(self.source)), 2)

    def test_changing_the_rule_replaces_upcoming_copies_but_not_published_ones(self):
        save_recurrence(self.source, frequency="daily", count=4)
        published, upcoming, _ = self._copies(self.source)
        published.platform_posts.update(status="published")

        save_recurrence(self.source, frequency="weekly", count=3)

        remaining = self._copies(self.source)
        self.assertIn(published, remaining)
        self.assertNotIn(upcoming, remaining)
        # The published copy stays, and the weekly rule regenerates its own dates.
        self.assertEqual(len(remaining), 3)

    def test_saving_an_unchanged_rule_keeps_copies(self):
        save_recurrence(self.source, frequency="daily", count=3)
        before = {c.id for c in self._copies(self.source)}

        save_recurrence(self.source, frequency="daily", count=3)

        self.assertEqual({c.id for c in self._copies(self.source)}, before)

    def test_stop_removes_upcoming_copies(self):
        save_recurrence(self.source, frequency="daily", count=3)

        removed = stop_recurrence(self.source)

        self.assertEqual(removed, 2)
        self.assertFalse(RecurrenceRule.objects.get(post=self.source).is_active)
        self.assertEqual(self._copies(self.source), [])
        self.assertEqual(stop_recurrence(self.source), 0)

    def test_an_unscheduled_post_cannot_repeat(self):
        draft = Post.objects.create(workspace=self.ws, caption="draft")
        with self.assertRaisesMessage(ValueError, "Schedule the post"):
            save_recurrence(draft, frequency="daily")


class ComposerRepeatTests(_SeriesMixin, TestCase):
    """The composer's Repeat control, end to end through save_post."""

    def setUp(self):
        self._workspace()
        self.user = User.objects.create_user(email="repeat@example.com", password="pw", tos_accepted_at=timezone.now())
        OrgMembership.objects.create(user=self.user, organization=self.org, org_role=OrgMembership.OrgRole.OWNER)
        WorkspaceMembership.objects.create(
            user=self.user, workspace=self.ws, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
        )
        self.client.force_login(self.user)
        self.save_url = reverse("composer:save_post", kwargs={"workspace_id": self.ws.id})
        self.day = timezone.localtime(timezone.now(), NEW_YORK).date() + timedelta(days=2)

    def _form(self, **extra):
        data = {
            "action": "schedule",
            "caption": "repeat me",
            "tags": "",
            "selected_accounts": str(self.account.id),
            "scheduled_date": self.day.isoformat(),
            "scheduled_time": "09:00",
            "recurrence_panel": "1",
        }
        data.update(extra)
        return data

    def _post(self, url=None, **extra):
        return self.client.post(url or self.save_url, data=self._form(**extra), HTTP_HX_REQUEST="true")

    def test_scheduling_with_weekdays_and_an_end_date(self):
        end = self.day + timedelta(days=14)
        response = self.client.post(
            self.save_url,
            data={
                **self._form(recurrence_frequency="weekly", recurrence_end="on", recurrence_end_date=end.isoformat()),
                "recurrence_weekdays": [str(self.day.weekday()), str((self.day.weekday() + 2) % 7)],
            },
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 204, response.content)
        source = Post.objects.get(caption="repeat me", recurrence_source__isnull=True)
        rule = source.recurrence_rule
        self.assertEqual(rule.end_date, end)
        self.assertEqual(len(rule.weekdays), 2)
        copies = self._copies(source)
        self.assertTrue(copies)
        self.assertTrue(all(c.scheduled_at.astimezone(NEW_YORK).date() <= end for c in copies))
        self.assertTrue(all(c.platform_posts.get().status == "scheduled" for c in copies))

    def test_after_n_times(self):
        response = self._post(recurrence_frequency="daily", recurrence_end="after", recurrence_count="3")

        self.assertEqual(response.status_code, 204, response.content)
        source = Post.objects.get(caption="repeat me", recurrence_source__isnull=True)
        self.assertEqual(source.recurrence_rule.count, 3)
        self.assertEqual(len(self._copies(source)), 2)

    def test_an_invalid_setting_is_refused_before_anything_is_saved(self):
        response = self._post(recurrence_frequency="daily", recurrence_end="after", recurrence_count="1")

        self.assertEqual(response.status_code, 400)
        self.assertIn("at least 2 posts", json.loads(response.content)["errors"]["recurrence"])
        self.assertFalse(Post.objects.exists())

    def test_never_is_an_ordinary_post(self):
        self.assertEqual(self._post(recurrence_frequency="never").status_code, 204)
        self.assertFalse(RecurrenceRule.objects.exists())

    def test_draft_save_does_not_start_a_series(self):
        self._post(action="save_draft", recurrence_frequency="daily")
        self.assertFalse(RecurrenceRule.objects.exists())

    def test_setting_never_on_a_series_stops_it(self):
        self._post(recurrence_frequency="daily", recurrence_end="after", recurrence_count="3")
        source = Post.objects.get(caption="repeat me", recurrence_source__isnull=True)
        edit_url = reverse("composer:save_post_edit", kwargs={"workspace_id": self.ws.id, "post_id": source.id})

        response = self._post(edit_url, recurrence_frequency="never")

        self.assertEqual(response.status_code, 204, response.content)
        self.assertFalse(RecurrenceRule.objects.get(post=source).is_active)
        self.assertEqual(self._copies(source), [])

    def test_compose_page_shows_the_series_and_links_copies_to_it(self):
        self._post(recurrence_frequency="daily", recurrence_end="after", recurrence_count="2")
        source = Post.objects.get(caption="repeat me", recurrence_source__isnull=True)
        (copy,) = self._copies(source)

        source_html = self.client.get(
            reverse("composer:compose_edit", kwargs={"workspace_id": self.ws.id, "post_id": source.id})
        ).content.decode()
        copy_html = self.client.get(
            reverse("composer:compose_edit", kwargs={"workspace_id": self.ws.id, "post_id": copy.id})
        ).content.decode()

        self.assertIn('name="recurrence_panel"', source_html)
        self.assertIn('"frequency": "daily"', source_html)
        self.assertIn("Stop repeating", source_html)
        self.assertNotIn('name="recurrence_panel"', copy_html)
        self.assertIn("Edit the series", copy_html)
        self.assertIn(str(source.id), copy_html)


class CalendarSeriesChipTests(_SeriesMixin, TestCase):
    def setUp(self):
        self._workspace()
        self.user = User.objects.create_user(email="cal@example.com", password="pw", tos_accepted_at=timezone.now())
        OrgMembership.objects.create(user=self.user, organization=self.org, org_role=OrgMembership.OrgRole.OWNER)
        WorkspaceMembership.objects.create(
            user=self.user, workspace=self.ws, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
        )
        self.client.force_login(self.user)
        # The first day of next month, so the whole short series is on one grid.
        today = timezone.localtime(timezone.now(), NEW_YORK)
        first_next = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
        self.start = first_next.replace(hour=9, minute=0, second=0, microsecond=0)
        self.url = reverse("calendar:calendar", kwargs={"workspace_id": self.ws.id})

    def _month(self):
        return self.client.get(
            self.url, {"view": "month", "date": self.start.date().isoformat()}, HTTP_HX_REQUEST="true"
        )

    def test_every_post_in_the_series_is_marked(self):
        source = self._source(self.start)
        save_recurrence(source, frequency="daily", count=3)

        response = self._month()

        self.assertEqual(response.status_code, 200)
        # Each of the 3 chips carries the summary twice: tooltip and screen-reader text.
        self.assertEqual(response.content.decode().count("Repeats: Every day, 3 times"), 6)

    def test_a_stopped_series_is_not_marked(self):
        source = self._source(self.start)
        save_recurrence(source, frequency="daily", count=3)
        stop_recurrence(source)

        self.assertNotIn("Repeats:", self._month().content.decode())

    def test_chips_do_not_query_per_post(self):
        source = self._source(self.start)
        save_recurrence(source, frequency="daily", count=2)
        self._month()  # warm the per-session lookups the first request makes
        few = _query_count(self._month)

        save_recurrence(source, frequency="daily", count=8)
        many = _query_count(self._month)

        self.assertEqual(Post.objects.filter(recurrence_source=source).count(), 7)
        self.assertEqual(few, many)


def _query_count(fn):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    with CaptureQueriesContext(connection) as ctx:
        fn()
    return len(ctx.captured_queries)


class ApprovalSeriesTests(_SeriesMixin, TestCase):
    """A series sent for approval keeps its rule and starts once approved."""

    _form = ComposerRepeatTests._form
    _post = ComposerRepeatTests._post

    def setUp(self):
        ComposerRepeatTests.setUp(self)
        self.ws.approval_workflow_mode = "required_internal"
        self.ws.save(update_fields=["approval_workflow_mode"])

    def test_rule_waits_for_approval_and_copies_inherit_it(self):
        from apps.approvals import services as approval_services
        from apps.approvals.policy import approval_allows_publish
        from apps.composer.services import transition_platform_post

        response = self._post(
            action="submit_for_approval", recurrence_frequency="daily", recurrence_end="after", recurrence_count="3"
        )

        self.assertEqual(response.status_code, 204, response.content)
        source = Post.objects.get(caption="repeat me")
        self.assertTrue(source.recurrence_rule.is_active)
        self.assertIsNotNone(source.proposed_publish_at)
        self.assertEqual(self._copies(source), [])
        # The hourly sweep leaves a series in review alone.
        generate_recurring_posts()
        self.assertEqual(self._copies(source), [])

        pp = source.platform_posts.get()
        approval_services.approve_post(pp, self.user, self.ws)
        pp.refresh_from_db()
        transition_platform_post(pp, "scheduled", scheduled_at=source.proposed_publish_at)
        source.refresh_from_db()
        generate_occurrences(source.recurrence_rule)

        copies = self._copies(source)
        self.assertEqual(len(copies), 2)
        for copy in copies:
            copy_pp = copy.platform_posts.get()
            self.assertEqual(copy_pp.status, "scheduled")
            self.assertTrue(approval_allows_publish(copy_pp))

    def test_approval_submit_without_a_time_is_refused(self):
        response = self._post(
            action="submit_for_approval", scheduled_date="", scheduled_time="", recurrence_frequency="daily"
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Pick a date", json.loads(response.content)["errors"]["recurrence"])
        self.assertFalse(Post.objects.exists())

    def test_compose_page_says_repeat_starts_once_approved(self):
        html = self.client.get(reverse("composer:compose", kwargs={"workspace_id": self.ws.id})).content.decode()
        self.assertIn("Repeat starts once approved", html)


class MoveSeriesTests(_SeriesMixin, TestCase):
    """Dragging a series' first post on the calendar moves its copies too."""

    def setUp(self):
        self._workspace()
        self.user = User.objects.create_user(email="drag@example.com", password="pw", tos_accepted_at=timezone.now())
        OrgMembership.objects.create(user=self.user, organization=self.org, org_role=OrgMembership.OrgRole.OWNER)
        WorkspaceMembership.objects.create(
            user=self.user, workspace=self.ws, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
        )
        self.client.force_login(self.user)
        day = timezone.localtime(timezone.now(), NEW_YORK).date() + timedelta(days=2)
        self.start = datetime.combine(day, time(9, 0), tzinfo=NEW_YORK)
        self.source = self._source(self.start)
        save_recurrence(self.source, frequency="daily", count=3)

    def test_copies_follow_the_first_post(self):
        new_start = self.start + timedelta(days=1, hours=1)
        response = self.client.post(
            reverse("calendar:reschedule", kwargs={"workspace_id": self.ws.id}),
            {
                "platform_post_id": str(self.source.platform_posts.get().id),
                "new_datetime": new_start.replace(tzinfo=None).isoformat(),
            },
        )

        self.assertEqual(response.status_code, 204, response.content)
        copies = self._copies(self.source)
        self.assertEqual(
            [c.scheduled_at.astimezone(NEW_YORK) for c in copies],
            [new_start + timedelta(days=1), new_start + timedelta(days=2)],
        )

    def test_moving_a_copy_leaves_the_series_alone(self):
        first_copy = self._copies(self.source)[0]
        before = [c.id for c in self._copies(self.source)]
        response = self.client.post(
            reverse("calendar:reschedule", kwargs={"workspace_id": self.ws.id}),
            {
                "platform_post_id": str(first_copy.platform_posts.get().id),
                "new_datetime": (self.start + timedelta(days=1, hours=3)).replace(tzinfo=None).isoformat(),
            },
        )

        self.assertEqual(response.status_code, 204, response.content)
        self.assertEqual([c.id for c in self._copies(self.source)], before)
