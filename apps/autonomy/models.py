"""Autonomy policy and the agent audit trail.

Models:
    AutonomyPolicy - One workspace's dial per action type, guardrails and crisis switch.
    AccountAutonomyOverride - Per social account overrides of that dial.
    AgentAction - What a person, agent or automated system did (or was stopped from doing), and why.
"""

import uuid

from django.conf import settings
from django.db import models


class Level(models.TextChoices):
    OFF = "off", "Off"
    DRAFT_ONLY = "draft_only", "Draft only"
    AUTO_GUARDRAILS = "auto_guardrails", "Auto with guardrails"
    FULL_AUTO = "full_auto", "Full auto"


class RecycleLevel(models.TextChoices):
    """Recycling keeps its own per-item approval rules unless the dial says otherwise."""

    PER_ITEM = "per_item", "Each item's own setting"
    OFF = Level.OFF.value, Level.OFF.label
    DRAFT_ONLY = Level.DRAFT_ONLY.value, Level.DRAFT_ONLY.label
    AUTO_GUARDRAILS = Level.AUTO_GUARDRAILS.value, Level.AUTO_GUARDRAILS.label
    FULL_AUTO = Level.FULL_AUTO.value, Level.FULL_AUTO.label


class ActionType(models.TextChoices):
    DRAFT_CONTENT = "draft_content", "Draft content"
    PUBLISH_POSTS = "publish_posts", "Schedule and publish posts"
    REPLY = "reply", "Reply to comments and DMs"
    RECYCLE_EVERGREEN = "recycle_evergreen", "Recycle evergreen posts"
    # Recorded for the audit trail only; the dial has no setting for it.
    APPROVAL = "approval", "Review and oversight"


# The action types the dial controls, in the order the settings page shows them.
DIAL_ACTIONS = (
    ActionType.DRAFT_CONTENT,
    ActionType.PUBLISH_POSTS,
    ActionType.REPLY,
    ActionType.RECYCLE_EVERGREEN,
)


class ActorType(models.TextChoices):
    HUMAN = "human", "Person"
    AGENT = "agent", "Agent"
    SYSTEM = "system", "Automation"


class Decision(models.TextChoices):
    EXECUTED = "executed", "Executed"
    HELD = "held", "Held for review"
    BLOCKED = "blocked", "Blocked"


class AutonomyPolicy(models.Model):
    """The workspace's autonomy dial, guardrails, undo window and crisis switch."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.OneToOneField(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="autonomy_policy",
    )

    draft_level = models.CharField(max_length=20, choices=Level.choices, default=Level.DRAFT_ONLY)
    publish_level = models.CharField(max_length=20, choices=Level.choices, default=Level.DRAFT_ONLY)
    reply_level = models.CharField(max_length=20, choices=Level.choices, default=Level.DRAFT_ONLY)
    recycle_level = models.CharField(max_length=20, choices=RecycleLevel.choices, default=RecycleLevel.PER_ITEM)

    # Guardrails, checked at "Auto with guardrails" (the blocked list also at "Full auto").
    blocked_phrases = models.JSONField(default=list, blank=True)
    review_links = models.BooleanField(default=True)
    review_mentions = models.BooleanField(default=True)
    review_prices = models.BooleanField(default=True)
    review_sensitive = models.BooleanField(default=True)
    max_posts_per_day = models.PositiveSmallIntegerField(default=3)
    max_replies_per_day = models.PositiveSmallIntegerField(default=20)
    quiet_hours_enabled = models.BooleanField(default=False)
    quiet_start = models.TimeField(null=True, blank=True)
    quiet_end = models.TimeField(null=True, blank=True)
    ai_risk_check = models.BooleanField(default=False)
    ai_risk_threshold = models.FloatField(default=0.6)
    ai_voice_check = models.BooleanField(default=False)
    ai_voice_threshold = models.FloatField(default=0.5)

    # How long an autonomous publish or reply waits where a person can undo it.
    undo_delay_minutes = models.PositiveSmallIntegerField(default=5)

    crisis_paused = models.BooleanField(default=False)
    crisis_paused_at = models.DateTimeField(null=True, blank=True)
    crisis_paused_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    # Whether clients see the executed automated activity in their portal.
    client_visible = models.BooleanField(default=False)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "autonomy_policy"

    def __str__(self):
        return f"AutonomyPolicy({self.workspace_id})"


class AccountAutonomyOverride(models.Model):
    """Per account overrides of the workspace dial. Blank means "use the workspace's"."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="autonomy_overrides",
    )
    social_account = models.OneToOneField(
        "social_accounts.SocialAccount",
        on_delete=models.CASCADE,
        related_name="autonomy_override",
    )
    draft_level = models.CharField(max_length=20, choices=Level.choices, blank=True, default="")
    publish_level = models.CharField(max_length=20, choices=Level.choices, blank=True, default="")
    reply_level = models.CharField(max_length=20, choices=Level.choices, blank=True, default="")
    recycle_level = models.CharField(max_length=20, choices=RecycleLevel.choices, blank=True, default="")
    max_posts_per_day = models.PositiveSmallIntegerField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "autonomy_account_override"

    def __str__(self):
        return f"AccountAutonomyOverride({self.social_account_id})"


