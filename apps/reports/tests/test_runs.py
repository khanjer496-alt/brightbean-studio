"""The run lifecycle: scheduling once per slot, retries, email, notification, share links."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from unittest.mock import patch

import pytest
from background_task.models import Task
from django.core import mail, signing
from django.utils import timezone

from apps.billing.entitlements import PlanLimitError
from apps.billing.tests.support import set_plan
from apps.notifications.models import EventType, Notification
from apps.reports import services
from apps.reports.models import ReportRun
from apps.reports.tasks import run_due_reports

pytestmark = pytest.mark.django_db

MONDAY_10 = datetime(2026, 10, 19, 10, tzinfo=UTC)


@pytest.fixture
def scheduled(definition):
    definition.frequency = "weekly"
    definition.weekday = 0
    definition.send_time = time(9)
    definition.schedule_anchor = datetime(2026, 10, 1, tzinfo=UTC)
    definition.save()
    return definition


class TestScheduling:
    def test_creates_one_run_per_slot(self, scheduled):
        assert services.schedule_due_runs(MONDAY_10) == 1
        assert services.schedule_due_runs(MONDAY_10 + timedelta(hours=3)) == 0
        assert services.schedule_due_runs(MONDAY_10 + timedelta(days=2)) == 0
        run = ReportRun.objects.get()
        assert (run.schedule_key, run.trigger, run.deliver) == ("W2026-43", "scheduled", True)
        assert services.schedule_due_runs(MONDAY_10 + timedelta(days=7)) == 1

    def test_period_is_the_slots_not_the_sweeps(self, scheduled):
        scheduled.range_type = "last_7"
        scheduled.save()
        services.schedule_due_runs(MONDAY_10 + timedelta(days=3))  # late sweep, Thursday
        run = ReportRun.objects.get()
        assert (run.period_start.isoformat(), run.period_end.isoformat()) == ("2026-10-12", "2026-10-18")

    def test_paused_or_unscheduled_definitions_are_skipped(self, scheduled):
        scheduled.is_active = False
        scheduled.save()
        assert services.schedule_due_runs(MONDAY_10) == 0

    def test_lost_plan_skips_scheduling(self, scheduled, settings, workspace):
        settings.BILLING_ENABLED = True
        set_plan(workspace.organization, "team")
        assert services.schedule_due_runs(MONDAY_10) == 0

    def test_duplicate_slot_is_refused_by_the_database(self, scheduled):
        assert services.create_run(scheduled, trigger="scheduled", schedule_key="W1") is not None
        assert services.create_run(scheduled, trigger="scheduled", schedule_key="W1") is None

    def test_sweep_task_builds_and_emails(self, scheduled, seeded):
        with patch("apps.reports.services.timezone.now", return_value=MONDAY_10):
            services.schedule_due_runs(MONDAY_10)
        run_due_reports.task_function()
        run = ReportRun.objects.get()
        assert run.status == "succeeded" and run.pdf_file
        assert [m.to for m in mail.outbox if m.attachments] == [["boss@client.test"]]


class TestExecution:
    def make_run(self, definition, **kw):
        return services.create_run(definition, trigger="manual", **kw)

    def test_builds_pdf_and_marks_ready(self, definition, seeded):
        run = services.execute_run(self.make_run(definition).id)
        run.refresh_from_db()
        assert run.status == "succeeded" and run.attempts == 1 and run.data["title"] == "Monthly report"
        with run.pdf_file.open("rb") as f:
            assert f.read(5) == b"%PDF-"
        assert [m for m in mail.outbox if m.to == ["boss@client.test"]] == []  # manual runs only email when asked

    def test_emails_pdf_and_link_when_delivering(self, definition, seeded):
        run = services.execute_run(self.make_run(definition, deliver=True).id)
        assert run.emailed_to == ["boss@client.test"]
        msg = next(m for m in mail.outbox if m.to == ["boss@client.test"])
        assert msg.to == ["boss@client.test"] and msg.subject.startswith("Monthly report")
        assert msg.attachments[0][0].endswith(".pdf") and msg.attachments[0][2] == "application/pdf"
        assert msg.attachments[0][1].startswith(b"%PDF-")
        assert "/reports/share/" in msg.body and "/reports/share/" in msg.alternatives[0][0]

    def test_white_label_email_has_no_product_name(self, definition, seeded, settings):
        settings.BRAND_NAME = "PostDelegate"
        definition.white_label = True
        definition.save()
        services.execute_run(self.make_run(definition, deliver=True).id)
        assert "PostDelegate" not in next(m for m in mail.outbox if m.to == ["boss@client.test"]).body

    def test_announces_once_to_report_managers_only(self, definition, seeded, owner, viewer, client_user):
        run = services.execute_run(self.make_run(definition).id)
        notes = Notification.objects.filter(event_type=EventType.REPORT_GENERATED)
        assert [n.user_id for n in notes] == [owner.id]
        assert str(run.id) in notes[0].data["run_id"]
        services._announce(run)
        assert notes.count() == 1

    def test_claimed_run_is_not_built_twice(self, definition, seeded):
        run = self.make_run(definition)
        assert services.execute_run(run.id) is not None
        assert services.execute_run(run.id) is None

    def test_failure_retries_then_gives_up(self, definition, seeded):
        run = self.make_run(definition)
        with patch("apps.reports.services.build_report_data", side_effect=RuntimeError("boom")):
            services.execute_run(run.id)
            run.refresh_from_db()
            assert run.status == "pending" and run.attempts == 1 and "boom" in run.error
            assert run.next_attempt_at > timezone.now()
            # Not due yet: the sweep leaves it alone.
            assert services.due_run_ids(10) == []
            for _ in range(2):
                ReportRun.objects.filter(pk=run.pk).update(next_attempt_at=timezone.now() - timedelta(seconds=1))
                services.execute_run(run.id)
        run.refresh_from_db()
        assert run.status == "failed" and run.attempts == services.MAX_ATTEMPTS
        assert Notification.objects.filter(event_type=EventType.REPORT_GENERATED).count() == 0

    def test_retry_after_partial_email_only_sends_the_rest(self, definition, seeded):
        definition.recipients = ["a@client.test", "b@client.test"]
        definition.save()
        run = self.make_run(definition, deliver=True)
        real = services.send_or_raise
        calls = []

        def flaky(msg):
            calls.append(msg.to[0])
            if msg.to == ["b@client.test"] and calls.count("b@client.test") == 1:
                raise RuntimeError("smtp down")
            real(msg)

        with patch("apps.reports.services.send_or_raise", side_effect=flaky):
            services.execute_run(run.id)
            run.refresh_from_db()
            assert run.status == "pending" and run.emailed_to == ["a@client.test"] and run.email_error
            ReportRun.objects.filter(pk=run.pk).update(next_attempt_at=timezone.now() - timedelta(seconds=1))
            services.execute_run(run.id)
        run.refresh_from_db()
        assert run.status == "succeeded" and run.emailed_to == ["a@client.test", "b@client.test"]
        assert [m.to[0] for m in mail.outbox if m.to[0].endswith("client.test")] == ["a@client.test", "b@client.test"]
        assert run.email_error == ""

    def test_plan_without_reports_fails_permanently(self, definition, seeded, settings, workspace):
        settings.BILLING_ENABLED = True
        set_plan(workspace.organization, "team")
        run = self.make_run(definition)
        services.execute_run(run.id)
        run.refresh_from_db()
        assert run.status == "failed" and "Branded reports" in run.error and run.attempts == 1

    def test_stale_running_runs_are_requeued(self, definition):
        run = self.make_run(definition)
        ReportRun.objects.filter(pk=run.pk).update(status="running", started_at=timezone.now() - timedelta(hours=1))
        assert services.reap_stale_runs() == 1
        run.refresh_from_db()
        assert run.status == "pending"

    def test_run_now_queues_a_task(self, definition, owner):
        run = services.queue_manual_run(definition, owner)
        assert run.status == "pending" and run.requested_by == owner
        assert Task.objects.filter(task_name="apps.reports.tasks.execute_report_run").exists()

    def test_run_now_refused_without_plan(self, definition, owner, settings, workspace):
        settings.BILLING_ENABLED = True
        set_plan(workspace.organization, "team")
        with pytest.raises(PlanLimitError):
            services.queue_manual_run(definition, owner)


class TestShareLinks:
    @pytest.fixture
    def ready(self, definition, seeded):
        return services.execute_run(services.create_run(definition, trigger="manual").id)

    def test_round_trip(self, ready):
        assert services.resolve_token(services.make_token(ready)) == ready

    def test_expires(self, ready, settings):
        settings.REPORT_LINK_MAX_AGE_DAYS = 7
        token = services.make_token(ready)
        later = timezone.now().timestamp() + 8 * 86400
        with patch("django.core.signing.time.time", return_value=later):
            assert services.resolve_token(token) is None
        with patch("django.core.signing.time.time", return_value=timezone.now().timestamp() + 6 * 86400):
            assert services.resolve_token(token) == ready

    def test_forged_or_foreign_tokens_fail(self, ready):
        assert services.resolve_token("garbage") is None
        assert services.resolve_token(signing.dumps({"run": str(ready.id)}, salt="other")) is None

    def test_unfinished_run_has_no_link(self, definition):
        run = services.create_run(definition, trigger="manual")
        assert services.resolve_token(services.make_token(run)) is None

    def test_public_page_and_pdf_need_no_login(self, client, ready):
        token = services.make_token(ready)
        page = client.get(f"/reports/share/{token}/")
        assert page.status_code == 200 and b"Monthly report" in page.content
        assert "noindex" in page["X-Robots-Tag"] and "no-store" in page["Cache-Control"]
        pdf = client.get(f"/reports/share/{token}/pdf/")
        assert pdf.status_code == 200 and pdf["Content-Type"] == "application/pdf"
        assert b"".join(pdf.streaming_content).startswith(b"%PDF-")

    def test_expired_link_page_is_410(self, client, ready, settings):
        token = services.make_token(ready)
        with patch("django.core.signing.time.time", return_value=timezone.now().timestamp() + 40 * 86400):
            assert client.get(f"/reports/share/{token}/").status_code == 410
            assert client.get(f"/reports/share/{token}/pdf/").status_code == 410

    def test_link_stops_when_plan_lapses(self, client, ready, settings, workspace):
        token = services.make_token(ready)
        settings.BILLING_ENABLED = True
        set_plan(workspace.organization, "team")
        assert client.get(f"/reports/share/{token}/").status_code == 410
