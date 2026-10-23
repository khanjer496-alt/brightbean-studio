from django.apps import AppConfig


class ReportsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.reports"
    verbose_name = "Client reports"

    def ready(self):
        from django.db.models.signals import post_migrate

        post_migrate.connect(self._register_tasks, sender=self)

    @staticmethod
    def _register_tasks(sender, **kwargs):
        from apps.common.background import register_recurring_task

        from .tasks import SWEEP_INTERVAL_SECONDS, run_due_reports

        register_recurring_task(run_due_reports, repeat=SWEEP_INTERVAL_SECONDS, verbose_name="run_due_reports")
