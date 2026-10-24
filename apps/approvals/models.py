"""Approval workflow models (F-2.2).

Models:
    ApprovalAction - Audit trail of approval decisions on a post.
    PostComment - Threaded comments with internal/external visibility.
    ApprovalReminder - Tracks reminder state and the approval deadline per post per stage.
    ApprovalLink - Signed, single-use, expiring one-tap link from an approval email.
    SlackApprovalConnection - A workspace's Slack channel for approval requests.
"""

import uuid

from django.conf import settings
from django.db import models

from apps.common.encryption import EncryptedTextField


class Channel(models.TextChoices):
    """The surface an approval decision was made on."""

    WEB = "web", "Web app"
    EMAIL = "email", "Email link"
    SLACK = "slack", "Slack"
    PORTAL = "portal", "Client portal"
    API = "api", "API"


class ApprovalAction(models.Model):
    """Audit trail entry for an approval workflow action on a post."""

    class ActionType(models.TextChoices):
        SUBMITTED = "submitted", "Submitted"
        APPROVED = "approved", "Approved"
        CHANGES_REQUESTED = "changes_requested", "Changes Requested"
        REJECTED = "rejected", "Rejected"
        RESUBMITTED = "resubmitted", "Resubmitted"
        HELD = "held", "Hold Requested"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    post = models.ForeignKey(
        "composer.Post",
        on_delete=models.CASCADE,
        related_name="approval_actions",
    )
    # Optional pointer to the specific PlatformPost the action targeted. Null
    # means the action was bundled at the Post level (all platforms at once).
    platform_post = models.ForeignKey(
        "composer.PlatformPost",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="approval_actions",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approval_actions",
    )
    action = models.CharField(max_length=20, choices=ActionType.choices)
    comment = models.TextField(blank=True, default="")
    # Where the decision was made, and the content it was made on: the
    # fingerprint is the hash of exactly what the decider could see (see
    # ``policy.review_fingerprint``), so the trail proves what was approved.
    channel = models.CharField(max_length=10, choices=Channel.choices, default=Channel.WEB)
    fingerprint = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "approvals_approval_action"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["post", "-created_at"], name="idx_approval_post_date"),
        ]

    def __str__(self):
        return f"{self.get_action_display()} by {self.user} on {self.post_id}"


class PostComment(models.Model):
    """Threaded comment on a post with internal/external visibility."""

    class Visibility(models.TextChoices):
        INTERNAL = "internal", "Internal"
        EXTERNAL = "external", "External"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    post = models.ForeignKey(
        "composer.Post",
        on_delete=models.CASCADE,
        related_name="comments",
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="post_comments",
    )
    parent_comment = models.ForeignKey(
        "self",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="replies",
    )
    body = models.TextField()
    visibility = models.CharField(
        max_length=10,
        choices=Visibility.choices,
        default=Visibility.EXTERNAL,
    )
    attachment = models.ImageField(
        upload_to="comment_attachments/%Y/%m/",
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "approvals_post_comment"
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["post", "created_at"], name="idx_comment_post_date"),
            models.Index(fields=["post", "visibility"], name="idx_comment_post_vis"),
        ]

    def __str__(self):
        prefix = "[deleted] " if self.deleted_at else ""
        return f"{prefix}Comment by {self.author} on {self.post_id}"

    @property
    def is_deleted(self):
        return self.deleted_at is not None


class ApprovalReminder(models.Model):
    """Tracks reminder count per post per approval stage."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    post = models.ForeignKey(
        "composer.Post",
        on_delete=models.CASCADE,
        related_name="approval_reminders",
    )
    stage = models.CharField(max_length=20)
    reminder_count = models.PositiveIntegerField(default=0)
    last_reminder_at = models.DateTimeField(null=True, blank=True)
    escalated = models.BooleanField(default=False)
    # "Needs approval by": set when the post enters the stage (from the
    # workspace's SLA unless a caller names a time). Null means no deadline, and
    # the stall rules in ``tasks`` apply instead.
    due_at = models.DateTimeField(null=True, blank=True, db_index=True)
    deadline_reminded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "approvals_approval_reminder"
        unique_together = [("post", "stage")]

    def __str__(self):
        return f"Reminder({self.stage}, count={self.reminder_count}) for {self.post_id}"


def _link_expiry():
    from datetime import timedelta

    from django.utils import timezone

    return timezone.now() + timedelta(hours=settings.APPROVAL_LINK_TTL_HOURS)


class ApprovalLink(models.Model):
    """One-tap decision link sent in an approval email.

    The URL carries only a signed reference to this row. The row is what makes
    the link single-use (``used_at``), expiring (``expires_at``) and bound to
    its recipient (``recipient`` and the address it was mailed to). The two
    buttons of one email share a ``group``: acting on either retires both.
    """

    class Action(models.TextChoices):
        APPROVE = "approve", "Approve"
        REQUEST_CHANGES = "request_changes", "Request changes"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    group = models.UUIDField(db_index=True)
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE, related_name="approval_links")
    post = models.ForeignKey("composer.Post", on_delete=models.CASCADE, related_name="approval_links")
    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="approval_links")
    recipient_email = models.EmailField()
    action = models.CharField(max_length=20, choices=Action.choices)
    # ``policy.review_fingerprint`` of the post when the email was built.
    fingerprint = models.CharField(max_length=64)
    expires_at = models.DateTimeField(default=_link_expiry)
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "approvals_approval_link"
        indexes = [models.Index(fields=["expires_at"], name="idx_approval_link_expiry")]

    def __str__(self):
        return f"ApprovalLink({self.action}) for {self.post_id} to {self.recipient_email}"

    @property
    def is_expired(self):
        from django.utils import timezone

        return timezone.now() >= self.expires_at

    @property
    def is_usable(self):
        return self.used_at is None and not self.is_expired


class SlackApprovalConnection(models.Model):
    """A workspace's Slack channel that receives approval requests.

    Separate from a Slack *publishing* account: it carries its own bot token,
    with the extra scopes needed to read members' verified emails and to open
    the request-changes dialog.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.OneToOneField(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="slack_approval_connection"
    )
    team_id = models.CharField(max_length=32, db_index=True)
    team_name = models.CharField(max_length=255, blank=True, default="")
    bot_token = EncryptedTextField()
    channel_id = models.CharField(max_length=32, blank=True, default="")
    channel_name = models.CharField(max_length=255, blank=True, default="")
    connected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "approvals_slack_connection"

    def __str__(self):
        return f"Slack {self.team_name or self.team_id} for {self.workspace_id}"

    @property
    def is_ready(self):
        return bool(self.channel_id and self.bot_token)
