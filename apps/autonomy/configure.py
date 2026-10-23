"""Changing the policy: one validated path for the settings page and the API.

Both call :func:`update_policy` / :func:`set_account_override`, so a value the
page would refuse is refused by the API too, and every change lands in the
audit trail.
"""

from __future__ import annotations

import datetime as dt

from django.db import transaction
from django.utils import timezone

from . import service
from .actors import ActorInfo
from .models import AccountAutonomyOverride, ActionType, AutonomyPolicy, Decision, Level, RecycleLevel
from .policy import get_or_create_policy

MAX_BLOCKED_PHRASES = 200
BLOCKED_PHRASE_MAX = 100
MAX_UNDO_DELAY_MINUTES = 60

_LEVEL_FIELDS = {
    "draft_level": Level,
    "publish_level": Level,
    "reply_level": Level,
    "recycle_level": RecycleLevel,
}
_BOOL_FIELDS = (
    "review_links",
    "review_mentions",
    "review_prices",
    "review_sensitive",
    "quiet_hours_enabled",
    "ai_risk_check",
    "ai_voice_check",
    "client_visible",
)
_INT_BOUNDS = {
    "max_posts_per_day": (0, 100),
    "max_replies_per_day": (0, 500),
    "undo_delay_minutes": (0, MAX_UNDO_DELAY_MINUTES),
}
_FLOAT_FIELDS = ("ai_risk_threshold", "ai_voice_threshold")
_TIME_FIELDS = ("quiet_start", "quiet_end")

EDITABLE_FIELDS = (
    *_LEVEL_FIELDS,
    "blocked_phrases",
    *_BOOL_FIELDS,
    *_INT_BOUNDS,
    *_FLOAT_FIELDS,
    *_TIME_FIELDS,
    "crisis_paused",
)


def clean_phrases(value) -> list[str]:
    """Blocked phrases as a tidy list: trimmed, de-duplicated, bounded."""
    if isinstance(value, str):
        value = value.replace(",", "\n").splitlines()
    if not isinstance(value, list | tuple):
        raise ValueError("Blocked phrases must be a list of words or phrases.")
    seen: dict[str, str] = {}
    for item in value:
        phrase = " ".join(str(item).split())
        if not phrase:
            continue
        if len(phrase) > BLOCKED_PHRASE_MAX:
            raise ValueError(f"Keep each blocked phrase under {BLOCKED_PHRASE_MAX} characters.")
        seen.setdefault(phrase.casefold(), phrase)
    if len(seen) > MAX_BLOCKED_PHRASES:
        raise ValueError(f"Add at most {MAX_BLOCKED_PHRASES} blocked phrases.")
    return list(seen.values())


def _clean_time(value) -> dt.time | None:
    if value in (None, ""):
        return None
    if isinstance(value, dt.time):
        return value
    try:
        return dt.time.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError("Quiet hours need times like 22:00.") from exc


def _clean(changes: dict) -> dict:
    unknown = sorted(set(changes) - set(EDITABLE_FIELDS))
    if unknown:
        raise ValueError(f"Unknown setting(s): {', '.join(unknown)}.")
    cleaned: dict = {}
    for name, value in changes.items():
        if name in _LEVEL_FIELDS:
            if value not in _LEVEL_FIELDS[name].values:
                raise ValueError(f"{name} must be one of {', '.join(_LEVEL_FIELDS[name].values)}.")
            cleaned[name] = value
        elif name == "blocked_phrases":
            cleaned[name] = clean_phrases(value)
        elif name in _BOOL_FIELDS or name == "crisis_paused":
            cleaned[name] = bool(value)
        elif name in _INT_BOUNDS:
            low, high = _INT_BOUNDS[name]
            try:
                number = int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{name} must be a whole number.") from exc
            if not low <= number <= high:
                raise ValueError(f"{name} must be between {low} and {high}.")
            cleaned[name] = number
        elif name in _FLOAT_FIELDS:
            try:
                number = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{name} must be a number between 0 and 1.") from exc
            if not 0 <= number <= 1:
                raise ValueError(f"{name} must be between 0 and 1.")
            cleaned[name] = number
        elif name in _TIME_FIELDS:
            cleaned[name] = _clean_time(value)
    return cleaned


