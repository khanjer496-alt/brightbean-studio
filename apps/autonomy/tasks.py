"""Background tasks for the autonomy trail."""

import logging

from background_task import background
from django.utils import timezone

logger = logging.getLogger(__name__)

# How often autonomous replies waiting out their undo window are checked.
REPLY_RELEASE_INTERVAL_SECONDS = 60

# How often entries older than ``settings.AUTONOMY_AUDIT_RETENTION_DAYS`` are pruned.
PRUNE_INTERVAL_SECONDS = 24 * 3600
_PRUNE_CHUNK = 1000


@background(schedule=0)
def release_due_replies():
    """Background entry point for :func:`send_due_replies`."""
    sent = send_due_replies()
    if sent:
        logger.info("Autonomy: sent %d reply(ies) whose undo window ended.", sent)


def send_due_replies(*, now=None) -> int:
    """Send autonomous replies whose undo window has ended and that nobody undid, held or paused."""
    from django.db import transaction

    from apps.inbox.models import InboxReply
    from apps.inbox.services import ReplyStateError, send_reply_now

    from .release import _live_autonomous, _waiting

    now = now or timezone.now()
    due = list(
        _live_autonomous()
        .filter(reply__status=InboxReply.Status.DRAFT, release_at__lte=now)
        .values_list("pk", "reply_id")
    )
    sent = 0
    for action_pk, reply_pk in due:
        with transaction.atomic():
            # Lock the reply first (undo, hold and edits lock it too), then re-check the
            # decision: undone, held, superseded by an edit or paused since the query above
            # means it stays a draft.
            reply = (
                InboxReply.objects.select_for_update(skip_locked=True)
                .filter(pk=reply_pk, status=InboxReply.Status.DRAFT)
                .select_related("inbox_message__social_account")
                .first()
            )
            if reply is None:
                continue
            check = now
            if not _live_autonomous().filter(pk=action_pk).exclude(_waiting(check)).exists():
                continue
            if _live_autonomous().filter(reply_id=reply_pk).filter(_waiting(check)).exists():
                continue
            try:
                send_reply_now(reply, actor=None)
                sent += 1
            except ReplyStateError:
                continue
            except Exception:
                # Not re-raised, so the "failed" status and its reason commit; a person can
                # retry it from the inbox and the sweep doesn't try it again.
                logger.warning("Autonomous reply %s was not sent", reply_pk)
    return sent


@background(schedule=0)
def prune_agent_actions():
    """Delete audit entries older than the retention window.

    Entries something still depends on stay: a hold nobody has reviewed, or a
    send still waiting out its undo window or a person's hold.
    """
    import datetime as dt

    from django.conf import settings
    from django.db.models import Q

    from .models import AgentAction, Decision

    days = settings.AUTONOMY_AUDIT_RETENTION_DAYS
    cutoff = timezone.now() - dt.timedelta(days=days)
    stale = AgentAction.objects.filter(created_at__lt=cutoff).exclude(
        Q(decision=Decision.HELD, review_outcome="")
        | Q(decision=Decision.EXECUTED, undone_at__isnull=True, held_at__isnull=False, hold_released_at__isnull=True)
    )
    deleted = 0
    try:
        while pks := list(stale.values_list("pk", flat=True)[:_PRUNE_CHUNK]):
            count, _ = AgentAction.objects.filter(pk__in=pks).delete()
            deleted += count
    except Exception:
        logger.exception("Agent action prune failed")
        return
    if deleted:
        logger.info("Pruned %d agent action(s) older than %d days", deleted, days)
