from django.contrib.auth.signals import user_logged_out
from django.core.cache import cache
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import Announcement, ImpersonationSession
from .services import ANNOUNCEMENTS_PENDING_KEY, end_impersonation


@receiver(user_logged_out)
def end_impersonation_on_logout(sender, request, **kwargs):
    """Signing out of a "view as" session closes its audit row before the session is flushed."""
    if request is not None and hasattr(request, "session"):
        end_impersonation(request, ImpersonationSession.EndReason.LOGOUT)


@receiver([post_save, post_delete], sender=Announcement)
def forget_pending_announcements(sender, **kwargs):
    cache.delete(ANNOUNCEMENTS_PENDING_KEY)
