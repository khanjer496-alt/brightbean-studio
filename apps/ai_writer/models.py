"""Models backing the AI writing assistant.

Two roles:

- ``OrganizationAIKey`` (one per Org, optional): the organization's own
  provider key ("bring your own key"). When present it is used instead of
  the platform key, and the requests it makes don't count against the
  plan's daily AI allowance.
- ``AIRequestLog``: one row per provider call — who asked for what, on
  whose key, and how many tokens it cost. The plan's daily allowance is
  counted from these rows (``apps.billing.entitlements.ai_request_count``).

The workspace's brand voice lives on ``Workspace`` itself, next to its other
publishing defaults.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models

from apps.common.encryption import EncryptedTextField


class OrganizationAIKey(models.Model):
    """An organization's own AI provider key, encrypted at rest."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.OneToOneField(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="ai_key",
    )
    # A key in ``apps.ai_writer.client.AI_PROVIDERS``.
    provider = models.CharField(max_length=32, default="anthropic")
    # Assign plaintext; EncryptedTextField encrypts on save and decrypts on
    # load, so calling encrypt() here would double-encrypt.
    api_key = EncryptedTextField()
    # Last four characters, so the settings page can show which key is
    # stored without ever rendering it.
    key_hint = models.CharField(max_length=8, blank=True, default="")
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "ai_writer_organization_key"

    def __str__(self):
        return f"OrganizationAIKey({self.organization_id}, {self.provider}, …{self.key_hint})"


class AIRequestLog(models.Model):
    """Per-call audit row. Request and response text are never stored."""

    class Source(models.TextChoices):
        WEB = "web", "Web"
        API = "api", "Agent API"
        MCP = "mcp", "MCP"

    class Status(models.TextChoices):
        OK = "ok", "OK"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="ai_request_logs",
    )
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ai_request_logs",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ai_request_logs",
    )
    action = models.CharField(max_length=32)
    source = models.CharField(max_length=8, choices=Source.choices, default=Source.WEB)
    provider = models.CharField(max_length=32)
    model = models.CharField(max_length=64)
    used_organization_key = models.BooleanField(default=False)
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.OK)
    # ``AIError.code`` for failed calls, so failures can be grouped without
    # storing provider messages.
    error_code = models.CharField(max_length=32, blank=True, default="")
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    latency_ms = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "ai_writer_request_log"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["organization", "-created_at"]),
        ]

    def __str__(self):
        return f"AIRequestLog({self.action}, {self.status}, {self.input_tokens}+{self.output_tokens})"
