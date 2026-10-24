from django.contrib import admin

from apps.api_keys.models import ApiKey, ApiKeyAuditLog, ApiKeyDailyUsage, ApiKeyUsageHourly, ConnectedApp


@admin.register(ApiKey)
class ApiKeyAdmin(admin.ModelAdmin):
    list_display = ("name", "workspace", "issued_by", "lookup_prefix", "revoked_at", "last_used_at", "created_at")
    list_filter = ("workspace", "revoked_at")
    search_fields = ("name", "lookup_prefix")
    readonly_fields = (
        "id",
        "lookup_prefix",
        "token_hash",
        "last_used_at",
        "last_used_ip",
        "created_at",
    )
    filter_horizontal = ("social_accounts",)


@admin.register(ApiKeyAuditLog)
class ApiKeyAuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "api_key", "action", "method", "path", "status_code", "ip")
    list_filter = ("action", "status_code")
    search_fields = ("path", "api_key__name")
    readonly_fields = tuple(f.name for f in ApiKeyAuditLog._meta.fields)


@admin.register(ApiKeyUsageHourly)
class ApiKeyUsageHourlyAdmin(admin.ModelAdmin):
    """Who is calling how much. Sort by ``count`` to find a poller."""

    list_display = ("hour_start", "actor", "api_key", "workspace", "action", "status_code", "count", "last_seen_at")
    list_filter = ("action", "status_code")
    search_fields = ("actor", "api_key__name", "api_key__lookup_prefix")
    date_hierarchy = "hour_start"
    readonly_fields = tuple(f.name for f in ApiKeyUsageHourly._meta.fields)


@admin.register(ApiKeyDailyUsage)
class ApiKeyDailyUsageAdmin(admin.ModelAdmin):
    """What each token spent of its budgets, per UTC day."""

    list_display = ("day", "actor", "requests", "ai_calls", "posts_scheduled")
    search_fields = ("actor", "api_key__name", "api_key__lookup_prefix")
    date_hierarchy = "day"
    readonly_fields = tuple(f.name for f in ApiKeyDailyUsage._meta.fields)


@admin.register(ConnectedApp)
class ConnectedAppAdmin(admin.ModelAdmin):
    """OAuth clients people connected, with the limits set on them. Revoke from the API keys page, which also
    revokes the client's tokens."""

    list_display = ("application", "user", "requires_approval", "expires_at", "revoked_at", "last_used_at")
    search_fields = ("user__email", "application__name")
    readonly_fields = ("user", "application", "created_at", "updated_at", "last_used_at")
