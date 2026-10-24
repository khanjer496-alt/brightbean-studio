"""What a workspace wants to be found for, and what the AI models answered when asked.

Everything here is a record of model answers at a point in time. None of it is a
search ranking, and the UI says so wherever it shows a figure.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models


class VisibilityProfile(models.Model):
    """One workspace's brand and the category prompts it wants to be found for."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.OneToOneField(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="visibility_profile"
    )
    brand_name = models.CharField(max_length=120)
    website = models.URLField(max_length=300, blank=True, default="")
    # Other names the brand goes by, matched as the brand when reading an answer.
    aliases = models.JSONField(default=list, blank=True)
    # Whether the weekly task runs this profile (a manual run is always possible).
    is_active = models.BooleanField(default=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    # Why the last scheduled run did not happen (no AI configured, allowance spent); "" otherwise.
    skip_reason = models.CharField(max_length=300, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "ai_visibility_profile"

    def __str__(self) -> str:
        return f"VisibilityProfile({self.brand_name})"


class VisibilityPrompt(models.Model):
    """A question the owner wants the brand to be an answer to ("best social media scheduler for agencies in Dubai")."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    profile = models.ForeignKey(VisibilityProfile, on_delete=models.CASCADE, related_name="prompts")
    text = models.CharField(max_length=200)
    position = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "ai_visibility_prompt"
        ordering = ["position", "created_at"]

    def __str__(self) -> str:
        return self.text


class VisibilityRun(models.Model):
    """One report: every prompt asked once, plus the "what do you know about <brand>?" question."""

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        COMPLETE = "complete", "Complete"
        PARTIAL = "partial", "Partly answered"
        FAILED = "failed", "Failed"

    class Trigger(models.TextChoices):
        WEEKLY = "weekly", "Weekly"
        MANUAL = "manual", "Run now"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    profile = models.ForeignKey(VisibilityProfile, on_delete=models.CASCADE, related_name="runs")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.QUEUED)
    trigger = models.CharField(max_length=10, choices=Trigger.choices, default=Trigger.WEEKLY)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    # Copies of the profile when the run started, so an old report still reads correctly after an edit.
    brand_name = models.CharField(max_length=120)
    website = models.URLField(max_length=300, blank=True, default="")
    # Engines asked and the model each reported, e.g. {"anthropic": "claude-opus-5-5"}.
    engines = models.JSONField(default=dict, blank=True)
    prompts_total = models.PositiveIntegerField(default=0)
    prompts_mentioned = models.PositiveIntegerField(default=0)
    # Share of answered category prompts that mention the brand, 0-100; None when nothing was answered.
    mention_rate = models.FloatField(null=True, blank=True)
    # Post ideas that would close the gaps: [{"text": str, "idea_id": str | null}].
    suggested_ideas = models.JSONField(default=list, blank=True)
    error_code = models.CharField(max_length=32, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "ai_visibility_run"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"VisibilityRun({self.brand_name}, {self.status})"

    @property
    def is_done(self) -> bool:
        return self.status in (self.Status.COMPLETE, self.Status.PARTIAL)


class VisibilityAnswer(models.Model):
    """One model answer to one question, and what was read from it."""

    class Kind(models.TextChoices):
        CATEGORY = "category", "Category prompt"
        BRAND = "brand", "About the brand"

    class Sentiment(models.TextChoices):
        POSITIVE = "positive", "Positive"
        NEUTRAL = "neutral", "Neutral"
        MIXED = "mixed", "Mixed"
        NEGATIVE = "negative", "Negative"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(VisibilityRun, on_delete=models.CASCADE, related_name="answers")
    prompt = models.ForeignKey(VisibilityPrompt, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    question = models.CharField(max_length=400)
    engine = models.CharField(max_length=32)
    model = models.CharField(max_length=64, blank=True, default="")
    answer = models.TextField(blank=True, default="")
    answered = models.BooleanField(default=False)
    error_code = models.CharField(max_length=32, blank=True, default="")
    mentioned = models.BooleanField(default=False)
    # 1-based place of the brand among the options the answer names; None when not named or not a list.
    position = models.PositiveSmallIntegerField(null=True, blank=True)
    sentiment = models.CharField(max_length=10, choices=Sentiment.choices, blank=True, default="")
    competitors = models.JSONField(default=list, blank=True)
    cited_urls = models.JSONField(default=list, blank=True)
    cites_brand_site = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "ai_visibility_answer"
        ordering = ["kind", "created_at"]
