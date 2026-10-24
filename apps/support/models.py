"""Staff support tools: the impersonation audit trail and in-app announcements."""

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


class ImpersonationSession(models.Model):
    """One staff "view as" session, kept as the audit record of who looked at whose account.

    The staff and target emails are copied onto the row so the trail survives
    either account being deleted. A row with no ``ended_at`` is live, or was
    cut short by the session expiring; ``expires_at`` says which.
    """

    class EndReason(models.TextChoices):
        STOPPED = "stopped", "Stopped by staff"
        LOGOUT = "logout", "Staff signed out"
        EXPIRED = "expired", "Timed out"
        INVALID = "invalid", "No longer allowed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    staff_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="impersonation_sessions_started",
    )
    staff_email = models.EmailField()
    target_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="impersonation_sessions_received",
    )
    target_email = models.EmailField()
    target_organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="support_sessions",
    )
    reason = models.CharField(max_length=200, help_text="Why support needed to look, e.g. a ticket reference.")
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    started_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)
    end_reason = models.CharField(max_length=20, choices=EndReason.choices, blank=True, default="")

    class Meta:
        db_table = "support_impersonation_session"
        ordering = ["-started_at"]
        indexes = [
            models.Index(fields=["target_organization", "-started_at"], name="idx_impers_org_started"),
        ]

    def __str__(self):
        return f"{self.staff_email} as {self.target_email} ({self.started_at:%Y-%m-%d %H:%M})"


class Announcement(models.Model):
    """A banner staff post for signed-in users, shown between ``starts_at`` and ``ends_at``."""

    class Severity(models.TextChoices):
        INFO = "info", "Info"
        WARNING = "warning", "Warning"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    message = models.CharField(max_length=300)
    severity = models.CharField(max_length=10, choices=Severity.choices, default=Severity.INFO)
    link_url = models.URLField(max_length=500, blank=True, default="")
    link_label = models.CharField(max_length=60, blank=True, default="", help_text="Defaults to 'Learn more'.")
    starts_at = models.DateTimeField(default=timezone.now)
    ends_at = models.DateTimeField(null=True, blank=True, help_text="Leave empty to keep it up until switched off.")
    plan_slugs = models.JSONField(
        default=list,
        blank=True,
        help_text="Only organizations on these plans see it. Leave empty for everyone.",
    )
    is_active = models.BooleanField(default=True, help_text="Switch off to withdraw it early.")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "support_announcement"
        ordering = ["-starts_at"]

    def __str__(self):
        return self.message[:60]


class AnnouncementDismissal(models.Model):
    """A user closed an announcement; it stays closed for them on every device."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    announcement = models.ForeignKey(Announcement, on_delete=models.CASCADE, related_name="dismissals")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="announcement_dismissals",
    )
    dismissed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "support_announcement_dismissal"
        constraints = [
            models.UniqueConstraint(fields=["announcement", "user"], name="uniq_announcement_dismissal"),
        ]
