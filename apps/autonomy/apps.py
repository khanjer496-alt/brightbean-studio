from django.apps import AppConfig


class AutonomyConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.autonomy"
    verbose_name = "Autonomy"

    def ready(self):
        from django.db.models.signals import post_migrate

        post_migrate.connect(self._register_tasks, sender=self)

    @staticmethod
    def _register_tasks(sender, **kwargs):
        from apps.autonomy.tasks import (
            PRUNE_INTERVAL_SECONDS,
            REPLY_RELEASE_INTERVAL_SECONDS,
            prune_agent_actions,
            release_due_replies,
        )
        from apps.common.background import register_recurring_task

        register_recurring_task(
            release_due_replies,
            repeat=REPLY_RELEASE_INTERVAL_SECONDS,
            verbose_name="release_due_autonomous_replies",
        )
        register_recurring_task(
            prune_agent_actions,
            repeat=PRUNE_INTERVAL_SECONDS,
            verbose_name="prune_agent_actions",
        )
