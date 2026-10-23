from django.contrib import admin

from .models import ProcessedStripeEvent, Subscription


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ("organization", "plan", "interval", "status", "trial_ends_at", "grace_until", "updated_at")
    list_filter = ("plan", "status", "interval")
    search_fields = ("organization__name", "stripe_customer_id", "stripe_subscription_id")
    readonly_fields = ("id", "stripe_synced_at", "created_at", "updated_at")


@admin.register(ProcessedStripeEvent)
class ProcessedStripeEventAdmin(admin.ModelAdmin):
    list_display = ("event_id", "event_type", "processed_at")
    list_filter = ("event_type",)
    search_fields = ("event_id",)
    readonly_fields = ("event_id", "event_type", "processed_at")
