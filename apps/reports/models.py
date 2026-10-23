"""Client reports: what to report on (a definition) and each time it was built (a run).

A :class:`ReportRun` stores the finished figures as JSON (``data``) next to the
PDF. Every surface that shows a run — the app, the shareable link, the client
portal, the PDF and the email — renders that one snapshot, so they can never
disagree and a report keeps reading the same after the underlying analytics
move on.
"""

from __future__ import annotations

import datetime as dt
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import models
from django.db.models import Q

from apps.common.validators import validate_hex_color

SECTION_SUMMARY = "summary"
SECTION_TRENDS = "trends"
SECTION_NETWORKS = "networks"
SECTION_TOP_POSTS = "top_posts"
SECTION_ACTIVITY = "activity"
SECTION_INBOX = "inbox"
SECTION_AI_SUMMARY = "ai_summary"

# (key, label) in the order sections appear in a report.
SECTIONS: tuple[tuple[str, str], ...] = (
    (SECTION_AI_SUMMARY, "What changed and what to do next (AI summary)"),
    (SECTION_SUMMARY, "Summary figures with period comparison"),
    (SECTION_TRENDS, "Trend charts"),
    (SECTION_NETWORKS, "Results by network"),
    (SECTION_TOP_POSTS, "Top posts"),
    (SECTION_ACTIVITY, "Posting activity"),
    (SECTION_INBOX, "Inbox response times"),
)
SECTION_KEYS = tuple(key for key, _ in SECTIONS)
DEFAULT_SECTIONS = [SECTION_AI_SUMMARY, SECTION_SUMMARY, SECTION_TRENDS, SECTION_NETWORKS, SECTION_TOP_POSTS]

MAX_RECIPIENTS = 10
MAX_CUSTOM_DAYS = 366


def default_sections() -> list[str]:
    return list(DEFAULT_SECTIONS)


def default_recipients() -> list[str]:
    return []


