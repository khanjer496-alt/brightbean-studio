"""Link-in-bio pages: a public page of links per workspace, and its privacy-friendly counters.

A :class:`BioPage` lives at ``/l/<slug>/``. Counters are kept as one row per
page, day, kind and target (:class:`BioDailyStat`); no visitor, IP address or
cookie is stored anywhere.
"""

from __future__ import annotations

import uuid

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from apps.common.validators import validate_hex_color

from . import validators

NIL_TARGET = uuid.UUID(int=0)


class BioPage(models.Model):
    class Theme(models.TextChoices):
        CLEAN = "clean", "Clean (light)"
        MIDNIGHT = "midnight", "Midnight (dark)"
        SUNRISE = "sunrise", "Sunrise (warm)"
        FOREST = "forest", "Forest (green)"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE, related_name="bio_pages")
    slug = models.CharField(max_length=validators.SLUG_MAX, unique=True, validators=[validators.validate_slug])
    title = models.CharField(max_length=80)
    bio = models.TextField(max_length=300, blank=True, default="")
    avatar = models.ForeignKey(
        "media_library.MediaAsset", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    theme = models.CharField(max_length=12, choices=Theme.choices, default=Theme.CLEAN)
    # Blank uses the theme's own accent.
    accent_color = models.CharField(max_length=7, blank=True, default="", validators=[validate_hex_color])
    social_accounts = models.ManyToManyField("social_accounts.SocialAccount", blank=True, related_name="+")
    show_latest_posts = models.BooleanField(default=False)
    # Empty means published posts from every account in the workspace.
    posts_account = models.ForeignKey(
        "social_accounts.SocialAccount", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    is_published = models.BooleanField(default=True)
    # Where the page is also served when LINKINBIO_CUSTOM_DOMAINS_ENABLED is on; the owner points a CNAME here.
    custom_domain = models.CharField(
        max_length=253, null=True, blank=True, unique=True, validators=[validators.validate_domain]
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "linkinbio_page"
        ordering = ["created_at", "id"]

    def __str__(self):
        return self.title


class BioLink(models.Model):
    class Icon(models.TextChoices):
        NONE = "", "No icon"
        LINK = "link", "Link"
        SHOP = "shop", "Shop"
        VIDEO = "video", "Video"
        MUSIC = "music", "Music"
        MAIL = "mail", "Newsletter"
        CALENDAR = "calendar", "Event"
        STAR = "star", "Featured"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    page = models.ForeignKey(BioPage, on_delete=models.CASCADE, related_name="links")
    title = models.CharField(max_length=100)
    url = models.CharField(max_length=validators.URL_MAX, validators=[validators.validate_http_url])
    icon = models.CharField(max_length=12, choices=Icon.choices, blank=True, default="")
    thumbnail = models.ForeignKey(
        "media_library.MediaAsset", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    position = models.PositiveIntegerField(default=0)
    # The link shows on the public page from ``starts_at`` (inclusive) until ``ends_at`` (exclusive).
    starts_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "linkinbio_link"
        ordering = ["position", "created_at", "id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(starts_at__isnull=True) | Q(ends_at__isnull=True) | Q(starts_at__lt=models.F("ends_at")),
                name="linkinbio_link_window_ordered",
            )
        ]

    def __str__(self):
        return self.title

    def clean(self):
        if self.starts_at and self.ends_at and self.starts_at >= self.ends_at:
            raise ValidationError("The end must be after the start.")

    def is_visible(self, now) -> bool:
        if not self.is_active:
            return False
        if self.starts_at and now < self.starts_at:
            return False
        return not (self.ends_at and now >= self.ends_at)


class BioPostLink(models.Model):
    """Where a published post's tile on a page's grid leads (Instagram "linkin.bio" style)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    post = models.OneToOneField("composer.Post", on_delete=models.CASCADE, related_name="bio_link")
    url = models.CharField(max_length=validators.URL_MAX, validators=[validators.validate_http_url])
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "linkinbio_post_link"


class BioDailyStat(models.Model):
    """How often a page was viewed or one of its targets clicked on a day, and roughly by how many people.

    ``kind`` is ``view`` (the page itself, ``target_id`` nil), ``link`` (a
    :class:`BioLink`) or ``tile`` (a :class:`BioPostLink`). ``uniques`` counts
    first sightings of a visitor token that is salted with the day and never
    stored, so it is an estimate that cannot be traced to a person.
    """

    class Kind(models.TextChoices):
        VIEW = "view", "Page view"
        LINK = "link", "Link click"
        TILE = "tile", "Post tile click"

    page = models.ForeignKey(BioPage, on_delete=models.CASCADE, related_name="stats")
    day = models.DateField()
    kind = models.CharField(max_length=5, choices=Kind.choices)
    target_id = models.UUIDField(default=NIL_TARGET)
    count = models.PositiveIntegerField(default=0)
    uniques = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "linkinbio_daily_stat"
        constraints = [
            models.UniqueConstraint(fields=["page", "day", "kind", "target_id"], name="linkinbio_stat_unique"),
        ]
        indexes = [models.Index(fields=["page", "day"], name="linkinbio_stat_page_day")]
