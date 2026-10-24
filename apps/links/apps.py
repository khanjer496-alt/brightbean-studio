from django.apps import AppConfig


class LinksConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.links"
    verbose_name = "Short links and UTM"

    def ready(self):
        from . import signals  # noqa: F401
