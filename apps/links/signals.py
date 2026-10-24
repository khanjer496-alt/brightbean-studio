"""Tag and shorten a post's links when an account's copy is submitted for review or scheduled.

Two hooks, both cheap when nothing applies (a post with no link and a workspace that never set links up costs
one or two small queries):

* a copy reaching ``pending_review`` or ``scheduled`` (or its own text changing there) is prepared, so the
  review screens, the approval fingerprint and the publisher all see the final links;
* the post's own text changing while a copy is in one of those states re-prepares those copies, so an edit
  made after scheduling is published with its links tagged and shortened too.

Preparation writes with ``QuerySet.update`` and never re-enters these hooks. It runs inside the caller's
transaction on purpose: a post scheduled for "now" must not reach the publisher before its links are rewritten.
"""

from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.composer.models import PlatformPost, Post

# A save that names its fields and none of these can't have moved the copy to review/scheduled or changed its text.
_RELEVANT_FIELDS = {"status", "platform_specific_caption", "platform_specific_first_comment"}
# The post's own fields that change its links or their UTM tags (the campaign can come from title, tags, category).
_POST_FIELDS = {"caption", "first_comment", "title", "tags", "category", "shorten_links"}


@receiver(post_save, sender=PlatformPost, dispatch_uid="links.prepare_on_schedule")
def prepare_links_on_schedule(sender, instance, raw=False, update_fields=None, **kwargs):
    from . import services

    if raw or instance.status not in services.PREPARE_STATUSES:
        return
    if update_fields is not None and not _RELEVANT_FIELDS & set(update_fields):
        return
    services.prepare_platform_post(instance)


@receiver(post_save, sender=Post, dispatch_uid="links.prepare_on_post_edit")
def prepare_links_on_post_edit(sender, instance, raw=False, created=False, update_fields=None, **kwargs):
    if raw or created:
        return
    if update_fields is not None and not _POST_FIELDS & set(update_fields):
        return
    from . import services

    services.prepare_post(instance)
