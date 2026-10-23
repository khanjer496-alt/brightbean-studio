"""Service layer for the Unified Social Inbox (F-3.1).

Both the HTMX views and the programmatic surfaces (the ``/api/v1/inbox``
REST router and the MCP inbox tools) go through these functions so the
three can't drift — the same rule the composer follows with
``apps.composer.services``.

A reply now has a lifecycle: it is created as a ``draft``, then a separate
send step delivers it to the platform and moves it to ``sent`` (or
``failed`` if the platform refused it). The platform-dispatch logic used
to live in ``apps/inbox/views.py``; it moved here verbatim.
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from apps.members.models import WorkspaceMembership
from apps.notifications.engine import notify
from apps.notifications.models import EventType
from providers import get_provider
from providers.exceptions import MessagingError
from providers.meta_messaging import META_DM_PLATFORMS

from .models import InboxMessage, InboxReply, InboxSLAConfig, InternalNote
from .reply_policy import PROGRAMMATIC, recipient_id, reply_eligibility, review_reply_allowed

logger = logging.getLogger(__name__)

# Message types answered on a comment edge rather than a messaging endpoint.
_COMMENT_LIKE_TYPES = {
    InboxMessage.MessageType.COMMENT,
    InboxMessage.MessageType.MENTION,
}

# States a reply can be sent (or re-sent) from.
_SENDABLE_STATUSES = {InboxReply.Status.DRAFT, InboxReply.Status.FAILED}


class ReplyStateError(ValueError):
    """Raised when an operation is not valid for a reply's current status."""


# ---------------------------------------------------------------------------
# Platform dispatch (moved from views.py, behaviour unchanged)
# ---------------------------------------------------------------------------


def reply_failure_reason(exc: Exception) -> str:
    """A short, actionable reason for the user.

    The platform's own error text carries internal diagnostics (trace IDs,
    raw API JSON) that mean nothing to a workspace member, so it stays in
    the log and the UI/API gets a stable sentence instead.
    """
    import httpx

    from providers.exceptions import OAuthError, RateLimitError, TokenExpiredError

    if isinstance(exc, MessagingError):
        return str(exc)
    if isinstance(exc, httpx.TransportError):
        return "Delivery could not be confirmed. Check the conversation before retrying."
    if isinstance(exc, RateLimitError):
        return "the account has hit its rate limit. Wait a few minutes and try again."
    if isinstance(exc, TokenExpiredError | OAuthError):
        return "the connection has expired. Reconnect the account in Workspace Settings."
    return "the platform rejected it. Try again, or reconnect the account if this keeps happening."


def _dispatch_to_platform(message: InboxMessage, body: str, *, origin=PROGRAMMATIC, actor=None) -> str:
    """Post ``body`` back to the platform and return the platform's reply ID.

    Raises if the platform refuses it, so the caller can avoid recording a
    reply as delivered when it never was.
    """
    from apps.publisher.engine import _resolve_publish_credentials

    account = message.social_account
    credentials = _resolve_publish_credentials(account)
    provider = get_provider(account.platform, credentials)

    # The messaging endpoints address a person, not a message, so carry the
    # sender's platform-scoped ID alongside the original payload.
    extra = dict(message.extra or {})
    recipient = recipient_id(message)
    if recipient:
        extra.setdefault("recipient_id", recipient)

    if message.message_type == InboxMessage.MessageType.REVIEW:
        allowed, reason = review_reply_allowed(message, origin=origin, actor=actor)
        if not allowed:
            raise MessagingError(reason, platform=account.platform)
        # Replying again to a review that already has a reply replaces it (an edit).
        result = provider.reply_to_review(
            access_token=account.oauth_access_token, review_id=message.platform_message_id, text=body
        )
    elif message.message_type in _COMMENT_LIKE_TYPES:
        result = provider.reply_to_comment(
            access_token=account.oauth_access_token,
            comment_id=message.platform_message_id,
            text=body,
            extra=extra,
        )
    else:
        policy = reply_eligibility(message, origin=origin, actor=actor, credentials=credentials)
        if not policy["can_send"]:
            raise MessagingError(policy["reason"], platform=account.platform)
        result = provider.reply_to_message(
            access_token=account.oauth_access_token,
            message_id=message.platform_message_id,
            text=body,
            extra=extra,
            human_agent=policy["human_agent"],
        )

    if message.message_type == "dm" and account.platform in META_DM_PLATFORMS and not result.platform_message_id:
        raise MessagingError(
            "Delivery could not be confirmed. Check the conversation before retrying.", platform=account.platform
        )
    return result.platform_message_id


