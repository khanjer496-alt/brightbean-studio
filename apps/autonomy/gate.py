"""The agent-facing wrappers: one call per kind of write, used by REST and MCP alike.

Each ``gate_*`` function evaluates the policy for what the agent is asking and
says what to do (:attr:`PostGate.schedule`, :attr:`ReplyGate.send`, ...). After
the write, ``gate.record_*(...)`` puts the decision in the audit trail and
returns the notice that goes back to the agent, so it learns *why* something
was held.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from django.utils import timezone

from . import service
from .models import ActionType, AgentAction, Decision
from .service import Verdict


class AutonomyRefusalError(Exception):
    """The policy refuses the action outright. ``message`` is written for the agent.

    The refusal is logged by :meth:`log`, which is safe to call again: a caller
    inside a transaction that rolls back calls it once more afterwards so the
    audit trail still has the entry.
    """

    def __init__(self, message: str, write):
        super().__init__(message)
        self.message = message
        self._write = write
        self._pk = uuid.uuid4()

    def log(self) -> None:
        if not AgentAction.objects.filter(pk=self._pk).exists():
            self._write(self._pk)


def notice(verdict: Verdict, action: AgentAction | None = None) -> dict:
    """The ``autonomy`` object an agent gets back, so it knows what happened and what to do next."""
    out = {
        "decision": verdict.decision,
        "level": verdict.level,
        "reasons": verdict.reasons,
        "message": verdict.agent_message
        or (
            f"Saved for a person to review; it was {verdict.reason_text}."
            if verdict.reasons
            else "Allowed by the workspace's autonomy policy."
        ),
        "action_id": str(action.id) if action else None,
        "undo_until": None,
    }
    if action is not None and action.release_at is not None:
        out["undo_until"] = action.release_at.isoformat()
        out["message"] = (
            f"Allowed by the workspace's autonomy policy. It can be undone until {action.release_at:%Y-%m-%d %H:%M} UTC."
        )
    return out


@dataclass
class _Gate:
    workspace: object
    actor: object
    account: object
    text: str
    verdict: Verdict
    action_type: str
    operation: str
    requested_at: dt.datetime | None = None

    @property
    def blocked(self) -> bool:
        return self.verdict.blocked

    def refuse(self, **links) -> AutonomyRefusalError:
        """Log the refusal and build the error for the agent."""
        refusal = AutonomyRefusalError(self.verdict.agent_message, lambda pk: self.record(pk=pk, **links))
        refusal.log()
        return refusal

    def record(self, *, source_text: str = "", pk=None, **links) -> AgentAction:
        return service.record(
            pk=pk,
            workspace=self.workspace,
            actor=self.actor,
            action_type=self.action_type,
            operation=self.operation,
            verdict=self.verdict,
            account=self.account,
            requested_at=self.requested_at,
            source_text=source_text,
            final_text=self.text,
            **links,
        )

    def record_with_notice(self, **links) -> tuple[AgentAction, dict]:
        action = self.record(**links)
        return action, notice(self.verdict, action)


@dataclass
class PostGate(_Gate):
    """Whether a post an agent asked for is scheduled or saved as a draft."""

    schedule: bool = False


def gate_post(
    *,
    workspace,
    actor,
    account,
    text: str,
    schedule: bool,
    scheduled_at: dt.datetime | None = None,
    operation: str,
    source: str = "api",
) -> PostGate:
    """Decide how an agent's create/schedule request is carried out.

    Asking to schedule needs the *publish* level to allow it; when it only
    holds the post, the post is still saved as a draft, which needs the
    *draft* level not to be Off.
    """
    drafting = service.evaluate(
        workspace=workspace,
        actor=actor,
        action_type=ActionType.DRAFT_CONTENT,
        account=account,
        text=text,
        source=source,
    )
    if not schedule:
        return PostGate(
            workspace, actor, account, text, drafting, ActionType.DRAFT_CONTENT, operation, scheduled_at, schedule=False
        )
    verdict = service.evaluate(
        workspace=workspace,
        actor=actor,
        action_type=ActionType.PUBLISH_POSTS,
        account=account,
        text=text,
        when=scheduled_at or timezone.now(),
        source=source,
    )
    if verdict.held and drafting.blocked:
        verdict = drafting
    return PostGate(
        workspace,
        actor,
        account,
        text,
        verdict,
        ActionType.PUBLISH_POSTS,
        operation,
        scheduled_at,
        schedule=verdict.executes,
    )


@dataclass
class ReplyGate(_Gate):
    """Whether an agent's reply is sent (after the undo window) or left as a draft."""

    send: bool = False


def gate_reply(*, workspace, actor, account, text: str, send: bool, operation: str, source: str = "api") -> ReplyGate:
    """Decide how an agent's reply request is carried out. Even a draft needs the reply level not to be Off."""
    now = timezone.now()
    verdict = service.evaluate(
        workspace=workspace,
        actor=actor,
        action_type=ActionType.REPLY,
        account=account,
        text=text,
        when=now,
        source=source,
        drafting_only=not send,
    )
    return ReplyGate(
        workspace, actor, account, text, verdict, ActionType.REPLY, operation, now, send=send and verdict.executes
    )


def content_text(*chunks: str | None, parts=()) -> str:
    """Everything a post asked for would publish as text, for the checks to read.

    ``chunks`` are the caption, title, first comment and any per-account
    overrides; ``parts`` are the later thread parts (``ThreadPartSpec``s), with
    their own per-account captions. Checking the caption alone would let a
    blocked phrase ride in a first comment or the third part of a thread.
    """
    texts = list(chunks)
    for part in parts or ():
        texts.append(part.caption)
        texts.extend((part.caption_overrides or {}).values())
    return "\n".join(t for t in texts if t)


