"""Outbound webhook endpoints and their delivery log.

An endpoint belongs to one workspace and subscribes to a chosen set of event
types. Each emitted event becomes one ``WebhookDelivery`` row per subscribed
endpoint; the row holds the exact payload that is signed and sent, so retries
and manual resends are byte-identical and share one delivery id.
"""

import json
import uuid

from django.conf import settings
from django.db import models

from apps.common.encryption import EncryptedTextField


class EventType(models.TextChoices):
    POST_SCHEDULED = "post.scheduled", "Post scheduled"
    POST_PUBLISHED = "post.published", "Post published"
    POST_FAILED = "post.failed", "Post failed"
    POST_PARTIALLY_PUBLISHED = "post.partially_published", "Post partially published (thread)"
    POST_APPROVED = "post.approved", "Post approved"
    POST_CHANGES_REQUESTED = "post.changes_requested", "Post changes requested"
    COMMENT_CREATED = "comment.created", "Approval comment created"
    INBOX_MESSAGE_RECEIVED = "inbox.message.received", "Inbox message received"
    ACCOUNT_DISCONNECTED = "account.disconnected", "Social account disconnected"
    AGENT_ACTION_EXECUTED = "agent.action.executed", "Agent action executed"
    AGENT_ACTION_HELD = "agent.action.held", "Agent action held for review"
    AGENT_ACTION_BLOCKED = "agent.action.blocked", "Agent action blocked"
    WEBHOOK_TEST = "webhook.test", "Test ping"


# What an endpoint can subscribe to. The test ping is sent on demand and is
# never part of a subscription.
SUBSCRIBABLE_EVENT_TYPES = [value for value in EventType.values if value != EventType.WEBHOOK_TEST]


class WebhookEndpoint(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="webhook_endpoints",
    )
    url = models.URLField(max_length=2000)
    description = models.CharField(max_length=255, blank=True, default="")
    is_enabled = models.BooleanField(default=True)
    event_types = models.JSONField(
        default=list, help_text="Subscribed event types (subset of SUBSCRIBABLE_EVENT_TYPES)."
    )
    # Needed in the clear to sign each request, so it is encrypted at rest
    # rather than hashed. The plaintext is shown to the user once, when it is
    # generated or rotated.
    secret = EncryptedTextField()
    secret_hint = models.CharField(max_length=8, blank=True, default="", help_text="Last characters of the secret.")
    consecutive_failures = models.PositiveIntegerField(default=0)
    disabled_at = models.DateTimeField(null=True, blank=True)
    disabled_reason = models.CharField(max_length=255, blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="webhook_endpoints",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "webhooks_endpoint"
        ordering = ["created_at", "id"]
        indexes = [models.Index(fields=["workspace", "is_enabled"])]

    def __str__(self):
        return f"{self.url} ({self.workspace_id})"


class DeliveryStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    DELIVERED = "delivered", "Delivered"
    FAILED = "failed", "Failed"


class WebhookDelivery(models.Model):
    # The id doubles as the ``PostDelegate-Delivery`` header, so it is stable
    # across retries and resends.
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    endpoint = models.ForeignKey(WebhookEndpoint, on_delete=models.CASCADE, related_name="deliveries")
    event_id = models.UUIDField()
    event_type = models.CharField(max_length=50, choices=EventType.choices)
    payload = models.JSONField()
    status = models.CharField(max_length=10, choices=DeliveryStatus.choices, default=DeliveryStatus.PENDING)
    attempts = models.PositiveIntegerField(default=0)
    # When the next attempt is due. The sweep and the first-attempt task both
    # claim a row by pushing this forward, so one row is never sent twice at once.
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    last_status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    last_duration_ms = models.PositiveIntegerField(null=True, blank=True)
    last_error = models.CharField(max_length=500, blank=True, default="")
    response_body = models.TextField(blank=True, default="")
    delivered_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "webhooks_delivery"
        ordering = ["-created_at", "id"]
        constraints = [
            models.UniqueConstraint(fields=["endpoint", "event_id"], name="webhooks_delivery_unique_event"),
        ]
        indexes = [
            models.Index(fields=["endpoint", "-created_at"]),
            models.Index(fields=["status", "next_attempt_at"]),
            models.Index(fields=["created_at"]),
        ]

    def __str__(self):
        return f"{self.event_type} -> {self.endpoint_id} ({self.status})"

    @property
    def payload_pretty(self) -> str:
        return json.dumps(self.payload, indent=2, sort_keys=True)
