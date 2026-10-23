"""Models behind the repurposing pipeline: a job per source, and the outputs it wrote.

A ``RepurposeJob`` records where the text came from (URL, file name, post, video),
what was asked for, and what the run spent. Its ``RepurposeOutput`` rows are
proposals: nothing becomes a draft or an idea until a person accepts it, and
nothing is ever published from here.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models


class RepurposeJob(models.Model):
    class SourceType(models.TextChoices):
        URL = "url", "Web page"
        TEXT = "text", "Pasted text"
        PDF = "pdf", "PDF"
        POST = "post", "Published post"
        VIDEO = "video", "Video"

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Done"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    ACTIVE_STATUSES = (Status.QUEUED, Status.RUNNING)

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE, related_name="repurpose_jobs")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    # Attribution: where the text came from. ``source_text`` is the extracted
    # text the run works from (capped), kept so the run can happen in a worker.
    source_type = models.CharField(max_length=8, choices=SourceType.choices)
    source_url = models.CharField(max_length=2000, blank=True, default="")
    source_label = models.CharField(max_length=255, blank=True, default="")
    source_text = models.TextField(blank=True, default="")
    source_post = models.ForeignKey("composer.Post", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    # What was asked for: ``[{"kind", "account_ids", "count"}]``, and every account involved.
    outputs_requested = models.JSONField(default=list)
    target_account_ids = models.JSONField(default=list)

    status = models.CharField(max_length=12, choices=Status.choices, default=Status.QUEUED, db_index=True)
    error = models.TextField(blank=True, default="")
    cancel_requested = models.BooleanField(default=False)

    # Surface that asked (an ``AIRequestLog.Source`` value) and what the run cost.
    origin = models.CharField(max_length=8, default="web")
    model = models.CharField(max_length=64, blank=True, default="")
    used_organization_key = models.BooleanField(default=False)
    calls_reserved = models.PositiveIntegerField(default=0)
    calls_made = models.PositiveIntegerField(default=0)
    token_cap = models.PositiveIntegerField(default=0)
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "repurpose_job"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["workspace", "-created_at"])]

    def __str__(self):
        return f"RepurposeJob({self.source_type}, {self.status})"

    @property
    def is_active(self) -> bool:
        return self.status in self.ACTIVE_STATUSES

    @property
    def source_name(self) -> str:
        return self.source_label or self.source_url or self.get_source_type_display()


class RepurposeOutput(models.Model):
    class Kind(models.TextChoices):
        THREAD = "thread", "Thread"
        POST = "post", "Long-form post"
        CAROUSEL = "carousel", "Carousel"
        VIDEO_SCRIPT = "video_script", "Short-video script"
        NEWSLETTER = "newsletter", "Newsletter blurb"
        STANDALONE = "standalone", "Standalone post"
        HASHTAGS = "hashtags", "Hashtag set"

    class Status(models.TextChoices):
        GENERATED = "generated", "Ready to review"
        ACCEPTED = "accepted", "Accepted"
        DISCARDED = "discarded", "Discarded"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job = models.ForeignKey(RepurposeJob, on_delete=models.CASCADE, related_name="outputs")
    kind = models.CharField(max_length=16, choices=Kind.choices)
    social_account = models.ForeignKey(
        "social_accounts.SocialAccount", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    position = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.GENERATED)

    title = models.CharField(max_length=255, blank=True, default="")
    # The main text (a post's caption, a thread's first part, a script as prose).
    text = models.TextField(blank=True, default="")
    # Later thread parts, carousel slide texts, script beats, hashtags.
    parts = models.JSONField(default=list, blank=True)
    # Structured extras: script hook/cta, slide objects, newsletter subject.
    data = models.JSONField(default=dict, blank=True)
    warnings = models.JSONField(default=list, blank=True)
    # Length as the target platform counts it, and its limit.
    char_count = models.PositiveIntegerField(default=0)
    char_limit = models.PositiveIntegerField(null=True, blank=True)

    post = models.ForeignKey("composer.Post", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    idea = models.ForeignKey("composer.Idea", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    document = models.ForeignKey(
        "media_library.MediaAsset", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "repurpose_output"
        ordering = ["position", "created_at"]

    def __str__(self):
        return f"RepurposeOutput({self.kind}, {self.status})"
