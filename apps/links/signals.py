"""Tag and shorten a post's links the moment an account's copy is scheduled."""

from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.composer.models import PlatformPost

# A save that names its fields and none of these can't have scheduled the post or changed its text.
_RELEVANT_FIELDS = {"status", "platform_specific_caption", "platform_specific_first_comment"}


@receiver(post_save, sender=PlatformPost, dispatch_uid="links.prepare_on_schedule")
def prepare_links_on_schedule(sender, instance, raw=False, update_fields=None, **kwargs):
    if raw or instance.status != PlatformPost.Status.SCHEDULED:
        return
    if update_fields is not None and not _RELEVANT_FIELDS & set(update_fields):
        return
    from . import services

    services.prepare_platform_post(instance)
