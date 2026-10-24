"""Recording that an account's credentials are dead, and telling the people who can fix it.

Three places learn this at different moments - the health check, the token
refresh before a publish, and a platform answering a publish with "this token is
revoked" - and all of them used to either say nothing or let the post burn its
retries first. They share one write so the account card, the webhook event and
the notification can never disagree about when it happened.
"""

import logging

from django.utils import timezone

from apps.webhooks import events as webhook_events

from .error_messages import RECONNECT_MESSAGE
from .models import SocialAccount

logger = logging.getLogger(__name__)

_LIVE = (SocialAccount.ConnectionStatus.CONNECTED, SocialAccount.ConnectionStatus.TOKEN_EXPIRING)


def mark_reconnect_required(account: SocialAccount, message: str = RECONNECT_MESSAGE) -> bool:
    """Flag ``account`` as needing a reconnect. Returns True when this call is what changed it.

    The conditional UPDATE is the claim: of several publishes failing on the same
    dead token at once, one flags the account and notifies, and the rest find it
    already flagged. ERROR is the status the rest of the app already treats as
    "reconnect me" (the sidebar, the composer's refusal to schedule).
    """
    changed = SocialAccount.objects.filter(pk=account.pk, connection_status__in=_LIVE).update(
        connection_status=SocialAccount.ConnectionStatus.ERROR,
        last_error=message,
        updated_at=timezone.now(),
    )
    account.connection_status = SocialAccount.ConnectionStatus.ERROR
    account.last_error = message
    if not changed:
        return False
    # A queryset update reaches no save() hook, so the event is sent here.
    webhook_events.account_disconnected(account, reason=message)
    _notify_managers(account, message)
    return True


def mark_token_expiring(account: SocialAccount, message: str) -> None:
    """Record that the stored token can no longer be renewed, while it still works.

    Only moves an account that is CONNECTED: one already flagged stays as it is.
    """
    SocialAccount.objects.filter(pk=account.pk, connection_status=SocialAccount.ConnectionStatus.CONNECTED).update(
        connection_status=SocialAccount.ConnectionStatus.TOKEN_EXPIRING,
        last_error=message,
        updated_at=timezone.now(),
    )
    if account.connection_status == SocialAccount.ConnectionStatus.CONNECTED:
        account.connection_status = SocialAccount.ConnectionStatus.TOKEN_EXPIRING
        account.last_error = message


def warn_refresh_token_expiring(account: SocialAccount) -> None:
    """Tell the account's managers, once, that it must be reconnected before its refresh token lapses.

    The access token keeps being renewed until then, so nothing is failing yet and the account stays
    usable. The notification goes out on the sweep that first moves it to TOKEN_EXPIRING; later sweeps just
    keep the card's message current. Sets the status and message on ``account`` for the caller to save.
    """
    expires_at = account.refresh_token_expires_at
    if expires_at is None or account.connection_status not in _LIVE:
        return
    message = f"Reconnect before {expires_at:%d %b %Y}: this connection can no longer be renewed after that."
    claimed = SocialAccount.objects.filter(
        pk=account.pk, connection_status=SocialAccount.ConnectionStatus.CONNECTED
    ).update(
        connection_status=SocialAccount.ConnectionStatus.TOKEN_EXPIRING,
        last_error=message,
        updated_at=timezone.now(),
    )
    account.connection_status = SocialAccount.ConnectionStatus.TOKEN_EXPIRING
    account.last_error = message
    if claimed:
        _notify_managers(account, message, title="needs to be reconnected soon")


def _notify_managers(account: SocialAccount, message: str, *, title: str = "needs to be reconnected") -> None:
    """Tell everyone who can reconnect the account. Best-effort: the flag is already set."""
    try:
        from apps.members.models import WorkspaceMembership
        from apps.notifications.engine import notify
        from apps.notifications.models import EventType

        memberships = WorkspaceMembership.objects.filter(workspace_id=account.workspace_id).select_related(
            "user", "custom_role"
        )
        for membership in memberships:
            if not membership.effective_permissions.get("manage_social_accounts", False):
                continue
            notify(
                membership.user,
                EventType.SOCIAL_ACCOUNT_DISCONNECTED,
                title=f"{account.get_platform_display()} account {title}",
                body=f"{account.account_name}: {message}",
                data={
                    "social_account_id": str(account.id),
                    "workspace_id": str(account.workspace_id),
                    "platform": account.platform,
                },
            )
    except Exception:
        logger.exception("Could not notify the managers that account %s needs to be reconnected", account.id)
