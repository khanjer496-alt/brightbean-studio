from django.contrib import admin

from .models import (
    ConnectionLink,
    ConnectionLinkUsage,
    DismissedHint,
    OnboardingChecklist,
    OnboardingFlow,
    SampleWorkspace,
)


@admin.register(ConnectionLink)
class ConnectionLinkAdmin(admin.ModelAdmin):
    list_display = ("workspace", "created_by", "expires_at", "revoked_at", "created_at")
    list_filter = ("workspace",)
    readonly_fields = ("id", "token", "created_at")
    search_fields = ("workspace__name", "created_by__email")


@admin.register(ConnectionLinkUsage)
class ConnectionLinkUsageAdmin(admin.ModelAdmin):
    list_display = ("connection_link", "social_account", "connected_at")
    readonly_fields = ("id", "connected_at")


@admin.register(OnboardingChecklist)
class OnboardingChecklistAdmin(admin.ModelAdmin):
    list_display = ("user", "workspace", "is_dismissed", "dismissed_at")
    list_filter = ("is_dismissed",)
    readonly_fields = ("id",)


@admin.register(OnboardingFlow)
class OnboardingFlowAdmin(admin.ModelAdmin):
    list_display = ("user", "workspace", "account_kind", "step", "status", "updated_at")
    list_filter = ("status", "account_kind")
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(DismissedHint)
class DismissedHintAdmin(admin.ModelAdmin):
    list_display = ("user", "key", "dismissed_at")
    readonly_fields = ("id", "dismissed_at")


@admin.register(SampleWorkspace)
class SampleWorkspaceAdmin(admin.ModelAdmin):
    list_display = ("workspace", "created_by", "created_at")
    readonly_fields = ("created_at",)
