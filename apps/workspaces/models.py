import uuid

from django.db import models

from apps.common.managers import OrgScopedManager
from apps.common.validators import validate_hex_color


class Workspace(models.Model):
    class ApprovalWorkflowMode(models.TextChoices):
        NONE = "none", "None"
        OPTIONAL = "optional", "Optional"
        REQUIRED_INTERNAL = "required_internal", "Required (Internal)"
        REQUIRED_INTERNAL_AND_CLIENT = "required_internal_and_client", "Required (Internal + Client)"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="workspaces",
    )
    name = models.CharField(max_length=100)
    icon = models.ImageField(upload_to="workspaces/icons/%Y/%m/", blank=True)
    description = models.CharField(max_length=500, blank=True, default="")
    timezone = models.CharField(max_length=63, blank=True, default="")
    primary_color = models.CharField(max_length=7, blank=True, default="", validators=[validate_hex_color])
    secondary_color = models.CharField(max_length=7, blank=True, default="", validators=[validate_hex_color])
    default_hashtags = models.JSONField(default=list, blank=True)
    default_first_comment = models.TextField(blank=True, default="")
    approval_workflow_mode = models.CharField(
        max_length=40,
        choices=ApprovalWorkflowMode.choices,
        default=ApprovalWorkflowMode.NONE,
    )
    # Hours a post may wait in a review stage before it is overdue ("needs approval
    # by ..."); blank means no deadline. Reminders go out before it, escalation after.
    approval_sla_hours = models.PositiveIntegerField(null=True, blank=True)
    is_archived = models.BooleanField(default=False)

    # Brand voice for the AI writing assistant (apps.ai_writer): every prompt
    # made from this workspace carries it. All optional — blank means no voice
    # is set and the assistant writes in a neutral, friendly register.
    brand_voice_tone = models.TextField(blank=True, default="")
    brand_voice_words_to_use = models.TextField(blank=True, default="")
    brand_voice_words_to_avoid = models.TextField(blank=True, default="")
    # Up to three example posts, as a list of strings.
    brand_voice_examples = models.JSONField(default=list, blank=True)

    # What a reply to a customer review may and may not promise (refunds,
    # liability, who to contact). Sent with every AI-drafted review reply.
    review_reply_policy = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = OrgScopedManager()

    class Meta:
        db_table = "workspaces_workspace"

    def __str__(self):
        return self.name

    @property
    def effective_timezone(self):
        return self.timezone or self.organization.default_timezone


class BrandKit(models.Model):
    """A brand's logos, colours, fonts, copy and rules, for a workspace or (as the fallback) its organization.

    Exactly one of ``workspace`` and ``organization`` is set. Every field is
    optional; ``apps.workspaces.brand_kit.resolve`` fills what a workspace's kit
    leaves blank from the organization's. Brand voice stays on ``Workspace``
    (``brand_voice_*``) and is presented with the kit rather than copied here.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.OneToOneField(
        Workspace, on_delete=models.CASCADE, null=True, blank=True, related_name="brand_kit"
    )
    organization = models.OneToOneField(
        "organizations.Organization", on_delete=models.CASCADE, null=True, blank=True, related_name="brand_kit"
    )

    # Logos come from the media library; deleting the asset just empties the slot.
    logo_primary = models.ForeignKey(
        "media_library.MediaAsset", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    logo_mark = models.ForeignKey(
        "media_library.MediaAsset", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    logo_on_light = models.ForeignKey(
        "media_library.MediaAsset", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    logo_on_dark = models.ForeignKey(
        "media_library.MediaAsset", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    # [{"name": "Sunrise", "hex": "#f97316"}, ...]; the first swatch is the primary colour.
    colors = models.JSONField(default=list, blank=True)
    # [{"name": "Inter", "role": "heading"|"body"|"other", "asset_id": "<MediaAsset id>" or ""}, ...]
    fonts = models.JSONField(default=list, blank=True)
    tagline = models.CharField(max_length=150, blank=True, default="")
    default_hashtags = models.JSONField(default=list, blank=True)
    # [{"name": "Medical", "text": "Not medical advice."}, ...]; offered in the composer.
    disclaimers = models.JSONField(default=list, blank=True)
    dos = models.TextField(blank=True, default="")
    donts = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "workspaces_brand_kit"
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(workspace__isnull=False, organization__isnull=True)
                    | models.Q(workspace__isnull=True, organization__isnull=False)
                ),
                name="brand_kit_workspace_xor_organization",
            )
        ]

    def __str__(self):
        return f"Brand kit of {self.workspace or self.organization}"
