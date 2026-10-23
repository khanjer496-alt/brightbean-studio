import logging

from django.apps import AppConfig

logger = logging.getLogger(__name__)

SYNC_INTERVAL_SECONDS = 3600  # hourly
BEST_TIMES_INTERVAL_SECONDS = 86400  # daily


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
        """Idempotently register the analytics crons (hourly sync and engagement alerts, daily best times)."""
        try:
            from background_task.models import Task

            from apps.analytics.tasks import check_engagement_alerts, refresh_best_times, sync_all_account_analytics

            for name, task, interval in (
                ("sync_all_account_analytics", sync_all_account_analytics, SYNC_INTERVAL_SECONDS),
                ("check_engagement_alerts", check_engagement_alerts, SYNC_INTERVAL_SECONDS),
                ("refresh_best_times", refresh_best_times, BEST_TIMES_INTERVAL_SECONDS),
            ):
                if not Task.objects.filter(verbose_name=name).exists():
                    task(repeat=interval, verbose_name=name)
                    logger.info("Registered recurring %s (every %ss)", name, interval)
        except Exception:
            logger.debug("Skipping analytics cron registration (database not ready)")