class ReportDefinition(models.Model):
    class RangeType(models.TextChoices):
        LAST_7 = "last_7", "Last 7 days"
        LAST_30 = "last_30", "Last 30 days"
        LAST_MONTH = "last_month", "Last calendar month"
        CUSTOM = "custom", "Custom dates"

    class Frequency(models.TextChoices):
        NONE = "none", "Not scheduled"
        WEEKLY = "weekly", "Weekly"
        MONTHLY = "monthly", "Monthly"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE, related_name="report_definitions")
    name = models.CharField(max_length=120)
    # Empty means every connected account in the workspace.
    social_accounts = models.ManyToManyField("social_accounts.SocialAccount", blank=True, related_name="+")
    range_type = models.CharField(max_length=12, choices=RangeType.choices, default=RangeType.LAST_30)
    custom_start = models.DateField(null=True, blank=True)
    custom_end = models.DateField(null=True, blank=True)
    sections = models.JSONField(default=default_sections)
    compare = models.BooleanField(default=True)

    # Branding. The logo is the organization's; a blank accent falls back to
    # the workspace colour. White label drops the product's own name.
    accent_color = models.CharField(max_length=7, blank=True, default="", validators=[validate_hex_color])
    white_label = models.BooleanField(default=False)

    recipients = models.JSONField(default=default_recipients, blank=True)
    visible_to_clients = models.BooleanField(
        default=False, help_text="Show finished reports in the client portal for this workspace."
    )

    frequency = models.CharField(max_length=8, choices=Frequency.choices, default=Frequency.NONE)
    weekday = models.PositiveSmallIntegerField(default=0, help_text="0 = Monday … 6 = Sunday (weekly).")
    day_of_month = models.PositiveSmallIntegerField(
        default=1, help_text="1–31 (monthly); a month without that day uses its last day."
    )
    send_time = models.TimeField(default=dt.time(9, 0))
    # Runs are only created for schedule occurrences after this instant, so
    # switching a schedule on mid-week doesn't fire this week's earlier slot.
    schedule_anchor = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    # Part of every shareable link's signed payload: replacing it revokes every link already handed out.
    share_key = models.UUIDField(default=uuid.uuid4, editable=False)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "reports_definition"
        ordering = ["name", "created_at"]
        indexes = [models.Index(fields=["workspace", "is_active"], name="reports_def_ws_active")]

    def __str__(self):
        return self.name

    @property
    def is_scheduled(self) -> bool:
        return self.is_active and self.frequency != self.Frequency.NONE

    def clean(self):
        errors: dict[str, str] = {}
        if not (self.name or "").strip():
            errors["name"] = "Give the report a name."

        sections = self.sections
        if not isinstance(sections, list) or not sections or any(s not in SECTION_KEYS for s in sections):
            errors["sections"] = "Choose at least one section."
        elif len(set(sections)) != len(sections):
            errors["sections"] = "Each section can only be listed once."

        if self.range_type == self.RangeType.CUSTOM:
            if not self.custom_start or not self.custom_end:
                errors["custom_start"] = "Pick both a start and an end date."
            elif self.custom_start > self.custom_end:
                errors["custom_end"] = "The end date can't be before the start date."
            elif (self.custom_end - self.custom_start).days + 1 > MAX_CUSTOM_DAYS:
                errors["custom_end"] = f"A custom range can cover at most {MAX_CUSTOM_DAYS} days."
            if self.frequency != self.Frequency.NONE:
                errors["frequency"] = "A fixed date range can't repeat. Pick a rolling range to schedule it."

        if self.frequency == self.Frequency.WEEKLY and not 0 <= self.weekday <= 6:
            errors["weekday"] = "Pick a day of the week."
        if self.frequency == self.Frequency.MONTHLY and not 1 <= self.day_of_month <= 31:
            errors["day_of_month"] = "Pick a day between 1 and 31."

        recipients = self.recipients
        if not isinstance(recipients, list):
            errors["recipients"] = "Recipients must be a list of email addresses."
        else:
            cleaned = []
            for raw in recipients:
                address = str(raw).strip().lower()
                try:
                    validate_email(address)
                except ValidationError:
                    errors["recipients"] = f"{raw} isn't a valid email address."
                    break
                if address not in cleaned:
                    cleaned.append(address)
            else:
                if len(cleaned) > MAX_RECIPIENTS:
                    errors["recipients"] = f"Add at most {MAX_RECIPIENTS} recipients."
                else:
                    self.recipients = cleaned
            if self.is_scheduled and not errors.get("recipients") and not self.recipients:
                errors["recipients"] = "Add at least one recipient to send this report on a schedule."
        if errors:
            raise ValidationError(errors)


class ReportRun(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Queued"
        RUNNING = "running", "Building"
        SUCCEEDED = "succeeded", "Ready"
        FAILED = "failed", "Failed"

    class Trigger(models.TextChoices):
        SCHEDULED = "scheduled", "Scheduled"
        MANUAL = "manual", "Run now"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    definition = models.ForeignKey(ReportDefinition, on_delete=models.CASCADE, related_name="runs")
    trigger = models.CharField(max_length=10, choices=Trigger.choices, default=Trigger.MANUAL)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True)
    period_start = models.DateField()
    period_end = models.DateField()
    # Names the schedule slot ("W2026-43", "M2026-10"); empty for manual runs.
    # The unique constraint below is what makes a slot run at most once.
    schedule_key = models.CharField(max_length=16, blank=True, default="")
    deliver = models.BooleanField(default=False, help_text="Email the recipients when the report is ready.")

    data = models.JSONField(default=dict, blank=True)
    pdf_file = models.FileField(upload_to="reports/%Y/%m/", blank=True, max_length=255)
    error = models.TextField(blank=True, default="")
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    emailed_to = models.JSONField(default=list, blank=True)
    email_error = models.TextField(blank=True, default="")
    notified_at = models.DateTimeField(null=True, blank=True)

    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "reports_run"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["definition", "schedule_key"],
                condition=~Q(schedule_key=""),
                name="reports_run_one_per_slot",
            )
        ]
        indexes = [models.Index(fields=["status", "next_attempt_at"], name="reports_run_due")]

    def __str__(self):
        return f"{self.definition_id} {self.period_start}..{self.period_end} ({self.status})"

    @property
    def workspace(self):
        return self.definition.workspace

    @property
    def is_ready(self) -> bool:
        return self.status == self.Status.SUCCEEDED