def _record_review_reply(message: InboxMessage, body: str) -> None:
    """Remember the public reply on the review, so "needs reply" and the response time see it."""
    now = timezone.now().isoformat()
    previous = message.review_reply
    message.extra = {
        **(message.extra or {}),
        "review_reply": {
            "comment": body,
            "update_time": now,
            "state": "",
            # Editing a reply moves ``update_time``; response time is measured to the first one.
            "first_replied_at": previous.get("first_replied_at") or previous.get("update_time") or now,
        },
    }
    message.save(update_fields=["extra"])


def delete_review_reply(message: InboxMessage) -> InboxMessage:
    """Remove the public reply from a review on the platform and forget it here.

    Raises ``ValueError`` when ``message`` is not a review with a reply, and the
    platform's own error (``ProviderError``) when it refuses.
    """
    from apps.publisher.engine import _resolve_publish_credentials

    if not message.review_reply:
        raise ValueError("This review has no reply to delete.")
    account = message.social_account
    provider = get_provider(account.platform, _resolve_publish_credentials(account))
    provider.delete_review_reply(account.oauth_access_token, message.platform_message_id)
    extra = dict(message.extra or {})
    extra.pop("review_reply", None)
    message.extra = extra
    message.status = InboxMessage.Status.OPEN
    message.save(update_fields=["extra", "status"])
    return message


def _apply_post_send_side_effects(message: InboxMessage, body: str = "") -> None:
    """Resolve or open the message after a reply goes out, per SLA config."""
    if message.message_type == InboxMessage.MessageType.REVIEW:
        _record_review_reply(message, body)
    sla_config = InboxSLAConfig.objects.filter(workspace=message.workspace, is_active=True).first()
    if sla_config and sla_config.auto_resolve_on_reply:
        if message.status != InboxMessage.Status.RESOLVED:
            message.status = InboxMessage.Status.RESOLVED
            message.save(update_fields=["status"])
    elif message.status == InboxMessage.Status.UNREAD:
        message.status = InboxMessage.Status.OPEN
        message.save(update_fields=["status"])


# ---------------------------------------------------------------------------
# Draft lifecycle
# ---------------------------------------------------------------------------


def create_reply_draft(*, message: InboxMessage, body: str, author=None) -> InboxReply:
    """Create a ``draft`` reply against ``message``. Not sent anywhere."""
    body = (body or "").strip()
    if not body:
        raise ValueError("Reply body cannot be empty.")
    return InboxReply.objects.create(
        inbox_message=message,
        author=author,
        body=body,
        status=InboxReply.Status.DRAFT,
    )


def _lock_reply(reply: InboxReply) -> None:
    """Refresh the caller's instance while locking the row for this transaction."""
    try:
        reply.refresh_from_db(from_queryset=InboxReply.objects.select_for_update())
    except InboxReply.DoesNotExist as exc:
        raise ReplyStateError("This reply has been discarded.") from exc


@transaction.atomic
def update_reply_draft(reply: InboxReply, *, body: str) -> InboxReply:
    """Edit a draft (or failed) reply's body."""
    _lock_reply(reply)
    if reply.status not in _SENDABLE_STATUSES:
        raise ReplyStateError(f"A {reply.get_status_display().lower()} reply cannot be edited.")
    body = (body or "").strip()
    if not body:
        raise ValueError("Reply body cannot be empty.")
    reply.body = body
    reply.save(update_fields=["body", "updated_at"])
    return reply


@transaction.atomic
def discard_reply_draft(reply: InboxReply) -> None:
    """Delete a draft (or failed) reply. Sent replies are permanent."""
    _lock_reply(reply)
    if reply.status not in _SENDABLE_STATUSES:
        raise ReplyStateError(f"A {reply.get_status_display().lower()} reply cannot be discarded.")
    reply.delete()


def send_reply_now(reply: InboxReply, *, actor=None, origin=PROGRAMMATIC) -> InboxReply:
    """Deliver an existing draft/failed reply to the platform.

    On a platform refusal the row is kept and moved to ``failed`` with a
    human-readable ``send_error`` so the team can retry; the underlying
    exception is re-raised for the caller to shape into its own error.
    Providers without a reply API retain the historical local-record behavior,
    except Meta DMs: those must be confirmed by the platform before becoming sent.
    """
    failure = None
    with transaction.atomic():
        # Keep the lock through delivery and persistence. Every competing send,
        # edit or discard must check the latest state after acquiring this lock.
        _lock_reply(reply)
        if reply.status not in _SENDABLE_STATUSES:
            raise ReplyStateError(f"A {reply.get_status_display().lower()} reply cannot be sent again.")

        message = reply.inbox_message
        if actor is not None and reply.author_id is None:
            reply.author = actor

        try:
            platform_reply_id = _dispatch_to_platform(message, reply.body, origin=origin, actor=actor)
        except Exception as exc:
            if isinstance(exc, NotImplementedError) and not (
                message.message_type == "dm" and message.social_account.platform in META_DM_PLATFORMS
            ):
                platform_reply_id = ""
            else:
                if isinstance(exc, NotImplementedError):
                    exc = MessagingError(
                        "This connection cannot send DMs. Reconnect the account or check its configuration."
                    )
                logger.warning(
                    "Inbox reply failed: reply_id=%s platform=%s error_type=%s",
                    reply.id,
                    message.social_account.platform,
                    type(exc).__name__,
                )
                reply.status = InboxReply.Status.FAILED
                reply.send_error = reply_failure_reason(exc)
                reply.save(update_fields=["status", "send_error", "author", "updated_at"])
                failure = exc

        if failure is None:
            reply.status = InboxReply.Status.SENT
            reply.platform_reply_id = platform_reply_id
            reply.send_error = ""
            reply.sent_at = timezone.now()
            reply.save(update_fields=["status", "platform_reply_id", "send_error", "sent_at", "author", "updated_at"])

    # Raising inside atomic would roll back the failed status and its reason.
    if failure is not None:
        raise failure
    # SLA bookkeeping must not roll back a reply already delivered externally.
    try:
        _apply_post_send_side_effects(message, reply.body)
    except Exception:
        logger.exception("Inbox reply %s was sent, but updating the message status failed", reply.id)
    return reply


