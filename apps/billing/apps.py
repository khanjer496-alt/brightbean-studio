from django.apps import AppConfig


class BillingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.billing"
    verbose_name = "Plans & billing"

    def ready(self):
        # The scheduled-post allowance is enforced on the model as well as in
        # the views, so a scheduling path added later can't skip it.
        from apps.billing import signals  # noqa: F401
