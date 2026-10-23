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
from .models import ActionType, AgentAction
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
        "message": verdict.agent_message or "Allowed by the workspace's autonomy policy.",
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


def publish_text(platform_post) -> str:
    """Everything a platform post would publish as text: caption, first comment and thread parts."""
    parts = [platform_post.effective_caption, platform_post.effective_first_comment]
    parts.extend(p.caption for p in platform_post.post.thread_parts.all())
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
