from django.contrib import admin

from .models import AccountAutonomyOverride, AgentAction, AutonomyPolicy


@admin.register(AutonomyPolicy)
class AutonomyPolicyAdmin(admin.ModelAdmin):
    list_display = ("workspace", "draft_level", "publish_level", "reply_level", "recycle_level", "crisis_paused")
    list_filter = ("crisis_paused", "publish_level", "reply_level")
    search_fields = ("workspace__name",)
    raw_id_fields = ("workspace", "crisis_paused_by")


@admin.register(AccountAutonomyOverride)
class AccountAutonomyOverrideAdmin(admin.ModelAdmin):
    list_display = ("social_account", "workspace", "draft_level", "publish_level", "reply_level", "recycle_level")
    raw_id_fields = ("workspace", "social_account")


@admin.register(AgentAction)
class AgentActionAdmin(admin.ModelAdmin):
    """The audit trail is read-only here too: nobody edits or deletes what happened."""

    list_display = ("created_at", "workspace", "actor_type", "actor_name", "operation", "decision")
    list_filter = ("actor_type", "decision", "action_type")
    search_fields = ("actor_name", "operation", "workspace__name")
    date_hierarchy = "created_at"
    readonly_fields = tuple(f.name for f in AgentAction._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
