from django.apps import AppConfig


class WebhooksConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.webhooks"
    verbose_name = "Outbound Webhooks"

    def ready(self):
        from django.db.models.signals import post_migrate

        # Status-change hooks for PlatformPost and SocialAccount rows.
        from apps.webhooks import signals  # noqa: F401

        post_migrate.connect(self._register_tasks, sender=self)

    @staticmethod
    def _register_tasks(sender, **kwargs):
        from apps.common.background import register_recurring_task
        from apps.webhooks.tasks import (
            PRUNE_INTERVAL_SECONDS,
            RETRY_SWEEP_INTERVAL_SECONDS,
            prune_webhook_deliveries,
            retry_due_webhook_deliveries,
        )

        register_recurring_task(
            retry_due_webhook_deliveries,
            repeat=RETRY_SWEEP_INTERVAL_SECONDS,
            verbose_name="retry_due_webhook_deliveries",
        )
        register_recurring_task(
            prune_webhook_deliveries,
            repeat=PRUNE_INTERVAL_SECONDS,
            verbose_name="prune_webhook_deliveries",
        )
