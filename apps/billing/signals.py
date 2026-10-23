"""Backstop for the monthly scheduled-post allowance.

The composer, the Agent API and MCP check the allowance up front so they can
answer with a proper message. This guard catches every other path that moves
a ``PlatformPost`` into a counted status — calendar drag-and-drop, queue
slots, CSV import, anything added later — at the moment it is saved.

Only the move *into* a counted status is checked: a post that already counts
(the publisher retrying it, moving it between ``publishing`` and
``scheduled``) is never stopped, so this can't strand a post mid-publish.
"""

from django.db.models.signals import post_init, post_save, pre_save
from django.dispatch import receiver

from apps.composer.models import PlatformPost

from .entitlements import COUNTED_POST_STATUSES, billing_enabled, check_can_schedule_posts


@receiver(post_init, sender=PlatformPost)
def _remember_loaded_status(sender, instance, **kwargs):
    # __dict__ rather than the attribute: a deferred ``status`` must not cost
    # a query on every row loaded.
    instance._billing_saved_status = instance.__dict__.get("status")


@receiver(post_save, sender=PlatformPost)
def _remember_saved_status(sender, instance, **kwargs):
    instance._billing_saved_status = instance.status


@receiver(pre_save, sender=PlatformPost)
def enforce_scheduled_post_allowance(sender, instance, raw=False, **kwargs):
    if raw or not billing_enabled() or instance.status not in COUNTED_POST_STATUSES:
        return
    previous = None if instance._state.adding else getattr(instance, "_billing_saved_status", None)
    if previous in COUNTED_POST_STATUSES:
        return
    check_can_schedule_posts(instance.post.workspace.organization, count=1, when=instance.scheduled_at)