class AgentAction(models.Model):
    """One entry of the audit trail: who acted, what the policy decided, and why."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="agent_actions",
    )
    social_account = models.ForeignKey(
        "social_accounts.SocialAccount",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="agent_actions",
    )
    # Kept beside the FK so the entry still reads right after the account is removed.
    account_label = models.CharField(max_length=255, blank=True, default="")

    actor_type = models.CharField(max_length=10, choices=ActorType.choices, db_index=True)
    actor_name = models.CharField(max_length=255)
    actor_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    api_key = models.ForeignKey(
        "api_keys.ApiKey",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="agent_actions",
    )

    action_type = models.CharField(max_length=20, choices=ActionType.choices)
    # The specific verb, e.g. "post.schedule" or "reply.send".
    operation = models.CharField(max_length=64)
    decision = models.CharField(max_length=10, choices=Decision.choices, db_index=True)
    # The dial setting the decision was made under; blank for people.
    level = models.CharField(max_length=20, blank=True, default="")
    summary = models.TextField(blank=True, default="")
    guardrail_results = models.JSONField(default=list, blank=True)
    ai_used = models.BooleanField(default=False)
    risk_score = models.FloatField(null=True, blank=True)

    # The text the actor started from (approved/original) and the text acted on.
    source_text = models.TextField(blank=True, default="")
    final_text = models.TextField(blank=True, default="")

    post = models.ForeignKey(
        "composer.Post",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="agent_actions",
    )
    platform_post = models.ForeignKey(
        "composer.PlatformPost",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="agent_actions",
    )
    reply = models.ForeignKey(
        "inbox.InboxReply",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="agent_actions",
    )
    # An engagement automation (repost, follow-up reply) held for a person or waiting out its undo window.
    plug_run = models.ForeignKey(
        "plugs.PlugRun",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="agent_actions",
    )
    # Identifies the thing acted on so a repeated refusal is logged once a day.
    target_key = models.CharField(max_length=64, blank=True, default="", db_index=True)

    # When the content was asked to go out, and the earliest it may (the undo window).
    requested_at = models.DateTimeField(null=True, blank=True)
    release_at = models.DateTimeField(null=True, blank=True)
    undone_at = models.DateTimeField(null=True, blank=True)
    undone_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    held_at = models.DateTimeField(null=True, blank=True)
    hold_released_at = models.DateTimeField(null=True, blank=True)

    review_outcome = models.CharField(max_length=10, blank=True, default="")
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    # Where the review happened: "web" (Activity), "email" (a one-tap link) or "slack".
    review_channel = models.CharField(max_length=10, blank=True, default="")

    platform_deleted_at = models.DateTimeField(null=True, blank=True)
    platform_delete_result = models.CharField(max_length=255, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "autonomy_agent_action"
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["workspace", "-created_at"], name="idx_agentaction_ws_time"),
            models.Index(fields=["workspace", "decision"], name="idx_agentaction_ws_decision"),
        ]

    def __str__(self):
        return f"{self.actor_name}: {self.operation} -> {self.decision}"

    @property
    def is_pending_release(self) -> bool:
        """Executed by an automation but not yet sent, so it can still be undone or held."""
        if self.undone_at is not None or self.release_at is None or self.decision != Decision.EXECUTED:
            return False
        if self.plug_run_id:
            return self.plug_run.status == "queued"
        if self.platform_post_id:
            return self.platform_post.status == "scheduled"
        if self.reply_id:
            return self.reply.status == "draft"
        return False
