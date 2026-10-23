from django.contrib import admin

from .models import AIRequestLog, OrganizationAIKey


@admin.register(OrganizationAIKey)
class OrganizationAIKeyAdmin(admin.ModelAdmin):
    list_display = ("organization", "provider", "key_hint", "updated_by", "updated_at")
    search_fields = ("organization__name",)
    # The key itself is never shown, not even to staff.
    exclude = ("api_key",)
    readonly_fields = ("id", "organization", "provider", "key_hint", "updated_by", "created_at", "updated_at")


@admin.register(AIRequestLog)
class AIRequestLogAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "organization",
        "user",
        "action",
        "source",
        "status",
        "input_tokens",
        "output_tokens",
        "used_organization_key",
    )
    list_filter = ("action", "source", "status", "used_organization_key")
    search_fields = ("organization__name", "user__email")
    readonly_fields = [f.name for f in AIRequestLog._meta.fields]
