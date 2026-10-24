"""What happens between "an agent said go" and the platform: the undo window, holds and review.

The publisher and the reply sweep both ask :func:`hold_exclusion` /
:func:`reply_is_held` before sending anything autonomous, so Undo, Hold and
crisis pause all work by one mechanism: the entry says "not yet" and the sender
respects it.
"""

from __future__ import annotations

import datetime as dt
import logging

from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from . import service
from .actors import ActorInfo, human
from .models import ActionType, ActorType, AgentAction, Decision, Level, RecycleLevel
from .policy import get_policy, resolve_level
from .service import UNDOABLE_ACTIONS

logger = logging.getLogger(__name__)


class ReleaseError(ValueError):
    """An operation the entry's current state doesn't allow (shown to the person)."""


# ---------------------------------------------------------------------------
# What the senders ask
# ---------------------------------------------------------------------------


def _waiting(now):
    """Executed autonomous entries that must not be sent yet: inside the undo window, on hold, or in a crisis."""
    return (
        Q(release_at__gt=now)
        | Q(held_at__isnull=False, hold_released_at__isnull=True)
        | Q(workspace__autonomy_policy__crisis_paused=True)
    )


def _live_autonomous():
    return AgentAction.objects.filter(decision=Decision.EXECUTED, undone_at__isnull=True).exclude(actor_type="human")


def hold_exclusion(now=None):
    """A ``Q`` for ``PlatformPost`` querysets: ``.exclude(hold_exclusion())`` drops what must wait."""
    now = now or timezone.now()
    waiting = _live_autonomous().filter(platform_post_id=OuterRef("pk")).filter(_waiting(now))
    return Q(Exists(waiting))


def is_platform_post_held(platform_post, now=None) -> bool:
    from apps.composer.models import PlatformPost

    return PlatformPost.objects.filter(pk=platform_post.pk).filter(hold_exclusion(now)).exists()


def reply_is_held(reply, now=None) -> bool:
    """Whether an autonomous reply must still wait (undo window, hold or crisis pause)."""
    now = now or timezone.now()
    return _live_autonomous().filter(reply_id=reply.pk).filter(_waiting(now)).exists()


# ---------------------------------------------------------------------------
# Re-checking the policy at the moment of sending
# ---------------------------------------------------------------------------

# Levels at which an agent or automation may put something out on its own.
_SENDING_LEVELS = (Level.AUTO_GUARDRAILS, Level.FULL_AUTO, RecycleLevel.PER_ITEM)


def _deciding_entry(platform_post):
    """The latest entry that decides whether ``platform_post`` may go out unattended, or None.

    A person's action (scheduling it, approving it, resuming it) is the latest
    word when it comes last; an automation's executed decision otherwise.
    Refusals don't count: they changed nothing.
    """
    return (
        AgentAction.objects.filter(platform_post_id=platform_post.pk, undone_at__isnull=True)
        .exclude(decision=Decision.BLOCKED)
        .order_by("-created_at", "-id")
        .first()
    )


def _started_by_an_agent(post_id) -> bool:
    return AgentAction.objects.filter(
        post_id=post_id,
        actor_type=ActorType.AGENT,
        action_type=ActionType.PUBLISH_POSTS,
        decision=Decision.EXECUTED,
        undone_at__isnull=True,
    ).exists()


def send_blocker(platform_post) -> str:
    """Why an autonomous post must not go out right now, or "" when it may.

    Asked at the moment of sending (the publisher's claim, each later thread
    part), so a crisis pause or a level lowered after the decision still wins.
    Posts a person scheduled or approved last are never stopped here. A copy in
    a repeating series answers to the level only when an agent started the
    series; a series a person set up keeps going (crisis pause still holds it).
    """
    entry = _deciding_entry(platform_post)
    if entry is None or entry.actor_type == ActorType.HUMAN or entry.decision != Decision.EXECUTED:
        return ""
    workspace = platform_post.post.workspace
    policy = get_policy(workspace)
    if policy.crisis_paused:
        return "crisis pause is on"
    action_type = entry.action_type
    if entry.operation == "recurrence.copy":
        source_id = platform_post.post.recurrence_source_id
        if source_id is None or not _started_by_an_agent(source_id):
            return ""
        action_type = ActionType.PUBLISH_POSTS
    if action_type not in (ActionType.PUBLISH_POSTS, ActionType.RECYCLE_EVERGREEN):
        return ""
    level, source = resolve_level(workspace, platform_post.social_account, action_type, policy=policy)
    if level in _SENDING_LEVELS:
        return ""
    label = RecycleLevel(level).label if level in RecycleLevel.values else level
    where = "this account" if source == "account" else "this workspace"
    return f"autonomy for '{ActionType(action_type).label.lower()}' is now {label} for {where}"


