"""Payload builders for each webhook event type.

One function per event, called from the code path that owns the change (the
publish engine, the approval services, the inbox sync, ...). Each builds the
``data`` object and hands it to ``services.emit``; none of them knows about a
specific platform; accounts are described by the same few fields everywhere.
"""

import functools
import logging

from . import services
from .models import EventType

logger = logging.getLogger(__name__)

# The publish engine sets this on a PlatformPost (it is never saved) when the
# platform answered a publish with the post's public URL, so ``post.published``
# can carry it. Rows published another way have no permalink.
PERMALINK_ATTR = "_webhook_permalink"


def _never_raises(func):
    """Webhooks are a side channel: building a payload must not fail the change that caused it."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception:
            logger.exception("Could not emit webhook event from %s", func.__name__)

    return wrapper


def _account_data(account) -> dict:
    return {
        "id": str(account.id),
        "platform": account.platform,
        "account_name": account.account_name,
        "account_handle": account.account_handle,
    }


def _user_data(user) -> dict | None:
    if user is None:
        return None
    return {"id": str(user.id), "email": user.email, "name": user.display_name}


def _iso(value):
    return value.isoformat() if value else None


def _platform_post_data(platform_post) -> dict:
    return {
        "post_id": str(platform_post.post_id),
        "platform_post_id": str(platform_post.id),
        "status": platform_post.status,
        "social_account": _account_data(platform_post.social_account),
        "scheduled_at": _iso(platform_post.scheduled_at or platform_post.post.scheduled_at),
        "published_at": _iso(platform_post.published_at),
        "external_id": platform_post.platform_post_id,
        "permalink": getattr(platform_post, PERMALINK_ATTR, None) or getattr(platform_post, "permalink", None) or None,
    }


@_never_raises
def post_event(event_type: str, platform_post, **extra) -> None:
    """Emit one of the per-platform-post events (scheduled, published, failed, partially_published).

    One event per account a post targets: a post going to three accounts sends
    three ``post.published`` events, each naming its own account.
    """
    data = {**_platform_post_data(platform_post), **extra}
    services.emit(event_type, platform_post.post.workspace, data)


@_never_raises
def post_failed(platform_post) -> None:
    post_event(EventType.POST_FAILED, platform_post, error=platform_post.publish_error)


@_never_raises
def post_partially_published(platform_post) -> None:
    post_event(EventType.POST_PARTIALLY_PUBLISHED, platform_post, error=platform_post.thread_error)


@_never_raises
def approval_event(
    event_type: str, post, workspace, user, platform_posts, comment: str = "", channel: str = "web"
) -> None:
    """Emit ``post.approved`` or ``post.changes_requested`` for the targets that moved."""
    services.emit(
        event_type,
        workspace,
        {
            "post_id": str(post.id),
            "platform_post_ids": [str(pp.id) for pp in platform_posts],
            "social_accounts": [_account_data(pp.social_account) for pp in platform_posts],
            "actor": _user_data(user),
            "comment": comment,
            "channel": channel,
        },
    )


@_never_raises
def comment_created(comment) -> None:
    post = comment.post
    services.emit(
        EventType.COMMENT_CREATED,
        post.workspace,
        {
            "comment_id": str(comment.id),
            "post_id": str(post.id),
            "parent_comment_id": str(comment.parent_comment_id) if comment.parent_comment_id else None,
            "visibility": comment.visibility,
            "body": comment.body,
            "author": _user_data(comment.author),
            "created_at": _iso(comment.created_at),
        },
    )


@_never_raises
def inbox_message_received(message) -> None:
    services.emit(
        EventType.INBOX_MESSAGE_RECEIVED,
        message.workspace,
        {
            "message_id": str(message.id),
            "message_type": message.message_type,
            "social_account": _account_data(message.social_account),
            "sender_name": message.sender_name,
            "sender_handle": message.sender_handle,
            "body": message.body,
            "received_at": _iso(message.received_at),
        },
    )


@_never_raises
def review_received(message) -> None:
    """Emit ``review.received`` for a review that is new to the inbox."""
    services.emit(
        EventType.REVIEW_RECEIVED,
        message.workspace,
        {
            "message_id": str(message.id),
            "social_account": _account_data(message.social_account),
            "rating": message.rating,
            "reviewer_name": message.sender_name,
            "body": message.body,
            "has_reply": bool(message.review_reply),
            "received_at": _iso(message.received_at),
        },
    )


@_never_raises
def account_disconnected(account, reason: str = "", *, removed: bool = False) -> None:
    """Emit ``account.disconnected``: the account lost its connection, or a member removed it.

    Sent once per break: removing an account that had already lost its
    connection sends nothing more, since receivers were told when it broke.
    """
    services.emit(
        EventType.ACCOUNT_DISCONNECTED,
        account.workspace,
        {
            **_account_data(account),
            "connection_status": "disconnected" if removed else account.connection_status,
            "removed": removed,
            "reason": reason,
        },
    )


@_never_raises
def agent_action(action) -> None:
    """Emit ``agent.action.executed``, ``.held`` or ``.blocked`` for an agent's or automation's action."""
    event_type = {
        "executed": EventType.AGENT_ACTION_EXECUTED,
        "held": EventType.AGENT_ACTION_HELD,
        "blocked": EventType.AGENT_ACTION_BLOCKED,
    }[action.decision]
    services.emit(
        event_type,
        action.workspace,
        {
            "action_id": str(action.id),
            "actor_type": action.actor_type,
            "actor_name": action.actor_name,
            "action_type": action.action_type,
            "operation": action.operation,
            "decision": action.decision,
            "level": action.level,
            "summary": action.summary,
            "reasons": [r["reason"] for r in action.guardrail_results if not r.get("passed")],
            "risk_score": action.risk_score,
            "social_account": _account_data(action.social_account) if action.social_account_id else None,
            "post_id": str(action.post_id) if action.post_id else None,
            "platform_post_id": str(action.platform_post_id) if action.platform_post_id else None,
            "reply_id": str(action.reply_id) if action.reply_id else None,
            "release_at": _iso(action.release_at),
            "created_at": _iso(action.created_at),
        },
    )