def _queued_to_send(reply):
    """The autonomous decisions that will send ``reply`` when their undo window ends."""
    return AgentAction.objects.filter(
        reply_id=reply.pk, decision=Decision.EXECUTED, undone_at__isnull=True, release_at__isnull=False
    ).exclude(actor_type="human")


def gate_reply_edit(*, workspace, actor, account, reply, text: str, source: str = "api") -> ReplyGate | None:
    """Gate an agent's edit of a draft reply. Raises :class:`AutonomyRefusalError` when the policy refuses it.

    A reply already approved to go out when its undo window ends was checked
    with its old text, so the new text is a fresh send decision. Returns that
    gate, to pass to :func:`requeue_edited_reply` in the same transaction as
    the edit; None for a plain draft nobody has queued.
    """
    queued = _queued_to_send(reply).exists()
    gate = gate_reply(
        workspace=workspace,
        actor=actor,
        account=account,
        text=text,
        send=queued,
        operation="reply.update",
        source=source,
    )
    if gate.blocked:
        raise gate.refuse(reply=reply)
    return gate if queued else None


def requeue_edited_reply(gate: ReplyGate | None, reply) -> dict | None:
    """Replace the old send decision for an edited reply with the new one (a fresh undo window, or a hold)."""
    if gate is None:
        return None
    _queued_to_send(reply).update(undone_at=timezone.now())
    _, note = gate.record_with_notice(reply=reply)
    return note


def publish_text(platform_post) -> str:
    """Everything a platform post would publish as text: caption, first comment and thread parts."""
    parts = [platform_post.effective_title, platform_post.effective_caption, platform_post.effective_first_comment]
    overrides = {s.part_id: s.platform_specific_caption for s in platform_post.part_states.all()}
    for part in platform_post.post.thread_parts.all():
        override = overrides.get(part.id)
        parts.append(override if override is not None else part.caption)
    return "\n".join(p for p in parts if p)


def gate_existing(
    *, workspace, actor, platform_posts, scheduled_at, operation: str, source: str = "api"
) -> list[tuple[object, PostGate]]:
    """Gate scheduling of posts that already exist (one decision per account they target)."""
    return [
        (
            pp,
            gate_post(
                workspace=workspace,
                actor=actor,
                account=pp.social_account,
                text=publish_text(pp),
                schedule=True,
                scheduled_at=scheduled_at,
                operation=operation,
                source=source,
            ),
        )
        for pp in platform_posts
    ]


def gate_resume(*, workspace, actor, post, platform_posts, source: str = "api") -> AutonomyRefusalError | None:
    """Whether an agent may restart stopped threads: resuming publishes the rest, so it needs publishing allowed.

    Nothing is held here (a half-published thread can't wait as a draft): when
    the policy would not let the agent publish, the resume is refused, logged,
    and left for a person. Returns the refusal, or None after recording each
    allowed resume.
    """
    pairs = gate_existing(
        workspace=workspace,
        actor=actor,
        platform_posts=platform_posts,
        scheduled_at=None,
        operation="post.resume_thread",
        source=source,
    )
    for pp, gate in pairs:
        if not gate.schedule:
            message = f"Not resumed because {gate.verdict.reason_text}. A person can resume the thread from the post."

            def write(pk, gate=gate, pp=pp, message=message):
                service.record(
                    pk=pk,
                    workspace=workspace,
                    actor=actor,
                    action_type=ActionType.PUBLISH_POSTS,
                    operation="post.resume_thread",
                    verdict=gate.verdict,
                    decision=Decision.BLOCKED,
                    summary=message,
                    account=pp.social_account,
                    post=post,
                    platform_post=pp,
                    final_text=gate.text,
                )

            refusal = AutonomyRefusalError(message, write)
            refusal.log()
            return refusal
    for pp, gate in pairs:
        # A resume goes out now: no undo window to record, just who did it under which level.
        service.record(
            workspace=workspace,
            actor=actor,
            action_type=ActionType.PUBLISH_POSTS,
            operation="post.resume_thread",
            verdict=gate.verdict,
            account=pp.social_account,
            post=post,
            final_text=gate.text,
            summary="Resumed a thread that stopped part-way.",
        )
    return None


def refusal_for(pairs, post) -> AutonomyRefusalError | None:
    """Log and return the refusal if any of the gated posts is blocked, else None."""
    for pp, gate in pairs:
        if gate.blocked:
            return gate.refuse(post=post, platform_post=pp)
    return None


def record_all(pairs, post) -> dict:
    """Record every decision and return the notice for the most restrictive one (held beats executed)."""
    notices = []
    for pp, gate in pairs:
        _, note = gate.record_with_notice(post=post, platform_post=pp)
        notices.append(note)
    order = {"blocked": 0, "held": 1, "executed": 2}
    return min(notices, key=lambda n: order[n["decision"]])


def deliver_reply(gate: ReplyGate, reply, user) -> dict:
    """Record the decision for ``reply`` and send it now when the policy allows and there is no undo window.

    A reply the policy allows but whose undo window is open stays a draft; the
    background sweep sends it when the window ends. Raises whatever the send
    raises (the entry is kept, noting the platform did not accept it).
    """
    from apps.inbox.services import send_reply_now

    action, note = gate.record_with_notice(reply=reply)
    if gate.send and action.release_at is None:
        try:
            send_reply_now(reply, actor=user)
        except Exception:
            action.summary = f"{action.summary} The platform did not accept the reply.".strip()
            action.save(update_fields=["summary"])
            raise
    return note
