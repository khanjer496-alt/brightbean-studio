from django.apps import AppConfig


class AutopostConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.autopost"
    verbose_name = "Feed autoposting"

    def ready(self):
        from django.db.models.signals import post_migrate

        post_migrate.connect(self._register_tasks, sender=self)

    @staticmethod
    def _register_tasks(sender, **kwargs):
        from apps.autopost.tasks import POLL_INTERVAL_SECONDS, poll_feeds
        from apps.common.background import register_recurring_task

        register_recurring_task(poll_feeds, repeat=POLL_INTERVAL_SECONDS, verbose_name="poll_feed_autoposts")
