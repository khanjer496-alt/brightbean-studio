"""Status-change hooks for the events whose writers are too many to instrument.

A post is scheduled or published from the composer, the calendar, the API, the
MCP tools and the publish engine, and an account's status is changed by the
health check, the sync jobs and the reconnect flow. All of them go through
``Model.save()``, so the change is caught here once. The previous status is
read in ``pre_save`` (a queryset ``.update()`` never reaches these hooks, which
is also why the failure and partial-thread events are emitted from the engine).
"""

from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from apps.composer.models import PlatformPost
from apps.social_accounts.models import SocialAccount

from . import events
from .models import EventType

_PREVIOUS_STATUS = "_webhooks_previous_status"
_LOST_CONNECTION = (SocialAccount.ConnectionStatus.DISCONNECTED, SocialAccount.ConnectionStatus.ERROR)


def _touches(instance, update_fields, field: str) -> bool:
    return not instance._state.adding and (update_fields is None or field in update_fields)


@receiver(pre_save, sender=PlatformPost)
def remember_platform_post_status(sender, instance, update_fields=None, **kwargs):
    previous = None
    if _touches(instance, update_fields, "status") and instance.status in (
        PlatformPost.Status.SCHEDULED,
        PlatformPost.Status.PUBLISHED,
    ):
        previous = sender.objects.filter(pk=instance.pk).values_list("status", flat=True).first()
    setattr(instance, _PREVIOUS_STATUS, previous)


@receiver(post_save, sender=PlatformPost)
def emit_platform_post_status(sender, instance, created, **kwargs):
    previous = getattr(instance, _PREVIOUS_STATUS, None)
    if not created and previous is None:
        return
    if previous == instance.status:
        return
    if instance.status == PlatformPost.Status.PUBLISHED:
        events.post_event(EventType.POST_PUBLISHED, instance)
    elif instance.status == PlatformPost.Status.SCHEDULED and previous != PlatformPost.Status.PUBLISHING:
        # Publishing -> scheduled is the engine queueing a retry, not a person scheduling.
        events.post_event(EventType.POST_SCHEDULED, instance)


@receiver(pre_save, sender=SocialAccount)
def remember_account_status(sender, instance, update_fields=None, **kwargs):
    previous = None
    if _touches(instance, update_fields, "connection_status") and instance.connection_status in _LOST_CONNECTION:
        previous = sender.objects.filter(pk=instance.pk).values_list("connection_status", flat=True).first()
    setattr(instance, _PREVIOUS_STATUS, previous)


@receiver(post_save, sender=SocialAccount)
def emit_account_disconnected(sender, instance, created, **kwargs):
    previous = getattr(instance, _PREVIOUS_STATUS, None)
    if created or previous is None or previous in _LOST_CONNECTION:
        return
    events.account_disconnected(instance, reason=instance.last_error)
