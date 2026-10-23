"""Background tasks for the autonomy trail."""

import logging

from background_task import background
from django.utils import timezone

logger = logging.getLogger(__name__)

# How often autonomous replies waiting out their undo window are checked.
REPLY_RELEASE_INTERVAL_SECONDS = 60


@background(schedule=0)
def release_due_replies():
    """Background entry point for :func:`send_due_replies`."""
    sent = send_due_replies()
    if sent:
        logger.info("Autonomy: sent %d reply(ies) whose undo window ended.", sent)


def send_due_replies(*, now=None) -> int:
    """Send autonomous replies whose undo window has ended and that nobody undid, held or paused."""
    from apps.inbox.models import InboxReply
    from apps.inbox.services import ReplyStateError, send_reply_now

    from .release import _live_autonomous, reply_is_held

    now = now or timezone.now()
    due = (
        _live_autonomous()
        .filter(reply__status=InboxReply.Status.DRAFT, release_at__lte=now)
        .select_related("reply__inbox_message__social_account")
    )
    sent = 0
    for action in due:
        reply = action.reply
        if reply is None or reply_is_held(reply, now):
            continue
        try:
            send_reply_now(reply, actor=None)
            sent += 1
        except ReplyStateError:
            continue
        except Exception:
            # The reply is left in "failed" with its reason; a person can retry it from the inbox.
            logger.warning("Autonomous reply %s was not sent", reply.pk)
    return sent
