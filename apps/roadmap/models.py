from django.db import models


class RoadmapItem(models.Model):
    """One line on the public roadmap, edited in Django admin. No votes, no comments."""

    class Status(models.TextChoices):
        NOW = "now", "Now"
        NEXT = "next", "Next"
        LATER = "later", "Later"

    title = models.CharField(max_length=120)
    description = models.CharField(max_length=300, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.LATER)
    order = models.PositiveIntegerField(default=0, help_text="Lower numbers show first within a column.")
    link_url = models.URLField(blank=True, help_text="Optional page with more detail.")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "roadmap_item"
        ordering = ["order", "id"]

    def __str__(self):
        return self.title
