import logging

from django.apps import AppConfig

logger = logging.getLogger(__name__)

SYNC_INTERVAL_SECONDS = 3600  # hourly


class AnalyticsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.analytics"
    verbose_name = "Analytics"

    def ready(self):
        from django.db.models.signals import post_migrate

        from . import signals  # noqa: F401

        # Register the recurring sync task with django-background-tasks. We
        # use ``post_migrate`` so the task table exists before we touch it;
        # mirrors the pattern in apps/publisher/apps.py.
        post_migrate.connect(self._register_sync_task, sender=self)

    @staticmethod
    def _register_sync_task(sender, **kwargs):
        """Idempotently register the hourly analytics crons (sync, engagement alerts)."""
        try:
            from background_task.models import Task

            from apps.analytics.tasks import check_engagement_alerts, sync_all_account_analytics

            for name, task in (
                ("sync_all_account_analytics", sync_all_account_analytics),
                ("check_engagement_alerts", check_engagement_alerts),
            ):
                if not Task.objects.filter(verbose_name=name).exists():
                    task(repeat=SYNC_INTERVAL_SECONDS, verbose_name=name)
                    logger.info("Registered recurring %s (every %ss)", name, SYNC_INTERVAL_SECONDS)
        except Exception:
            logger.debug("Skipping analytics cron registration (database not ready)")
