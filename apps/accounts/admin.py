from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.shortcuts import redirect

from .models import OAuthConnection, Session, User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ("email", "name", "is_active", "is_staff", "created_at")
    list_filter = ("is_active", "is_staff", "totp_enabled")
    search_fields = ("email", "name")
    ordering = ("-created_at",)
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Personal info", {"fields": ("name", "avatar")}),
        ("Permissions", {"fields": ("is_active", "is_staff", "is_superuser")}),
        ("2FA", {"fields": ("totp_enabled",)}),
    )
    add_fieldsets = ((None, {"classes": ("wide",), "fields": ("email", "password1", "password2")}),)
    actions = ["view_as_user"]

    @admin.action(description="View as this user (support)", permissions=["impersonate"])
    def view_as_user(self, request, queryset):
        if queryset.count() != 1:
            self.message_user(request, "Pick exactly one user to view as.", messages.ERROR)
            return None
        return redirect("support:impersonate", user_id=queryset.get().pk)

    def has_impersonate_permission(self, request):
        return request.user.is_superuser


@admin.register(OAuthConnection)
class OAuthConnectionAdmin(admin.ModelAdmin):
    list_display = ("user", "provider", "provider_email", "created_at")
    list_filter = ("provider",)


@admin.register(Session)
class SessionAdmin(admin.ModelAdmin):
    list_display = ("user", "device_info", "ip_address", "last_active_at", "expires_at")
    list_filter = ("created_at",)
