from django.apps import AppConfig


class CalendarConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.calendar"
    verbose_name = "Content Calendar"

    def ready(self):
        from django.db.models.signals import post_migrate

        post_migrate.connect(self._register_tasks, sender=self)

    @staticmethod
    def _register_tasks(sender, **kwargs):
        from apps.calendar.tasks import (
            EVERGREEN_FILL_INTERVAL_SECONDS,
            RECURRENCE_GENERATION_INTERVAL_SECONDS,
            run_evergreen_fill,
            run_recurring_post_generation,
        )
        from apps.common.background import register_recurring_task

        register_recurring_task(
            run_recurring_post_generation,
            repeat=RECURRENCE_GENERATION_INTERVAL_SECONDS,
            verbose_name="generate_recurring_posts",
        )
        register_recurring_task(
            run_evergreen_fill,
            repeat=EVERGREEN_FILL_INTERVAL_SECONDS,
            verbose_name="fill_queues_from_evergreen",
        )
