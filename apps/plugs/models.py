"""Engagement automations ("plugs"): do something once a published post reaches a count.

Models:
    PlugRule - "when this post reaches N likes/reposts/views within T hours, repost it / reply / have another account repost or quote it".
    PlugRun - One rule watching one published post: its counts, and what happened when it fired (once).
"""

import uuid

from django.conf import settings
from django.db import models

from apps.common.managers import WorkspaceScopedManager
from providers import plug_capability_for
from providers.types import PlugAction, PlugTrigger


def reads_per_post(window_hours: int, interval_minutes: int) -> int:
    return 1 + (window_hours * 60) // max(interval_minutes, 1)


class PlugRule(models.Model):
    """A rule on one post (``platform_post``) or the default for every new post of an account (``platform_post`` empty)."""

    TRIGGER_CHOICES = [
        (PlugTrigger.LIKES.value, "Likes"),
        (PlugTrigger.REPOSTS.value, "Reposts"),
        (PlugTrigger.VIEWS.value, "Views"),
    ]
    ACTION_CHOICES = [
        (PlugAction.REPOST.value, "Repost"),
        (PlugAction.REPLY.value, "Reply"),
        (PlugAction.QUOTE.value, "Quote"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE, related_name="plug_rules")
    # The account whose posts are watched.
    social_account = models.ForeignKey(
        "social_accounts.SocialAccount", on_delete=models.CASCADE, related_name="plug_rules"
    )
    platform_post = models.ForeignKey(
        "composer.PlatformPost", on_delete=models.CASCADE, null=True, blank=True, related_name="plug_rules"
    )
    trigger = models.CharField(max_length=10, choices=TRIGGER_CHOICES)
    threshold = models.PositiveIntegerField()
    window_hours = models.PositiveSmallIntegerField(
        help_text="Hours after publishing during which the count is watched."
    )
    action = models.CharField(max_length=10, choices=ACTION_CHOICES)
    # The account that acts when it is not the one that published the post (a cross-account reposter).
    actor_account = models.ForeignKey(
        "social_accounts.SocialAccount", on_delete=models.CASCADE, null=True, blank=True, related_name="plug_roles"
    )
    message = models.TextField(blank=True, default="")
    # Minutes between two reads of the post's counts; empty means the network's shortest gap
    # (``PlugCapability.check_interval_minutes``). Longer gaps cost less where reads are billed.
    check_interval_minutes = models.PositiveSmallIntegerField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = WorkspaceScopedManager()

    class Meta:
        db_table = "plugs_rule"
        ordering = ["created_at", "id"]
        indexes = [models.Index(fields=["social_account", "is_active"], name="idx_plugrule_account_active")]

    def __str__(self):
        return f"PlugRule({self.trigger}>={self.threshold} -> {self.action})"

    @property
    def is_default(self) -> bool:
        return self.platform_post_id is None

    @property
    def acting_account(self):
        return self.actor_account or self.social_account

    @property
    def is_cross_account(self) -> bool:
        return self.actor_account_id is not None and self.actor_account_id != self.social_account_id

    @property
    def interval_minutes(self) -> int:
        """Minutes between two reads of a watched post: the rule's own gap, never shorter than the network allows."""
        floor = plug_capability_for(self.social_account.platform).check_interval_minutes
        return max(self.check_interval_minutes or floor, floor)

    @property
    def reads_per_post(self) -> int:
        """The most reads one watched post can cost over the whole window (one at the start, then one per gap)."""
        return reads_per_post(self.window_hours, self.interval_minutes)

    @property
    def read_cost_per_post(self) -> float | None:
        """The most the reads of one watched post can cost the customer, in US dollars, where the network bills them."""
        cost = plug_capability_for(self.social_account.platform).read_cost_usd
        return None if cost is None else round(cost * self.reads_per_post, 4)

    @property
    def summary(self) -> str:
        what = {"repost": "repost it", "reply": "reply", "quote": "quote it"}[self.action]
        who = f" from {self.actor_account.account_name}" if self.is_cross_account else ""
        return (
            f"At {self.threshold:,} {self.get_trigger_display().lower()} within {self.window_hours} "
            f"hour{'s' if self.window_hours != 1 else ''}: {what}{who}"
        )


class PlugRun(models.Model):
    """One rule watching one published post. Fires at most once (the unique key), and records how it went."""

    class Status(models.TextChoices):
        WATCHING = "watching", "Watching"
        FIRING = "firing", "Acting"
        QUEUED = "queued", "Waiting out the undo window"
        RETRY = "retry", "Retrying"
        AWAITING_APPROVAL = "awaiting_approval", "Waiting for approval"
        DONE = "done", "Done"
        FAILED = "failed", "Failed"
        BLOCKED = "blocked", "Blocked by the autonomy policy"
        EXPIRED = "expired", "Window ended"
        CANCELLED = "cancelled", "Cancelled"
        UNCONFIRMED = "unconfirmed", "Outcome unknown"

    #: Runs that still have something to do.
    OPEN_STATUSES = (Status.WATCHING, Status.FIRING, Status.QUEUED, Status.RETRY, Status.AWAITING_APPROVAL)

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    rule = models.ForeignKey(PlugRule, on_delete=models.CASCADE, related_name="runs")
    platform_post = models.ForeignKey("composer.PlatformPost", on_delete=models.CASCADE, related_name="plug_runs")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.WATCHING, db_index=True)
    next_check_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    check_failures = models.PositiveSmallIntegerField(default=0)
    attempts = models.PositiveSmallIntegerField(default=0)
    last_counts = models.JSONField(default=dict, blank=True)
    reached_value = models.PositiveIntegerField(null=True, blank=True)
    fired_at = models.DateTimeField(null=True, blank=True)
    result_post_id = models.CharField(max_length=500, blank=True, default="")
    detail = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "plugs_run"
        ordering = ["-created_at", "-id"]
        constraints = [models.UniqueConstraint(fields=["rule", "platform_post"], name="uniq_plugrun_rule_post")]
        indexes = [models.Index(fields=["status", "next_check_at"], name="idx_plugrun_status_next")]

    def __str__(self):
        return f"PlugRun({self.rule_id} on {self.platform_post_id}: {self.status})"
