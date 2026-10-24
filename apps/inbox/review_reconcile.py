"""Finds reviews deleted on the platform and archives them here.

The routine poll only ever sees reviews that exist, so a review the author
deleted (or the platform removed) would otherwise stay in the inbox, counted and
awaiting a reply, forever. Once a day per location this lists the platform's
review ids (cheap: ids only) and compares them with the reviews the inbox holds.
A review that is gone is archived and stamped ``removed_from_platform_at``; it
is kept, never deleted, so history and replies survive, and it drops out of
"needs reply" and the rating analytics. If the platform lists it again, the
sync (when it fetches the review) or the next reconcile restores it.

Quotas: locations are checked at most once a day and a few per run, a spent
quota stops the run through the shared circuit breaker, and a rate limit holds
just that location back with jittered exponential backoff.
"""

from __future__ import annotations

import logging
import random
from datetime import timedelta

from django.utils import timezone

from apps.common import quota
from apps.social_accounts.models import SocialAccount
from providers import PROVIDER_REGISTRY, get_provider
from providers.exceptions import ProviderError, QuotaExceededError, RateLimitError

from .models import InboxMessage, ReviewReconcileState

logger = logging.getLogger(__name__)

RECONCILE_INTERVAL = timedelta(hours=23)
# Locations checked per run, so a deployment with many does not spend a burst of quota at once.
MAX_LOCATIONS_PER_RUN = 25
BACKOFF_BASE = timedelta(minutes=15)
BACKOFF_MAX = timedelta(hours=12)


def backoff_delay(consecutive_rate_limits: int, retry_after: int | None = None) -> timedelta:
    """How long to hold a location back after its ``consecutive_rate_limits``-th rate limit in a row.

    Doubles each time up to ``BACKOFF_MAX``, then takes a random point in the
    upper half of that, so locations limited together do not all come back
    together. Never earlier than the platform's own ``retry_after``.
    """
    ceiling = min(BACKOFF_BASE * 2 ** max(consecutive_rate_limits - 1, 0), BACKOFF_MAX)
    delay = timedelta(seconds=random.uniform(ceiling.total_seconds() / 2, ceiling.total_seconds()))
    return max(delay, timedelta(seconds=retry_after or 0))


def _due_accounts():
    now = timezone.now()
    accounts = SocialAccount.objects.filter(
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        platform__in=[name for name, cls in PROVIDER_REGISTRY.items() if getattr(cls, "supports_reviews", False)],
    ).select_related("review_reconcile_state")
    due = []
    for account in accounts:
        state = getattr(account, "review_reconcile_state", None)
        if state and state.backoff_until and state.backoff_until > now:
            continue
        if state and state.last_reconciled_at and state.last_reconciled_at > now - RECONCILE_INTERVAL:
            continue
        due.append((state.last_reconciled_at if state and state.last_reconciled_at else None, account))
    # Never-checked locations first, then the longest-waiting.
    due.sort(key=lambda item: (item[0] is not None, item[0] or now))
    return [account for _, account in due][:MAX_LOCATIONS_PER_RUN]


def reconcile_all() -> None:
    quota_cache: dict = {}
    for account in _due_accounts():
        try:
            # A spent quota trips the shared breaker, which the remaining locations then respect.
            reconcile_account(account, quota_cache=quota_cache)
        except Exception:
            logger.exception("Review reconcile failed for account %s", account.id)


def reconcile_account(account, *, quota_cache: dict | None = None) -> int | bool | None:
    """Archive the reviews of ``account`` the platform no longer lists.

    Returns how many were archived, ``None`` when the check was skipped or
    deferred, and ``False`` when the platform's quota is spent.
    """
    from apps.publisher.engine import _resolve_publish_credentials

    provider = get_provider(account.platform, _resolve_publish_credentials(account))
    credential = quota.credential_key(getattr(provider, "credentials", None))
    scope = quota.read_scope(account.platform)
    if quota.quota_blocked_until(account.platform, credential, scope, cache=quota_cache):
        return False

    state, _ = ReviewReconcileState.objects.get_or_create(social_account=account)
    started_at = timezone.now()
    try:
        listed = provider.list_review_ids(account.oauth_access_token)
    except QuotaExceededError as exc:
        quota.trip_from_exception(account.platform, credential, exc, cache=quota_cache)
        return False
    except RateLimitError as exc:
        state.consecutive_rate_limits += 1
        delay = backoff_delay(state.consecutive_rate_limits, getattr(exc, "retry_after", None))
        state.backoff_until = started_at + delay
        state.save(update_fields=["consecutive_rate_limits", "backoff_until"])
        logger.warning(
            "Review reconcile rate-limited for account %s; backing off %d minutes (attempt %d)",
            account.id,
            delay.total_seconds() // 60,
            state.consecutive_rate_limits,
        )
        return None
    except (ProviderError, NotImplementedError) as exc:
        logger.warning("Review reconcile could not list reviews for account %s: %s", account.id, type(exc).__name__)
        return None

    state.consecutive_rate_limits = 0
    state.backoff_until = None
    if listed is None:
        logger.warning("Review reconcile skipped for account %s: the review list was incomplete", account.id)
        state.save(update_fields=["consecutive_rate_limits", "backoff_until"])
        return None

    restored = _restore_listed_again(account, listed)
    # Reviews that arrived while the list was being read are not in it; only
    # those the inbox already held when it started can be judged gone.
    candidates = [
        message
        for message in InboxMessage.objects.filter(
            social_account=account, message_type=InboxMessage.MessageType.REVIEW, created_at__lt=started_at
        )
        if not message.removed_from_platform
    ]
    missing = [message for message in candidates if message.platform_message_id not in listed]
    if not listed and candidates:
        # An empty list with reviews on file looks more like a failed read than a wiped location.
        logger.warning("Review reconcile skipped for account %s: the platform listed no reviews", account.id)
        missing = []
    for message in missing:
        message.extra = {
            **(message.extra or {}),
            "removed_from_platform_at": started_at.isoformat(),
            "removed_previous_status": message.status,
        }
        message.status = InboxMessage.Status.ARCHIVED
        message.save(update_fields=["extra", "status"])
    state.last_reconciled_at = started_at
    state.save(update_fields=["last_reconciled_at", "consecutive_rate_limits", "backoff_until"])
    if missing or restored:
        logger.info(
            "Review reconcile for account %s: archived %d deleted on the platform, restored %d listed again",
            account.id,
            len(missing),
            restored,
        )
    return len(missing)


def _restore_listed_again(account, listed: set[str]) -> int:
    """Undo the archiving of removed reviews the platform lists again; returns how many.

    The routine poll only fetches reviews updated since it last looked, so a
    review the platform reinstates unchanged would otherwise stay archived.
    """
    restored = 0
    removed = InboxMessage.objects.filter(
        social_account=account,
        message_type=InboxMessage.MessageType.REVIEW,
        extra__has_key="removed_from_platform_at",
    )
    for message in removed:
        if message.platform_message_id not in listed:
            continue
        extra = dict(message.extra or {})
        extra.pop("removed_from_platform_at", None)
        previous = extra.pop("removed_previous_status", None)
        message.extra = extra
        if previous and message.status == InboxMessage.Status.ARCHIVED:
            message.status = previous
        message.save(update_fields=["extra", "status"])
        restored += 1
    return restored
