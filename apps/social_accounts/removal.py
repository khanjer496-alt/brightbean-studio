"""Removing connected accounts when a person or a platform asks for their data to go."""

import logging

from django.db import transaction
from django.db.models import Count

from .models import SocialAccount
from .webhooks import unsubscribe_account_webhooks

logger = logging.getLogger(__name__)


def revoke_grant(account):
    """Ask the platform to end the grant behind ``account``, where it supports that.

    Best-effort: a platform that is down or has already dropped the grant must
    not stop the deletion that follows.
    """
    try:
        from apps.publisher.engine import _resolve_publish_credentials
        from providers import get_provider

        provider = get_provider(account.platform, _resolve_publish_credentials(account))
        if account.revocation_token:
            provider.revoke_token(account.revocation_token)
    except Exception:
        logger.warning("Failed to revoke the grant for %s, deleting it anyway", account.pk)


def remove_account(account):
    """Revoke the grant, then delete the account and the posts that only targeted it."""
    if account.platform == "youtube":
        # Purging queues the revocation and removes every copy of API data.
        from .youtube_compliance import purge_connection

        purge_connection(account)
        return

    if account.oauth_access_token:
        unsubscribe_account_webhooks(account)
    revoke_grant(account)

    from apps.composer.models import PlatformPost, Post

    orphan_post_ids = list(
        PlatformPost.objects.filter(social_account=account)
        .values("post_id")
        .annotate(total_platforms=Count("post__platform_posts"))
        .filter(total_platforms=1)
        .values_list("post_id", flat=True)
    )
    with transaction.atomic():
        Post.objects.filter(id__in=orphan_post_ids).delete()
        account.delete()


def remove_accounts_in_organization(organization):
    """Remove every connected account of ``organization`` before the organization itself goes."""
    for account in SocialAccount.objects.filter(workspace__organization=organization).select_related("workspace"):
        remove_account(account)
