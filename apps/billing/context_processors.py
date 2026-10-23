"""Template context for plans & billing."""

from django.conf import settings


def billing_flag(request):
    """Add ``BILLING_ENABLED`` so templates can show or hide the billing surface."""
    return {"BILLING_ENABLED": getattr(settings, "BILLING_ENABLED", False)}
