"""Short links, UTM defaults and click counters.

A :class:`ShortLink` is one destination made for one workspace, usually for one
account's copy of one post. Counters are one row per link, day and kind
(:class:`LinkDailyStat`); no visitor, IP address or cookie is stored anywhere.
"""

from __future__ import annotations

import uuid

from django.db import models

from apps.common.encryption import EncryptedTextField
from apps.linkinbio.validators import URL_MAX, validate_domain, validate_http_url


class LinkSettings(models.Model):
    """A workspace's short-link and UTM preferences (one row, created on first use)."""

    class ShortenMode(models.TextChoices):
        ALWAYS = "always", "Always shorten links"
        ASK = "ask", "Ask me for each post"
        NEVER = "never", "Never shorten links"

    class Provider(models.TextChoices):
        BUILTIN = "builtin", "Built-in (our own address)"
        DUB = "dub", "Dub"
        SHORTIO = "shortio", "Short.io"
        KUTT = "kutt", "Kutt"

    class CampaignFrom(models.TextChoices):
        NONE = "none", "Leave blank"
        POST = "post", "The post's title (or first words)"
        CATEGORY = "category", "The post's content category"
        TAG = "tag", "The post's first tag"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.OneToOneField("workspaces.Workspace", on_delete=models.CASCADE, related_name="link_settings")

    # Existing workspaces keep their links as typed until an owner opts in.
    shorten_mode = models.CharField(max_length=6, choices=ShortenMode.choices, default=ShortenMode.NEVER)
    provider = models.CharField(max_length=8, choices=Provider.choices, default=Provider.BUILTIN)
    # The customer's own key for the chosen service. Encrypted at rest, never shown again after saving.
    api_key = EncryptedTextField(blank=True, default="")
    # The service's domain for new links (Dub, Short.io, Kutt). Blank uses the service's default.
    provider_domain = models.CharField(max_length=253, blank=True, default="", validators=[validate_domain])
    # A self-hosted Kutt API address (https only). Blank uses kutt.it.
    provider_base_url = models.CharField(max_length=200, blank=True, default="")
    # Our own short links on this host instead of the app's address; off (blank) by default.
    custom_short_domain = models.CharField(
        max_length=253, null=True, blank=True, unique=True, validators=[validate_domain]
    )

    utm_enabled = models.BooleanField(default=False)
    utm_medium = models.CharField(max_length=60, blank=True, default="social")
    utm_campaign_from = models.CharField(max_length=8, choices=CampaignFrom.choices, default=CampaignFrom.POST)
    # Fixed campaign used when nothing else is available (and always, when set and campaign_from is "none").
    utm_campaign = models.CharField(max_length=100, blank=True, default="")
    # Extra parameters added to every link: {"utm_content": "spring", "ref": "social"}.
    utm_custom = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "links_settings"

    def __str__(self):
        return f"LinkSettings({self.workspace_id})"

    @property
    def uses_external_provider(self) -> bool:
        return self.provider != self.Provider.BUILTIN and bool(self.api_key)


class ShortLink(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE, related_name="short_links")
    # The path segment of a built-in link; for an external service it is that service's own slug.
    code = models.CharField(max_length=40, db_index=True)
    destination_url = models.CharField(max_length=URL_MAX, validators=[validate_http_url])
    # The address people see. Built-in links are served from here at /s/<code>.
    short_url = models.CharField(max_length=500)
    provider = models.CharField(max_length=8, choices=LinkSettings.Provider.choices, default="builtin")
    external_id = models.CharField(max_length=200, blank=True, default="")
    post = models.ForeignKey("composer.Post", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    platform_post = models.ForeignKey(
        "composer.PlatformPost", on_delete=models.SET_NULL, null=True, blank=True, related_name="short_links"
    )
    social_account = models.ForeignKey(
        "social_accounts.SocialAccount", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_by = models.ForeignKey("accounts.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    is_active = models.BooleanField(default=True)
    # What an external service last said about clicks (its own count, shown beside ours).
    external_clicks = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "links_short_link"
        ordering = ["-created_at"]
        constraints = [
            # Only built-in codes are served by us, so only they must be unique across the site.
            models.UniqueConstraint(fields=["code"], condition=models.Q(provider="builtin"), name="links_builtin_code"),
        ]
        indexes = [
            models.Index(fields=["workspace", "-created_at"], name="links_link_ws_created"),
            models.Index(fields=["platform_post"], name="links_link_platform_post"),
        ]

    def __str__(self):
        return f"{self.short_url} -> {self.destination_url}"


class LinkDailyStat(models.Model):
    """Clicks on a link on one UTC day, and roughly by how many people.

    ``kind`` is ``click`` (a person) or ``preview`` (a platform's link-preview
    fetcher, counted apart so it never inflates clicks). ``uniques`` counts first
    sightings of a visitor token salted with the day and never stored.
    """

    class Kind(models.TextChoices):
        CLICK = "click", "Click"
        PREVIEW = "preview", "Preview fetch"

    link = models.ForeignKey(ShortLink, on_delete=models.CASCADE, related_name="stats")
    day = models.DateField()
    kind = models.CharField(max_length=7, choices=Kind.choices)
    count = models.PositiveIntegerField(default=0)
    uniques = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "links_daily_stat"
        constraints = [models.UniqueConstraint(fields=["link", "day", "kind"], name="links_stat_unique")]
        indexes = [models.Index(fields=["day"], name="links_stat_day")]
