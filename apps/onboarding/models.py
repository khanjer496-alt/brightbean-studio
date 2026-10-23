import secrets
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.common.managers import WorkspaceScopedManager


def _generate_connection_token():
    return secrets.token_urlsafe(32)


class ConnectionLink(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="connection_links",
    )
    token = models.CharField(
        max_length=255,
        unique=True,
        db_index=True,
        default=_generate_connection_token,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="created_connection_links",
    )
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = WorkspaceScopedManager()

    class Meta:
        db_table = "onboarding_connection_link"

    def __str__(self):
        return f"ConnectionLink for {self.workspace.name} (expires {self.expires_at})"

    @property
    def is_expired(self):
        return timezone.now() > self.expires_at

    @property
    def is_revoked(self):
        return self.revoked_at is not None

    @property
    def is_active(self):
        return not self.is_expired and not self.is_revoked


class ConnectionLinkUsage(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    connection_link = models.ForeignKey(
        ConnectionLink,
        on_delete=models.CASCADE,
        related_name="usages",
    )
    social_account = models.ForeignKey(
        "social_accounts.SocialAccount",
        on_delete=models.CASCADE,
        related_name="connection_link_usages",
    )
    connected_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "onboarding_connection_link_usage"
        unique_together = [("connection_link", "social_account")]

    def __str__(self):
        return f"{self.social_account} via {self.connection_link}"


class OnboardingChecklist(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="onboarding_checklists",
    )
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="onboarding_checklists",
    )
    is_dismissed = models.BooleanField(default=False)
    dismissed_at = models.DateTimeField(null=True, blank=True)
    # Items the user confirmed by hand because no data records them (for
    # example "I reviewed my posting times"). The rest are computed live from
    # the workspace by ``apps.onboarding.checklist``.
    confirmed_items = models.JSONField(default=list, blank=True)

    class Meta:
        db_table = "onboarding_checklist"
        unique_together = [("user", "workspace")]

    def __str__(self):
        status = "dismissed" if self.is_dismissed else "active"
        return f"Checklist for {self.user} in {self.workspace} ({status})"


class OnboardingFlow(models.Model):
    """Where a new owner is in the first-run welcome flow for their workspace.

    Created at signup for the account that owns the freshly provisioned
    organization, never for an invited member, so people who already use the
    product (and everyone joining a team) never see it. ``step`` is the step to
    show next, so leaving and coming back resumes in the right place.
    """

    class AccountKind(models.TextChoices):
        CREATOR = "creator", "Creator"
        BUSINESS = "business", "Business"
        AGENCY = "agency", "Agency"

    class Step(models.TextChoices):
        WORKSPACE = "workspace", "Workspace"
        CHANNEL = "channel", "Channel"
        POST = "post", "First post"

    class Status(models.TextChoices):
        ACTIVE = "active", "In progress"
        SKIPPED = "skipped", "Skipped"
        COMPLETED = "completed", "Completed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="onboarding_flows",
    )
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="onboarding_flows",
    )
    account_kind = models.CharField(max_length=20, choices=AccountKind.choices, blank=True, default="")
    step = models.CharField(max_length=20, choices=Step.choices, default=Step.WORKSPACE)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "onboarding_flow"
        unique_together = [("user", "workspace")]

    def __str__(self):
        return f"Welcome flow for {self.user} in {self.workspace} ({self.status}, {self.step})"

    @property
    def is_active(self):
        return self.status == self.Status.ACTIVE

    @property
    def is_agency(self):
        return self.account_kind == self.AccountKind.AGENCY


class DismissedHint(models.Model):
    """A contextual hint a user has closed, so it never comes back."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="dismissed_hints",
    )
    key = models.CharField(max_length=40)
    dismissed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "onboarding_dismissed_hint"
        unique_together = [("user", "key")]

    def __str__(self):
        return f"{self.key} dismissed by {self.user}"


class SampleWorkspace(models.Model):
    """Marks a workspace as the example one, filled with sample drafts.

    The marker is what keeps the example inert: the publisher skips these
    workspaces, scheduling a post in one is refused, and billing does not count
    their placeholder channel or the workspace itself.
    """

    workspace = models.OneToOneField(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="sample_marker",
        primary_key=True,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="sample_workspaces",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "onboarding_sample_workspace"

    def __str__(self):
        return f"Sample workspace {self.workspace_id}"
