"""Saved groups of a workspace's social accounts ("Client A: all channels")."""

from __future__ import annotations

import uuid

from django.db import models
from django.db.models.functions import Lower


class AccountSet(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE, related_name="account_sets")
    name = models.CharField(max_length=80)
    accounts = models.ManyToManyField("social_accounts.SocialAccount", blank=True, related_name="account_sets")
    # Optional per-account defaults, ``{"<account id>": {"board_id": "123", "privacy_level": "SELF_ONLY"}}``: the
    # same ``platform_extra`` keys the composer saves, restricted to what each platform allows (see services).
    defaults = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey("accounts.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "account_sets_set"
        ordering = [Lower("name"), "created_at"]
        constraints = [
            models.UniqueConstraint(Lower("name"), "workspace", name="account_sets_unique_name"),
        ]

    def __str__(self):
        return self.name
