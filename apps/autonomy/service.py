"""Enforcing the autonomy policy and writing the audit trail.

Call :func:`evaluate` before an agent or automation does something, act on the
:class:`Verdict` (do it, do the safe version of it, or refuse), then call
:func:`record` so the decision is in the trail. People are never held back: for a
person ``evaluate`` always says "execute" and ``record`` just writes the entry.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

from django.db import models, transaction
from django.utils import timezone

from . import guardrails
from .models import ActionType, AgentAction, Decision, Level, RecycleLevel
from .policy import get_policy, resolve_level

logger = logging.getLogger(__name__)

EXECUTE = "execute"
HOLD = "hold"
BLOCK = "block"

_DECISION = {EXECUTE: Decision.EXECUTED, HOLD: Decision.HELD, BLOCK: Decision.BLOCKED}

# Actions that put something in front of the public, so crisis pause holds them
# rather than refusing them outright.
_HELD_IN_CRISIS = (ActionType.PUBLISH_POSTS, ActionType.REPLY)

# Actions that reach the public, so an executed one waits out the undo window.
UNDOABLE_ACTIONS = (ActionType.PUBLISH_POSTS, ActionType.REPLY, ActionType.RECYCLE_EVERGREEN)

_SAFE_VERSION = {
    ActionType.DRAFT_CONTENT: "It was saved as a draft and flagged for a person to look at.",
    ActionType.PUBLISH_POSTS: "It was saved as a draft; a person can approve it in Activity.",
    ActionType.REPLY: "It was saved as a draft reply in the inbox; a person can send it.",
    ActionType.RECYCLE_EVERGREEN: "It was sent for review instead of being queued.",
}


@dataclass
class Verdict:
    outcome: str
    action_type: str
    level: str = ""
    level_source: str = ""
    reasons: list[str] = field(default_factory=list)
    results: list[dict] = field(default_factory=list)
    risk_score: float | None = None
    ai_used: bool = False
    # Evergreen only: the dial says "each item's own setting", so the caller keeps its own rules.
    defer_to_item: bool = False
    # The undo window to apply to an executed autonomous publish or reply.
    undo_delay: dt.timedelta = dt.timedelta(0)

    @property
    def decision(self) -> str:
        return _DECISION[self.outcome]

    @property
    def executes(self) -> bool:
        return self.outcome == EXECUTE

    @property
    def held(self) -> bool:
        return self.outcome == HOLD

    @property
    def blocked(self) -> bool:
        return self.outcome == BLOCK

    @property
    def reason_text(self) -> str:
        return "; ".join(self.reasons)

    @property
    def summary(self) -> str:
        if self.held:
            return f"Held for review because {self.reason_text}."
        if self.blocked:
            return f"Blocked because {self.reason_text}."
        if self.level:
            label = Level(self.level).label if self.level in Level.values else self.level
            if self.reasons:
                return f"Allowed under {label}, {self.reason_text}."
            return f"Allowed under {label}."
        return ""

    @property
    def agent_message(self) -> str:
        """What to tell the agent, written so it knows what happens next."""
        if self.held:
            return f"Held for review because {self.reason_text}. {_SAFE_VERSION[self.action_type]}"
        if self.blocked:
            return f"Blocked because {self.reason_text}. Nothing was saved."
        return ""


def _verdict(outcome, action_type, level, source, reasons, report=None, **extra) -> Verdict:
    # A decision made by the dial itself (Off, Draft only, crisis pause) has no guardrail
    # behind it; it still gets a row so the trail shows what stopped it.
    results = (
        report.as_dicts()
        if report
        else [
            {"name": "policy", "label": "Autonomy policy", "passed": False, "reason": r, "blocking": outcome == BLOCK}
            for r in reasons
        ]
    )
    return Verdict(
        outcome=outcome,
        action_type=action_type,
        level=level,
        level_source=source,
        reasons=reasons,
        results=results,
        risk_score=report.risk_score if report else None,
        ai_used=report.ai_used if report else False,
        **extra,
    )


# The trail keeps what was said, not an unbounded copy of it: a long thread is clipped here.
TEXT_LIMIT = 20_000


def _clip(text: str) -> str:
    text = text or ""
    return text if len(text) <= TEXT_LIMIT else text[: TEXT_LIMIT - 1] + "…"


def undo_delay(policy) -> dt.timedelta:
    return dt.timedelta(minutes=policy.undo_delay_minutes)


def evaluate(
    *,
    workspace,
    actor,
    action_type: str,
    account=None,
    text: str = "",
    when: dt.datetime | None = None,
    source: str = "api",
    drafting_only: bool = False,
) -> Verdict:
    """Decide whether ``actor`` may do ``action_type`` now: execute it, hold it for review, or block it.

    ``drafting_only`` is for a request that only writes something down (a reply
    draft): it needs the level not to be Off, and nothing goes out.
    """
    if actor.is_human:
        return Verdict(outcome=EXECUTE, action_type=action_type)
    policy = get_policy(workspace)
    level, level_source = resolve_level(workspace, account, action_type, policy=policy)
    when = when or timezone.now()
    delay = undo_delay(policy)

    if policy.crisis_paused:
        reason = "crisis pause is on, so every autonomous action is stopped until a person turns it off"
        outcome = HOLD if action_type in _HELD_IN_CRISIS and not drafting_only else BLOCK
        return _verdict(outcome, action_type, level, level_source, [reason])

    if level == Level.OFF:
        label = ActionType(action_type).label.lower()
        where = "this account" if level_source == "account" else "this workspace"
        return _verdict(BLOCK, action_type, level, level_source, [f"autonomy for '{label}' is Off for {where}"])

    if level == Level.DRAFT_ONLY:
        # Nothing goes out without a person here, so a blocked phrase doesn't refuse the draft:
        # it is flagged, and the reviewer sees the warning beside it in Activity.
        report = guardrails.Report([guardrails.check_blocked_phrases(policy, text)])
        for result in report.results:
            result.blocking = False
        flags = [f"flagged: {r.reason}" for r in report.failures()]
        if drafting_only or action_type == ActionType.DRAFT_CONTENT:
            return _verdict(EXECUTE, action_type, level, level_source, flags, report)
        reason = "its autonomy level is Draft only, so a person has to approve it"
        report.results.insert(0, guardrails.GuardrailResult("policy", "Autonomy policy", False, reason))
        return _verdict(HOLD, action_type, level, level_source, [reason, *flags], report)

    if drafting_only:
        return _verdict(EXECUTE, action_type, level, level_source, [])

    if level == RecycleLevel.PER_ITEM:
        return _verdict(EXECUTE, action_type, level, level_source, [], defer_to_item=True, undo_delay=delay)

    report = guardrails.run(
        policy,
        workspace=workspace,
        account=account,
        action_type=action_type,
        text=text,
        when=when,
        level=level,
        source=source,
    )
    failures = report.failures()
    reasons = [r.reason for r in failures]
    if report.blocked:
        reasons = [r.reason for r in failures if r.blocking]
        return _verdict(BLOCK, action_type, level, level_source, reasons, report)
    if report.needs_review:
        return _verdict(HOLD, action_type, level, level_source, reasons, report)
    return _verdict(EXECUTE, action_type, level, level_source, [], report, undo_delay=delay)


def release_time(verdict: Verdict, requested_at: dt.datetime | None, now: dt.datetime | None = None):
    """The earliest an executed autonomous publish or reply may go out (None: no undo window)."""
    if verdict.undo_delay <= dt.timedelta(0):
        return None
    now = now or timezone.now()
    return max(requested_at or now, now + verdict.undo_delay)


def record(
    *,
    workspace,
    actor,
    action_type: str,
    operation: str,
    verdict: Verdict | None = None,
    decision: str | None = None,
    account=None,
    post=None,
    platform_post=None,
    reply=None,
    source_text: str = "",
    final_text: str = "",
    requested_at: dt.datetime | None = None,
    summary: str = "",
    target_key: str = "",
    ai_used: bool = False,
    notify: bool = True,
    pk=None,
) -> AgentAction:
    """Write one audit entry (and tell people and webhooks about a hold or a block)."""
    if verdict is not None:
        decision = decision or verdict.decision
        summary = summary or verdict.summary
    action = AgentAction.objects.create(
        **({"id": pk} if pk else {}),
        workspace=workspace,
        social_account=account,
        account_label=(account.account_name or account.account_handle) if account is not None else "",
        actor_type=actor.type,
        actor_name=actor.name,
        actor_user=actor.user if actor.is_human else None,
        api_key=actor.api_key,
        action_type=action_type,
        operation=operation,
        decision=decision or Decision.EXECUTED,
        level=(verdict.level if verdict is not None else ""),
        summary=summary,
        guardrail_results=verdict.results if verdict is not None else [],
        ai_used=bool(verdict and verdict.ai_used) or ai_used,
        risk_score=verdict.risk_score if verdict is not None else None,
        source_text=_clip(source_text),
        final_text=_clip(final_text),
        post=post,
        platform_post=platform_post,
        reply=reply,
        requested_at=requested_at,
        release_at=(
            release_time(verdict, requested_at)
            if verdict is not None and verdict.executes and not actor.is_human and action_type in UNDOABLE_ACTIONS
            else None
        ),
        target_key=target_key,
    )
    if notify:
        announce(action)
    return action


def recently_recorded(workspace, target_key: str, decision: str, *, hours: int = 24) -> bool:
    """Whether the same refusal or hold was already logged lately (an hourly task would repeat it)."""
    if not target_key:
        return False
    return AgentAction.objects.filter(
        workspace=workspace,
        target_key=target_key,
        decision=decision,
        created_at__gte=timezone.now() - dt.timedelta(hours=hours),
    ).exists()


def announce(action: AgentAction) -> None:
    """Webhook for every autonomous decision; a notification for the ones needing a person."""
    if action.actor_type == "human":
        return
    try:
        with transaction.atomic():
            from apps.webhooks import events

            events.agent_action(action)
            if action.decision in (Decision.HELD, Decision.BLOCKED):
                _notify_managers(action)
    except Exception:
        logger.exception("Could not announce agent action %s", action.pk)


def _notify_managers(action: AgentAction) -> None:
    from apps.members.models import WorkspaceMembership
    from apps.notifications.engine import notify
    from apps.notifications.models import EventType

    held = action.decision == Decision.HELD
    event_type = EventType.AGENT_ACTION_HELD if held else EventType.AGENT_ACTION_BLOCKED
    what = ActionType(action.action_type).label.lower()
    title = f"{action.actor_name}: {what} held for review" if held else f"{action.actor_name}: {what} blocked"
    members = WorkspaceMembership.objects.filter(
        workspace=action.workspace,
        workspace_role__in=[WorkspaceMembership.WorkspaceRole.OWNER, WorkspaceMembership.WorkspaceRole.MANAGER],
    ).select_related("user")
    for membership in members:
        notify(
            user=membership.user,
            event_type=event_type,
            title=title,
            body=action.summary,
            data={"workspace_id": str(action.workspace_id), "action_id": str(action.id)},
        )


def recycling_stopped(workspace, account) -> str:
    """Why evergreen recycling must not run for ``account`` right now, or "" when it may."""
    policy = get_policy(workspace)
    if policy.crisis_paused:
        return "crisis pause is on"
    level, _ = resolve_level(workspace, account, ActionType.RECYCLE_EVERGREEN, policy=policy)
    if level == Level.OFF:
        return "autonomy for recycling evergreen posts is Off"
    return ""


def reply_send_blocker(workspace, account) -> str:
    """Why an unattended reply must not be sent now, or "" when the reply policy allows it."""
    policy = get_policy(workspace)
    if policy.crisis_paused:
        return "crisis pause is on"
    level, source = resolve_level(workspace, account, ActionType.REPLY, policy=policy)
    if level in (Level.AUTO_GUARDRAILS, Level.FULL_AUTO):
        return ""
    where = "this account" if source == "account" else "this workspace"
    return f"autonomy for replies is now {Level(level).label} for {where}"


def record_scheduled(
    *, workspace, actor, operation: str, post, platform_posts, text: str, summary: str, source_text: str = ""
) -> None:
    """Log posts that were queued without a policy decision (a person's schedule, a repeating series).

    One entry each and no announcement: the entries exist so the trail is
    complete and so crisis pause can hold what an automation queued.
    """
    AgentAction.objects.bulk_create(
        [
            AgentAction(
                workspace=workspace,
                social_account=pp.social_account,
                account_label=pp.social_account.account_name or pp.social_account.account_handle,
                actor_type=actor.type,
                actor_name=actor.name,
                actor_user=actor.user if actor.is_human else None,
                action_type=ActionType.PUBLISH_POSTS,
                operation=operation,
                decision=Decision.EXECUTED,
                post=post,
                platform_post=pp,
                source_text=_clip(source_text),
                final_text=_clip(text),
                requested_at=pp.scheduled_at or post.scheduled_at,
                summary=summary,
            )
            for pp in platform_posts
        ]
    )


def client_visible_actions(workspace, *, limit: int = 50):
    """What the workspace shows its clients: automated posts and replies that have actually gone out.

    Nothing when the workspace hasn't turned this on. Held, blocked and undone
    entries, people's own actions and the guardrail details stay with the team.
    """
    if not get_policy(workspace).client_visible:
        return AgentAction.objects.none()
    now = timezone.now()
    return (
        AgentAction.objects.filter(
            workspace=workspace,
            decision=Decision.EXECUTED,
            undone_at__isnull=True,
            action_type__in=UNDOABLE_ACTIONS,
        )
        .exclude(actor_type="human")
        .exclude(operation="recurrence.copy")
        .filter(models.Q(release_at__isnull=True) | models.Q(release_at__lte=now))
        # Only what is actually out there: a post still queued, held, failed or since taken down isn't shown.
        .filter(models.Q(platform_post__status="published") | models.Q(reply__status="sent"))
        .filter(platform_deleted_at__isnull=True)
        .select_related("social_account")
        .order_by("-created_at")[:limit]
    )
