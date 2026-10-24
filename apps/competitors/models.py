"""Public profiles a workspace benchmarks itself against, and their daily history."""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models


class Competitor(models.Model):
    """One public profile on one network, tracked by one workspace."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE, related_name="competitors")
    # A key in ``providers.PROVIDER_REGISTRY`` whose provider supports public profile metrics.
    platform = models.CharField(max_length=32)
    # Canonical form from ``SocialProvider.normalize_competitor_handle``.
    handle = models.CharField(max_length=255)
    display_name = models.CharField(max_length=255, blank=True, default="")
    profile_url = models.URLField(max_length=500, blank=True, default="")
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    # The last failure's user-facing sentence; cleared by the next success.
    last_error = models.CharField(max_length=300, blank=True, default="")

    class Meta:
        db_table = "competitors_competitor"
        ordering = ["platform", "handle"]
        constraints = [
            models.UniqueConstraint(fields=["workspace", "platform", "handle"], name="competitor_unique_handle"),
        ]

    def __str__(self) -> str:
        return f"{self.platform}:{self.handle}"

    @property
    def label(self) -> str:
        return self.display_name or self.handle


class CompetitorSnapshot(models.Model):
    """One day's public numbers for a competitor; a second run on the same day overwrites it."""

    competitor = models.ForeignKey(Competitor, on_delete=models.CASCADE, related_name="snapshots")
    date = models.DateField()
    followers = models.BigIntegerField(null=True, blank=True)
    posts_count = models.BigIntegerField(null=True, blank=True)
    avg_engagement = models.FloatField(null=True, blank=True)
    posts_per_week = models.FloatField(null=True, blank=True)
    sampled_posts = models.PositiveIntegerField(default=0)
    captured_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "competitors_snapshot"
        ordering = ["date"]
        constraints = [
            models.UniqueConstraint(fields=["competitor", "date"], name="competitor_snapshot_unique_day"),
        ]

    def __str__(self) -> str:
        return f"{self.competitor_id} · {self.date} · {self.followers}"