def send_reply(*, message: InboxMessage, body: str, author=None, origin=PROGRAMMATIC) -> InboxReply:
    """Create a reply and send it in one step (the classic composer flow).

    Refused Meta DM replies are retained as failed drafts for human review.
    Other providers retain their existing failed-row cleanup behavior.
    """
    with transaction.atomic():
        reply = create_reply_draft(message=message, body=body, author=author)
    try:
        return send_reply_now(reply, actor=author, origin=origin)
    except Exception:
        if not (message.message_type == "dm" and message.social_account.platform in META_DM_PLATFORMS):
            InboxReply.objects.filter(pk=reply.pk, status=InboxReply.Status.FAILED).delete()
        raise


# ---------------------------------------------------------------------------
# Triage and bulk actions
# ---------------------------------------------------------------------------

# "Select all matching the filter" acts on at most this many messages at once.
BULK_SELECT_ALL_MAX = 5000


class BulkActionError(ValueError):
    """Raised when a bulk action can't be applied as asked (shown to the person)."""


def apply_triage(message: InboxMessage, action: str, *, actor) -> InboxMessage:
    """One keyboard-triage action. ``resolve`` on an already resolved message archives it."""
    if action == "resolve":
        resolved = message.status == InboxMessage.Status.RESOLVED
        message.status = InboxMessage.Status.ARCHIVED if resolved else InboxMessage.Status.RESOLVED
        message.save(update_fields=["status"])
    elif action == "unread":
        message.status = InboxMessage.Status.UNREAD
        message.save(update_fields=["status"])
    elif action == "assign_me":
        message.assigned_to = actor
        message.save(update_fields=["assigned_to"])
    return message


def bulk_update(workspace, message_ids, *, action: str, value: str = "", actor=None) -> int:
    """Apply ``action`` to the workspace's messages in ``message_ids``; returns how many it matched.

    Ids outside the workspace are ignored, so a forged list can't reach
    another workspace's messages.
    """
    qs = InboxMessage.objects.filter(id__in=message_ids, workspace=workspace)
    if action == "mark_read":
        qs.filter(status=InboxMessage.Status.UNREAD).update(status=InboxMessage.Status.OPEN)
    elif action == "mark_unread":
        qs.update(status=InboxMessage.Status.UNREAD)
    elif action == "resolve":
        qs.exclude(status=InboxMessage.Status.ARCHIVED).update(status=InboxMessage.Status.RESOLVED)
    elif action == "archive":
        qs.update(status=InboxMessage.Status.ARCHIVED)
    elif action == "status":
        qs.update(status=value)
    elif action == "sentiment":
        qs.update(sentiment=value, sentiment_source=InboxMessage.SentimentSource.MANUAL)
    elif action == "unassign":
        qs.update(assigned_to=None)
    elif action == "assign":
        membership = (
            WorkspaceMembership.objects.filter(workspace=workspace, user_id=value).select_related("user").first()
        )
        if membership is None:
            raise BulkActionError("That person is not a member of this workspace.")
        count = qs.update(assigned_to=membership.user)
        if count and membership.user != actor:
            notify(
                user=membership.user,
                event_type=EventType.NEW_INBOX_MESSAGE,
                title=f"{count} inbox message{'s were' if count != 1 else ' was'} assigned to you",
                body="Open your queue to see them.",
                data={"workspace_id": str(workspace.id)},
            )
        return count
    elif action == "note":
        ids = list(qs.values_list("id", flat=True))
        InternalNote.objects.bulk_create(
            [InternalNote(inbox_message_id=mid, author=actor, body=value) for mid in ids], batch_size=500
        )
        return len(ids)
    else:
        raise BulkActionError("Unknown bulk action.")
    return qs.count()
