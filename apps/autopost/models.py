"""RSS auto-posting: a feed a workspace already follows, plus what to do with its new items.

Models:
    FeedAutopost - The settings and polling state of one feed (``composer.Feed``): target accounts, mode, destination, filters, daily cap.
    FeedItem - What happened to one item of the feed (posted, drafted, held, skipped, failed), once, so an item is never handled twice.
"""

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.common.managers import WorkspaceScopedManager


class FeedAutopost(models.Model):
    class Mode(models.TextChoices):
        LINK = "link", "Post the link as is"
        AI = "ai", "AI-written post in your brand voice"
        AI_IMAGE = "ai_image", "AI-written post with the article's image"

    class Destination(models.TextChoices):
        QUEUE = "queue", "Next free slot in the queue"
        DELAY = "delay", "Schedule after a fixed delay"
        DRAFT = "draft", "Save as a draft for approval"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE, related_name="feed_autoposts")
    feed = models.OneToOneField("composer.Feed", on_delete=models.CASCADE, related_name="autopost")
    is_active = models.BooleanField(default=True)
    accounts = models.ManyToManyField("social_accounts.SocialAccount", related_name="feed_autoposts", blank=True)
    mode = models.CharField(max_length=10, choices=Mode.choices, default=Mode.LINK)
    destination = models.CharField(max_length=10, choices=Destination.choices, default=Destination.DRAFT)
    delay_minutes = models.PositiveIntegerField(default=60)
    include_keywords = models.JSONField(default=list, blank=True)
    exclude_keywords = models.JSONField(default=list, blank=True)
    max_posts_per_day = models.PositiveSmallIntegerField(default=3)

    # Only items published after this moment are posted, so adding a feed never floods the accounts with its back catalogue.
    started_at = models.DateTimeField(default=timezone.now)
    # Set once the first poll has recorded the items that were already there.
    baselined_at = models.DateTimeField(null=True, blank=True)

    # Polling state: what the server said last time (so the next request can be conditional), and the backoff.
    etag = models.CharField(max_length=255, blank=True, default="")
    last_modified = models.CharField(max_length=255, blank=True, default="")
    last_checked_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    next_check_at = models.DateTimeField(null=True, blank=True, db_index=True)
    consecutive_failures = models.PositiveSmallIntegerField(default=0)
    last_error = models.CharField(max_length=300, blank=True, default="")

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = WorkspaceScopedManager()

    class Meta:
        db_table = "autopost_feed"
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"FeedAutopost({self.feed_id})"


class FeedItem(models.Model):
    """One item of a feed and what became of it. The unique key makes the poller idempotent."""

    class Status(models.TextChoices):
        PROCESSING = "processing", "Processing"
        SCHEDULED = "scheduled", "Scheduled"
        DRAFT = "draft", "Saved as a draft"
        HELD = "held", "Held for approval"
        SKIPPED = "skipped", "Skipped"
        FAILED = "failed", "Failed"
        OLDER = "older", "Already there when the feed was added"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    autopost = models.ForeignKey(FeedAutopost, on_delete=models.CASCADE, related_name="items")
    # SHA-256 of the item's GUID (else its link and title), so the same item is recognised again however the feed re-orders it.
    key = models.CharField(max_length=64)
    title = models.CharField(max_length=300, blank=True, default="")
    link = models.URLField(max_length=1000, blank=True, default="")
    published_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PROCESSING)
    reason = models.TextField(blank=True, default="")
    ai_used = models.BooleanField(default=False)
    # One entry per target account: {"account", "status", "reason", "post_id"}.
    outcomes = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "autopost_item"
        ordering = ["-created_at", "-id"]
        constraints = [models.UniqueConstraint(fields=["autopost", "key"], name="uniq_feeditem_autopost_key")]

    def __str__(self):
        return f"FeedItem({self.title[:40]}: {self.status})"
