from django.apps import AppConfig


class PlugsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.plugs"
    verbose_name = "Engagement automations"

    def ready(self):
        from django.db.models.signals import post_migrate

        post_migrate.connect(self._register_tasks, sender=self)

    @staticmethod
    def _register_tasks(sender, **kwargs):
        from apps.common.background import register_recurring_task
        from apps.plugs.tasks import (
            CHECK_INTERVAL_SECONDS,
            RELEASE_INTERVAL_SECONDS,
            check_plug_rules,
            release_due_plugs,
        )

        register_recurring_task(check_plug_rules, repeat=CHECK_INTERVAL_SECONDS, verbose_name="check_plug_rules")
        register_recurring_task(release_due_plugs, repeat=RELEASE_INTERVAL_SECONDS, verbose_name="release_due_plugs")