def hold_at_send(platform_post, reason: str, *, what: str = "post") -> AgentAction:
    """Record that the sender held ``platform_post`` for a person, so it shows in Activity to approve."""
    from .actors import system

    entry = _deciding_entry(platform_post)
    return service.record(
        workspace=platform_post.post.workspace,
        actor=system("Publisher"),
        action_type=entry.action_type if entry and entry.action_type in UNDOABLE_ACTIONS else ActionType.PUBLISH_POSTS,
        operation="publish.held_at_send" if what == "post" else "thread.paused",
        decision=Decision.HELD,
        summary=(
            f"Held for review because {reason}. It was not sent; approve it in Activity to send it."
            if what == "post"
            else f"Thread paused because {reason}. The parts already live stay; approve it in Activity to resume."
        ),
        account=platform_post.social_account,
        post=platform_post.post,
        platform_post=platform_post,
        requested_at=platform_post.scheduled_at or platform_post.post.scheduled_at,
        final_text=entry.final_text if entry else "",
    )


# ---------------------------------------------------------------------------
# The tray
# ---------------------------------------------------------------------------


def going_out_soon(workspace, *, limit: int = 50):
    """Autonomous publishes and replies that haven't gone out and can still be stopped."""
    return (
        _live_autonomous()
        .filter(workspace=workspace, release_at__isnull=False)
        .filter(
            Q(platform_post__status="scheduled", platform_post__isnull=False)
            | Q(reply__status="draft", reply__isnull=False)
        )
        .select_related("social_account", "platform_post__post", "reply__inbox_message")
        .order_by("release_at")[:limit]
    )


# ---------------------------------------------------------------------------
# Undo, hold, release
# ---------------------------------------------------------------------------


def _live(action: AgentAction) -> AgentAction:
    """The entry locked and fresh, or an error when it can no longer be changed.

    The platform post or reply it sends is locked too, and re-read: the
    publisher claims a post under that row lock (and the reply sweep sends
    under the reply's), so an undo or hold either lands before the send starts
    or sees that it already has.
    """
    from apps.composer.models import PlatformPost
    from apps.inbox.models import InboxReply

    action = (
        AgentAction.objects.select_for_update(of=("self",))
        .select_related("platform_post__post", "reply")
        .get(pk=action.pk)
    )
    if action.decision != Decision.EXECUTED or action.actor_type == "human":
        raise ReleaseError("Only an executed automated action can be undone.")
    if action.platform_post_id:
        action.platform_post = (
            PlatformPost.objects.select_for_update().select_related("post").get(pk=action.platform_post_id)
        )
    if action.reply_id:
        action.reply = InboxReply.objects.select_for_update().get(pk=action.reply_id)
    return action


def _still_waiting(action: AgentAction) -> None:
    from apps.inbox.models import InboxReply

    if action.platform_post is not None and action.platform_post.status != "scheduled":
        raise ReleaseError("It already went out (or is going out now), so it can't be stopped. Delete it instead.")
    if action.reply is not None and action.reply.status != InboxReply.Status.DRAFT:
        raise ReleaseError("The reply was already sent, so it can't be stopped.")


def undo(action: AgentAction, user) -> AgentAction:
    """Cancel an autonomous publish or reply before it goes out. Safe to repeat."""
    from apps.composer.services import transition_platform_post
    from apps.inbox.models import InboxReply

    with transaction.atomic():
        action = _live(action)
        if action.undone_at is not None:
            return action
        pp, reply = action.platform_post, action.reply
        if pp is not None:
            if pp.status != "scheduled":
                raise ReleaseError(
                    "It already went out (or is going out now), so it can't be undone. Delete it instead."
                )
            try:
                transition_platform_post(pp, "draft")
            except ValueError as exc:
                raise ReleaseError(str(exc)) from exc
        elif reply is not None:
            if reply.status != InboxReply.Status.DRAFT:
                raise ReleaseError("The reply was already sent, so it can't be undone.")
        else:
            raise ReleaseError("There is nothing to undo.")
        action.undone_at = timezone.now()
        action.undone_by = user
        action.save(update_fields=["undone_at", "undone_by"])
    return action


