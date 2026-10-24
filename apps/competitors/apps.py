from django.apps import AppConfig


class CompetitorsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.competitors"
    verbose_name = "Competitor benchmarking"

    def ready(self):
        from django.db.models.signals import post_migrate

        post_migrate.connect(self._register_tasks, sender=self)

    @staticmethod
    def _register_tasks(sender, **kwargs):
        from apps.common.background import register_recurring_task

        from .tasks import SNAPSHOT_INTERVAL_SECONDS, snapshot_competitors

        register_recurring_task(
            snapshot_competitors, repeat=SNAPSHOT_INTERVAL_SECONDS, verbose_name="snapshot_competitors"
        )