def update_policy(workspace, actor: ActorInfo, **changes) -> AutonomyPolicy:
    """Apply validated changes to the workspace policy and log them. Raises ``ValueError`` on a bad value."""
    cleaned = _clean(changes)
    with transaction.atomic():
        policy = get_or_create_policy(workspace)
        policy = AutonomyPolicy.objects.select_for_update().get(pk=policy.pk)
        before_pause = policy.crisis_paused
        changed = []
        for name, value in cleaned.items():
            if getattr(policy, name) != value:
                setattr(policy, name, value)
                changed.append(name)
        if policy.quiet_hours_enabled and not (policy.quiet_start and policy.quiet_end):
            raise ValueError("Set a start and end time for quiet hours.")
        if "crisis_paused" in changed:
            policy.crisis_paused_at = timezone.now() if policy.crisis_paused else None
            policy.crisis_paused_by = actor.user if policy.crisis_paused and actor.is_human else None
            changed += ["crisis_paused_at", "crisis_paused_by"]
        if not changed:
            return policy
        policy.save(update_fields=[*changed, "updated_at"])
        if policy.crisis_paused != before_pause:
            summary = (
                "Crisis pause turned on: every autonomous action is stopped and queued ones are held."
                if policy.crisis_paused
                else "Crisis pause turned off: autonomous actions and queued items resume."
            )
        else:
            summary = (
                "Changed autonomy settings: " + ", ".join(sorted(c for c in changed if c in EDITABLE_FIELDS)) + "."
            )
        service.record(
            workspace=workspace,
            actor=actor,
            action_type=ActionType.APPROVAL,
            operation="autonomy.crisis_pause" if policy.crisis_paused != before_pause else "autonomy.policy_update",
            decision=Decision.EXECUTED,
            summary=summary,
        )
    return policy


def set_account_override(workspace, account, actor: ActorInfo, **levels) -> AccountAutonomyOverride | None:
    """Set (or with every value blank, remove) one account's overrides. Blank means "use the workspace's"."""
    names = {"draft_level", "publish_level", "reply_level", "recycle_level", "max_posts_per_day"}
    unknown = sorted(set(levels) - names)
    if unknown:
        raise ValueError(f"Unknown setting(s): {', '.join(unknown)}.")
    if account.workspace_id != workspace.id:
        raise ValueError("That account is not in this workspace.")
    values: dict = {}
    for name, value in levels.items():
        if name == "max_posts_per_day":
            if value in (None, ""):
                values[name] = None
            else:
                values[name] = _clean({name: value})[name]
        elif value in (None, ""):
            values[name] = ""
        else:
            values[name] = _clean({name: value})[name]
    with transaction.atomic():
        override = AccountAutonomyOverride.objects.filter(workspace=workspace, social_account=account).first()
        current = {n: getattr(override, n) if override else ("" if n != "max_posts_per_day" else None) for n in names}
        wanted = {**current, **values}
        if wanted == current:
            return override
        if all(v in ("", None) for v in wanted.values()):
            if override is not None:
                override.delete()
            override = None
        else:
            if override is None:
                override = AccountAutonomyOverride(workspace=workspace, social_account=account)
            for name, value in wanted.items():
                setattr(override, name, value)
            override.save()
        service.record(
            workspace=workspace,
            actor=actor,
            action_type=ActionType.APPROVAL,
            operation="autonomy.account_override",
            decision=Decision.EXECUTED,
            account=account,
            summary="Changed this account's autonomy overrides." if override else "Removed this account's overrides.",
        )
    return override