def hold(action: AgentAction, user) -> AgentAction:
    """Stop an autonomous publish or reply from going out until a person releases it."""
    with transaction.atomic():
        action = _live(action)
        if action.undone_at is not None:
            raise ReleaseError("It was already undone.")
        _still_waiting(action)
        if action.held_at is None or action.hold_released_at is not None:
            action.held_at = timezone.now()
            action.hold_released_at = None
            action.save(update_fields=["held_at", "hold_released_at"])
    return action


def release_hold(action: AgentAction, user) -> AgentAction:
    """Let a held item go out now (the undo window no longer applies)."""
    with transaction.atomic():
        action = _live(action)
        if action.undone_at is not None:
            raise ReleaseError("It was already undone.")
        _still_waiting(action)
        now = timezone.now()
        action.hold_released_at = now
        action.release_at = now
        action.save(update_fields=["hold_released_at", "release_at"])
    return action


# ---------------------------------------------------------------------------
# Reviewing what was held
# ---------------------------------------------------------------------------


def approve_held(action: AgentAction, user) -> AgentAction:
    """Approve something an agent's policy held: schedule the draft or send the reply, as this person."""
    from apps.composer.services import transition_platform_post
    from apps.inbox.services import ReplyStateError, send_reply_now

    with transaction.atomic():
        action = (
            AgentAction.objects.select_for_update(of=("self",))
            .select_related("platform_post__post", "reply")
            .get(pk=action.pk)
        )
        if action.decision != Decision.HELD or action.review_outcome:
            raise ReleaseError("This is not waiting for review.")
        pp, reply = action.platform_post, action.reply
        if pp is not None and action.operation == "thread.paused":
            from apps.publisher.engine import resume_thread

            # Approving a paused thread resumes it from the part where it stopped.
            try:
                resume_thread(pp)
            except ValueError as exc:
                raise ReleaseError(str(exc)) from exc
        elif pp is not None and (
            action.action_type == ActionType.PUBLISH_POSTS or action.operation == "publish.held_at_send"
        ):
            if pp.status not in ("draft", "approved"):
                raise ReleaseError("The post has changed since it was held; open it to review it there.")
            when = action.requested_at
            if when is None or when <= timezone.now():
                when = timezone.now() + dt.timedelta(minutes=1)
            try:
                transition_platform_post(pp, "scheduled", scheduled_at=when)
            except ValueError as exc:
                raise ReleaseError(str(exc)) from exc
            # A held schedule kept its repeat setting; the series starts now that it is approved.
            _start_series(pp.post)
        elif reply is not None:
            try:
                send_reply_now(reply, actor=user)
            except ReplyStateError as exc:
                raise ReleaseError(str(exc)) from exc
            except Exception as exc:
                raise ReleaseError(f"The reply was not sent: {reply.send_error or 'please try again later.'}") from exc
        else:
            raise ReleaseError("Open the draft to review it; there is nothing to schedule from here.")
        action.review_outcome = "approved"
        action.reviewed_by = user
        action.reviewed_at = timezone.now()
        action.save(update_fields=["review_outcome", "reviewed_by", "reviewed_at"])
    service.record(
        workspace=action.workspace,
        actor=human(user),
        action_type=ActionType.APPROVAL,
        operation="autonomy.approve_held",
        decision=Decision.EXECUTED,
        account=action.social_account,
        post=action.post,
        platform_post=action.platform_post,
        reply=action.reply,
        summary=f"Approved the held {ActionType(action.action_type).label.lower()} from {action.actor_name}.",
        final_text=action.final_text,
    )
    return action


def _start_series(post) -> None:
    from apps.calendar.models import RecurrenceRule
    from apps.calendar.services import refresh_series

    if post.recurrence_source_id is None and RecurrenceRule.objects.filter(post=post, is_active=True).exists():
        post.refresh_from_db()
        refresh_series(post)


