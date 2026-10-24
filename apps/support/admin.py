from django import forms
from django.contrib import admin

from apps.billing.plans import PLAN_CHOICES

from .models import Announcement, ImpersonationSession


class AnnouncementForm(forms.ModelForm):
    plan_slugs = forms.MultipleChoiceField(
        choices=PLAN_CHOICES,
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text="Only organizations on these plans see it. Leave empty for everyone.",
    )

    class Meta:
        model = Announcement
        fields = "__all__"


@admin.register(Announcement)
class AnnouncementAdmin(admin.ModelAdmin):
    form = AnnouncementForm
    list_display = ("message", "severity", "starts_at", "ends_at", "is_active")
    list_filter = ("severity", "is_active")
    search_fields = ("message",)


@admin.register(ImpersonationSession)
class ImpersonationSessionAdmin(admin.ModelAdmin):
    """The audit trail: read-only, so a session can't be rewritten after the fact."""

    list_display = ("started_at", "staff_email", "target_email", "reason", "ip_address", "ended_at", "end_reason")
    list_filter = ("end_reason",)
    search_fields = ("staff_email", "target_email", "reason")
    readonly_fields = tuple(f.name for f in ImpersonationSession._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
