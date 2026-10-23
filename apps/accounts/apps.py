import logging

from django.apps import AppConfig

logger = logging.getLogger(__name__)


class AccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.accounts"
    verbose_name = "Accounts"

    def ready(self):
        from django.db.models.signals import post_migrate

        import apps.accounts.signals  # noqa: F401

        post_migrate.connect(self._register_tasks, sender=self)
        post_migrate.connect(self._sync_site_identity, sender=self)
        self._warn_if_rate_limits_are_process_local()

    @staticmethod
    def _warn_if_rate_limits_are_process_local():
        """Say so at startup when rate limits cannot hold across workers (see production.py)."""
        from django.conf import settings

        if getattr(settings, "RATE_LIMIT_CACHE_IS_PROCESS_LOCAL", False):
            logger.warning(
                "REDIS_URL is not set: rate limits (login, signup, password reset, Agent API) are "
                "counted per process, so each worker allows the full limit and restarts reset it. "
                "Set REDIS_URL to share them."
            )

    @staticmethod
    def _register_tasks(sender, **kwargs):
        from apps.accounts.tasks import SESSION_CLEANUP_INTERVAL_SECONDS, clear_expired_sessions
        from apps.common.background import register_recurring_task

        register_recurring_task(
            clear_expired_sessions,
            repeat=SESSION_CLEANUP_INTERVAL_SECONDS,
            verbose_name="clear_expired_sessions",
        )

    @staticmethod
    def _sync_site_identity(sender, **kwargs):
        """Keep django.contrib.sites aligned with the deployment identity.

        Upstream ships a historical data migration for studio.brightbean.xyz.
        Deployment branding is runtime configuration, so migrations cannot
        safely hard-code PostDelegate.  The migrate container runs on every
        deploy; syncing here keeps auth/OAuth helpers from ever depending on
        the upstream Site row.
        """
        from django.conf import settings
        from django.contrib.sites.models import Site

        Site.objects.update_or_create(
            id=settings.SITE_ID,
            defaults={"domain": settings.SITE_DOMAIN, "name": settings.SITE_NAME},
        )