def resolve_paused_thread(platform_post, user) -> int:
    """Mark a paused thread's pending "thread paused" entries as approved by ``user``.

    For a person who resumes the thread somewhere other than Activity (the
    composer), so the entry doesn't keep waiting for a review that already
    happened. Returns how many entries were resolved.
    """
    return AgentAction.objects.filter(
        platform_post=platform_post, operation="thread.paused", decision=Decision.HELD, review_outcome=""
    ).update(review_outcome="approved", reviewed_by=user, reviewed_at=timezone.now())


def dismiss_held(action: AgentAction, user) -> AgentAction:
    """Dismiss a held item: the draft stays a draft, the reply draft is discarded."""
    from apps.inbox.models import InboxReply

    with transaction.atomic():
        action = AgentAction.objects.select_for_update(of=("self",)).select_related("reply").get(pk=action.pk)
        if action.decision != Decision.HELD or action.review_outcome:
            raise ReleaseError("This is not waiting for review.")
        reply = action.reply
        if reply is not None and reply.status == InboxReply.Status.DRAFT:
            reply.delete()
        action.review_outcome = "dismissed"
        action.reviewed_by = user
        action.reviewed_at = timezone.now()
        action.save(update_fields=["review_outcome", "reviewed_by", "reviewed_at"])
    return action


# ---------------------------------------------------------------------------
# After it is live
# ---------------------------------------------------------------------------


def can_delete_from_platform(action: AgentAction) -> bool:
    """Published by an autonomous action, not yet deleted, and the provider can delete."""
    pp = action.platform_post
    if pp is None or action.decision != Decision.EXECUTED or action.actor_type == "human":
        return False
    if pp.status != "published" or not pp.platform_post_id or action.platform_deleted_at is not None:
        return False
    return provider_supports_delete(pp.social_account)


def provider_supports_delete(account) -> bool:
    """Whether the account's provider implements ``delete_post`` (no platform is named here)."""
    from providers import PROVIDER_REGISTRY
    from providers.base import SocialProvider

    provider_cls = PROVIDER_REGISTRY.get(account.platform)
    return provider_cls is not None and provider_cls.delete_post is not SocialProvider.delete_post


_DELETING = "Deleting from the platform..."


def delete_from_platform(action: AgentAction, user) -> AgentAction:
    """Take an autonomous post down from the platform and record how that went."""
    from apps.publisher.engine import _provider_and_access_token

    with transaction.atomic():
        # Claimed under the row lock so a double click deletes (and records) once.
        action = (
            AgentAction.objects.select_for_update(of=("self",))
            .select_related("platform_post__social_account__workspace")
            .get(pk=action.pk)
        )
        pp = action.platform_post
        if action.decision != Decision.EXECUTED or action.actor_type == "human":
            raise ReleaseError("Only a post an automation published can be deleted from here.")
        if pp is None or pp.status != "published" or not pp.platform_post_id:
            raise ReleaseError("There is no published post to delete.")
        if action.platform_deleted_at is not None:
            return action
        if action.platform_delete_result == _DELETING:
            raise ReleaseError("It is being deleted now.")
        action.platform_delete_result = _DELETING
        action.save(update_fields=["platform_delete_result"])
    account = pp.social_account
    if not provider_supports_delete(account):
        result, deleted = f"{account.get_platform_display()} does not allow deleting posts from here.", False
    else:
        try:
            provider, token = _provider_and_access_token(account)
            deleted = bool(provider.delete_post(token, pp.platform_post_id))
            result = "Deleted from the platform." if deleted else "The platform did not remove the post."
        except Exception as exc:
            logger.warning("Delete from platform failed for %s: %s", action.pk, type(exc).__name__)
            deleted, result = False, "The platform refused or could not be reached. Try again, or delete it there."
    if deleted:
        action.platform_deleted_at = timezone.now()
    action.platform_delete_result = result
    action.save(update_fields=["platform_deleted_at", "platform_delete_result"])
    service.record(
        workspace=action.workspace,
        actor=human(user),
        action_type=ActionType.PUBLISH_POSTS,
        operation="post.delete_from_platform",
        decision=Decision.EXECUTED if deleted else Decision.BLOCKED,
        account=action.social_account,
        post=action.post,
        platform_post=pp,
        summary=result,
    )
    if not deleted:
        raise ReleaseError(result)
    return action


def actor_for(user) -> ActorInfo:
    return human(user)
