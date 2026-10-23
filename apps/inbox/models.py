"""Models for the Unified Social Inbox (F-3.1)."""

import uuid

from django.conf import settings
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVector
from django.db import models

from apps.common.managers import WorkspaceScopedManager


class InboxMessage(models.Model):
    api_refreshed_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class MessageType(models.TextChoices):
        COMMENT = "comment", "Comment"
        MENTION = "mention", "Mention"
        DM = "dm", "Direct Message"
        REVIEW = "review", "Review"

    class Status(models.TextChoices):
        UNREAD = "unread", "Unread"
        OPEN = "open", "Open"
        RESOLVED = "resolved", "Resolved"
        ARCHIVED = "archived", "Archived"

    class Sentiment(models.TextChoices):
        POSITIVE = "positive", "Positive"
        NEUTRAL = "neutral", "Neutral"
        NEGATIVE = "negative", "Negative"

    class SentimentSource(models.TextChoices):
        AUTO = "auto", "Auto"
        MANUAL = "manual", "Manual"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="inbox_messages",
    )
    social_account = models.ForeignKey(
        "social_accounts.SocialAccount",
        on_delete=models.CASCADE,
        related_name="inbox_messages",
    )
    platform_message_id = models.CharField(max_length=255, db_index=True)
    message_type = models.CharField(
        max_length=20,
        choices=MessageType.choices,
        default=MessageType.COMMENT,
        db_index=True,
    )
    sender_name = models.CharField(max_length=255)
    sender_handle = models.CharField(max_length=255, blank=True, default="")
    sender_avatar_url = models.URLField(max_length=500, blank=True, default="")
    body = models.TextField(blank=True, default="")
    sentiment = models.CharField(
        max_length=10,
        choices=Sentiment.choices,
        default=Sentiment.NEUTRAL,
        db_index=True,
    )
    sentiment_source = models.CharField(
        max_length=10,
        choices=SentimentSource.choices,
        default=SentimentSource.AUTO,
    )
    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.UNREAD,
        db_index=True,
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_inbox_messages",
    )
    parent_message = models.ForeignKey(
        "self",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="thread_replies",
    )
    related_post = models.ForeignKey(
        "composer.PlatformPost",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="inbox_messages",
    )
    # Star rating (1-5) of a ``review``; null for every other message type.
    rating = models.PositiveSmallIntegerField(null=True, blank=True)
    extra = models.JSONField(default=dict, blank=True)
    received_at = models.DateTimeField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = WorkspaceScopedManager()

    class Meta:
        db_table = "inbox_message"
        ordering = ["-received_at"]
        unique_together = [("social_account", "platform_message_id")]
        indexes = [
            models.Index(
                fields=["workspace", "status", "-received_at"],
                name="inbox_msg_ws_status_recv",
            ),
            models.Index(
                fields=["workspace", "assigned_to", "status"],
                name="inbox_msg_ws_assign_status",
            ),
            models.Index(
                fields=["workspace", "social_account", "-received_at"],
                name="inbox_msg_ws_account_recv",
            ),
            # Feed order with its tie-break, for the cursor pagination.
            models.Index(
                fields=["workspace", "-received_at", "-id"],
                name="inbox_msg_ws_recv_id",
            ),
            # The feed's "N new messages" poll counts by arrival.
            models.Index(
                fields=["workspace", "created_at"],
                name="inbox_msg_ws_created",
            ),
            models.Index(
                fields=["workspace", "assigned_to", "-received_at"],
                name="inbox_msg_ws_assign_recv",
            ),
            models.Index(
                fields=["workspace", "sentiment", "-received_at"],
                name="inbox_msg_ws_sent_recv",
            ),
            models.Index(
                fields=["workspace", "message_type", "rating", "-received_at"],
                name="inbox_msg_ws_type_rating",
            ),
            # Must match ``apps.inbox.feed.search_vector`` for the planner to use it.
            GinIndex(
                SearchVector("body", "sender_name", "sender_handle", config="simple"),
                name="inbox_msg_search_gin",
            ),
        ]

    def __str__(self):
        return f"{self.get_message_type_display()} from {self.sender_name}"

    @property
    def platform(self):
        return self.social_account.platform

    @property
    def review_reply(self) -> dict:
        """The owner's public reply already on the platform (``comment``, ``update_time``), or {}."""
        return (self.extra or {}).get("review_reply") or {}

    @property
    def needs_reply(self) -> bool:
        """A review with no public reply yet."""
        return self.message_type == self.MessageType.REVIEW and not self.review_reply

    @property
    def attachments(self):
        from providers.meta_messaging import normalize_attachments

        extra = self.extra or {}
        return normalize_attachments(extra.get("attachments", (extra.get("message") or {}).get("attachments", [])))


def rating_band(rating: int | None) -> str:
    """The saved-reply band for a star rating: positive (4-5), neutral (3), negative (1-2), else ""."""
    if not rating:
        return ""
    if rating >= 4:
        return SavedReply.RatingBand.POSITIVE
    return SavedReply.RatingBand.NEUTRAL if rating == 3 else SavedReply.RatingBand.NEGATIVE


class InboxReply(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    inbox_message = models.ForeignKey(
        InboxMessage,
        on_delete=models.CASCADE,
        related_name="replies",
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="inbox_replies",
    )
    body = models.TextField()
    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.DRAFT,
        db_index=True,
    )
    platform_reply_id = models.CharField(max_length=255, blank=True, default="")
    send_error = models.TextField(blank=True, default="")
    # ``sent_at`` is null until the reply is actually delivered to the platform;
    # a row now exists in ``draft``/``failed`` states before any send happens.
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "inbox_reply"
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.get_status_display()} reply by {self.author} ({self.created_at:%Y-%m-%d %H:%M})"


class InternalNote(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    inbox_message = models.ForeignKey(
        InboxMessage,
        on_delete=models.CASCADE,
        related_name="internal_notes",
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="inbox_notes",
    )
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "inbox_internal_note"
        ordering = ["created_at"]

    def __str__(self):
        return f"Note by {self.author} on {self.created_at:%Y-%m-%d %H:%M}"


class SavedReply(models.Model):
    class RatingBand(models.TextChoices):
        ANY = "", "Any message"
        POSITIVE = "positive", "Reviews: 4-5 stars"
        NEUTRAL = "neutral", "Reviews: 3 stars"
        NEGATIVE = "negative", "Reviews: 1-2 stars"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="saved_replies",
    )
    title = models.CharField(max_length=255)
    body = models.TextField(
        help_text="Supports variables: {sender_name}, {account_name}, {post_url}",
    )
    # Offered first for reviews in this star band; blank applies to everything.
    rating_band = models.CharField(max_length=10, choices=RatingBand.choices, blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="created_saved_replies",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = WorkspaceScopedManager()

    class Meta:
        db_table = "inbox_saved_reply"
        ordering = ["title"]

    def __str__(self):
        return self.title

    def render(self, context: dict) -> str:
        """Substitute variables in body with context values."""
        text = self.body
        for key, value in context.items():
            text = text.replace(f"{{{key}}}", str(value))
        return text


class InboxSLAConfig(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.OneToOneField(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="inbox_sla_config",
    )
    target_response_minutes = models.PositiveIntegerField(default=120)
    is_active = models.BooleanField(default=False)
    auto_resolve_on_reply = models.BooleanField(
        default=True,
        help_text="Automatically mark messages as resolved when a reply is sent.",
    )

    class Meta:
        db_table = "inbox_sla_config"

    def __str__(self):
        return f"SLA Config for {self.workspace} ({self.target_response_minutes}min)"
