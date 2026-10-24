"""Engagement automations: rules, the periodic checker and the one action each rule takes.

A :class:`PlugRule` says "when this post reaches N likes/reposts/views within T
hours, do X". The checker (:func:`check_rules`) gives every rule a
:class:`PlugRun` per watched post, reads the post's counts through the
provider's ``plug_metrics`` (the provider class declares which counts it can
read and which actions it offers, ``SocialProvider.plugs``), and when a count
is reached asks the autonomy policy before acting as the "system" actor:

* blocked: the run ends as ``blocked`` and the trail says why;
* held (Draft only, a token or crisis pause): the run waits as
  ``awaiting_approval`` and a person approves or dismisses it in Activity;
* allowed: the run waits out the undo window (``queued``) and then acts.

Each (rule, post) pair is one row, claimed by a status change under a row lock,
so a rule fires at most once however many workers race on it, and a request
whose outcome is unknown (``ambiguous_write``) is recorded, never repeated.
Reads are spaced by the rule's interval (never shorter than the provider's
``check_interval_minutes``) and back off after errors and rate limits.

Platform rules shape what may happen on its own: an action by *another*
account (a cross-account repost) is always held for a person, a network whose
terms forbid automated posting (``PlugCapability.requires_approval``) holds
every action, and one account takes at most
``PlugCapability.max_actions_per_day`` automated actions in any 24 hours.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
from dataclasses import dataclass

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.autonomy import release, service
from apps.autonomy.actors import system
from apps.autonomy.models import ActionType, AgentAction, Decision
from apps.composer.models import PlatformPost
from apps.social_accounts.models import SocialAccount
from providers import plug_capability_for
from providers.exceptions import APIError, ProviderError, QuotaExceededError, RateLimitError
from providers.types import PlugAction, PlugTarget

from .models import PlugRule, PlugRun

logger = logging.getLogger(__name__)

MAX_RULES_PER_POST = 5
MAX_DEFAULT_RULES_PER_ACCOUNT = 5
MAX_THRESHOLD = 100_000_000
MAX_WINDOW_HOURS = 168
MAX_INTERVAL_MINUTES = 24 * 60
DAY = dt.timedelta(hours=24)
MAX_ATTEMPTS = 3
MAX_CHECK_FAILURES = 8
# Reads of one account's posts in a single run of the checker, so a long list can't spend a platform's allowance.
MAX_READS_PER_ACCOUNT = 25
BACKOFF_CAP = dt.timedelta(hours=6)
RATE_LIMIT_PAUSE = dt.timedelta(minutes=30)
ACTION_RETRY_DELAY = dt.timedelta(minutes=10)
# An action that was claimed but never reported back: the worker died mid-request, so the outcome is unknown.
FIRING_STALE_AFTER = dt.timedelta(minutes=30)

WHO = "Engagement automations"


class PlugError(ValueError):
    """A rule or run the caller can't have; the message is written for the person."""


@dataclass
class CheckResult:
    created: int = 0
    read: int = 0
    fired: int = 0
    expired: int = 0
    failed: int = 0


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


def clean_rule(
    *,
    workspace,
    account: SocialAccount,
    platform_post: PlatformPost | None,
    trigger: str,
    threshold,
    window_hours,
    action: str,
    actor_account: SocialAccount | None = None,
    message: str = "",
    check_interval_minutes=None,
    exclude_rule: PlugRule | None = None,
) -> dict:
    """The validated fields of a rule, or :class:`PlugError` saying what to change."""
    if account.workspace_id != workspace.id:
        raise PlugError("That account is not in this workspace.")
    capability = plug_capability_for(account.platform)
    label = account.get_platform_display()
    if not capability.watchable:
        raise PlugError(f"{label} offers no way to watch a post's numbers, so it can't have engagement automations.")
    if trigger not in capability.triggers:
        offered = ", ".join(sorted(t.value for t in capability.triggers))
        raise PlugError(f"{label} does not report {trigger} for a post. It reports: {offered}.")
    try:
        threshold = int(threshold)
        window_hours = int(window_hours)
    except (TypeError, ValueError) as exc:
        raise PlugError("The count and the hours must be whole numbers.") from exc
    if not 1 <= threshold <= MAX_THRESHOLD:
        raise PlugError("The count must be at least 1.")
    if not 1 <= window_hours <= MAX_WINDOW_HOURS:
        raise PlugError(f"The window must be between 1 and {MAX_WINDOW_HOURS} hours.")
    if action not in {a.value for a in PlugAction}:
        raise PlugError("Choose repost, reply or quote.")
    if check_interval_minutes in ("", None):
        check_interval_minutes = None
    else:
        try:
            check_interval_minutes = int(check_interval_minutes)
        except (TypeError, ValueError) as exc:
            raise PlugError("The time between checks must be a whole number of minutes.") from exc
        floor = capability.check_interval_minutes
        if not floor <= check_interval_minutes <= MAX_INTERVAL_MINUTES:
            raise PlugError(f"{label} posts can be checked every {floor} to {MAX_INTERVAL_MINUTES} minutes.")

    actor = actor_account if actor_account is not None and actor_account.pk != account.pk else None
    if actor is not None:
        if actor.workspace_id != workspace.id:
            raise PlugError("The account that reposts must be in the same workspace.")
        if actor.platform != account.platform:
            raise PlugError("Another account can only repost a post on the same network.")
        if actor.connection_status != SocialAccount.ConnectionStatus.CONNECTED:
            raise PlugError("The account that reposts is not connected.")
    if not capability.allows(action, cross_account=actor is not None):
        who = "another account" if actor is not None else "the account itself"
        raise PlugError(f"On {label}, {who} cannot {action} a post.")

    message = (message or "").strip()
    if action == PlugAction.REPOST:
        message = ""
    else:
        if not message:
            raise PlugError("Write the message to post.")
        limit = capability.message_max_length or account.char_limit
        if len(message) > limit:
            raise PlugError(f"The message is {len(message)} characters; {label} allows {limit}.")

    existing = PlugRule.objects.filter(social_account=account, platform_post=platform_post)
    if exclude_rule is not None:
        existing = existing.exclude(pk=exclude_rule.pk)
    if existing.filter(trigger=trigger, threshold=threshold, action=action, actor_account=actor).exists():
        raise PlugError("That rule already exists.")
    cap = MAX_RULES_PER_POST if platform_post is not None else MAX_DEFAULT_RULES_PER_ACCOUNT
    if existing.count() >= cap:
        raise PlugError(f"At most {cap} rules fit on {'one post' if platform_post is not None else 'an account'}.")
    return {
        "trigger": trigger,
        "threshold": threshold,
        "window_hours": window_hours,
        "action": action,
        "actor_account": actor,
        "message": message,
        "check_interval_minutes": check_interval_minutes,
    }


def create_rule(*, workspace, account, platform_post=None, user=None, is_active: bool = True, **fields) -> PlugRule:
    if platform_post is not None:
        if platform_post.social_account_id != account.id or platform_post.post.workspace_id != workspace.id:
            raise PlugError("That post is not on this account.")
        if platform_post.status == PlatformPost.Status.FAILED:
            raise PlugError("A post that failed to publish can't have automations.")
    cleaned = clean_rule(workspace=workspace, account=account, platform_post=platform_post, **fields)
    if platform_post is not None and platform_post.status == PlatformPost.Status.PUBLISHED:
        ends = platform_post.published_at + dt.timedelta(hours=cleaned["window_hours"])
        if ends <= timezone.now():
            raise PlugError("This post was published longer ago than that window, so the rule could never fire.")
    return PlugRule.objects.create(
        workspace=workspace,
        social_account=account,
        platform_post=platform_post,
        created_by=user,
        is_active=is_active,
        **cleaned,
    )


def update_rule(rule: PlugRule, **changes) -> PlugRule:
    """Change a rule's trigger, count, window, action, message, reposting account or on/off switch."""
    is_active = changes.pop("is_active", None)
    if changes:
        merged = {
            "trigger": rule.trigger,
            "threshold": rule.threshold,
            "window_hours": rule.window_hours,
            "action": rule.action,
            "actor_account": rule.actor_account,
            "message": rule.message,
            "check_interval_minutes": rule.check_interval_minutes,
            **changes,
        }
        cleaned = clean_rule(
            workspace=rule.workspace,
            account=rule.social_account,
            platform_post=rule.platform_post,
            exclude_rule=rule,
            **merged,
        )
        for key, value in cleaned.items():
            setattr(rule, key, value)
    if is_active is not None:
        rule.is_active = is_active
    rule.save()
    return rule


def delete_rule(rule: PlugRule) -> None:
    """Remove a rule. Runs still waiting are dropped with it; the audit trail keeps what already happened."""
    rule.delete()


def can_have_rules(account: SocialAccount) -> bool:
    return plug_capability_for(account.platform).watchable


def reposter_choices(account: SocialAccount):
    """Other connected accounts of the same workspace and network that could repost ``account``'s posts."""
    if not plug_capability_for(account.platform).can_act_for_others:
        return SocialAccount.objects.none()
    return (
        SocialAccount.objects.for_workspace(account.workspace_id)
        .filter(platform=account.platform, connection_status=SocialAccount.ConnectionStatus.CONNECTED)
        .exclude(pk=account.pk)
        .order_by("account_name")
    )


# ---------------------------------------------------------------------------
# The checker
# ---------------------------------------------------------------------------


def _ends_at(run: PlugRun) -> dt.datetime:
    return run.platform_post.published_at + dt.timedelta(hours=run.rule.window_hours)


def _create_runs(now) -> int:
    """Give every active rule a run for each published post it should watch. Idempotent (the unique key)."""
    created = 0
    rules = PlugRule.objects.filter(is_active=True, social_account__connection_status="connected").select_related(
        "platform_post"
    )
    for rule in rules:
        window = dt.timedelta(hours=rule.window_hours)
        if rule.platform_post_id is not None:
            posts = [rule.platform_post] if rule.platform_post.status == PlatformPost.Status.PUBLISHED else []
        else:
            # A default applies to posts published after it was set, never to the account's back catalogue.
            posts = list(
                PlatformPost.objects.filter(
                    social_account_id=rule.social_account_id,
                    status=PlatformPost.Status.PUBLISHED,
                    published_at__gte=max(now - window, rule.created_at),
                ).exclude(plug_runs__rule=rule)
            )
        for post in posts:
            if post.published_at is None or not post.platform_post_id or post.published_at + window < now:
                continue
            try:
                with transaction.atomic():
                    _, made = PlugRun.objects.get_or_create(rule=rule, platform_post=post)
            except IntegrityError:
                made = False
            created += made
    return created


def _backoff(run: PlugRun, now, base: dt.timedelta) -> dt.datetime:
    return now + min(base * (2 ** min(run.check_failures, 10)), BACKOFF_CAP)


def _drop_unpublished() -> int:
    """End the watch on posts that are no longer live (deleted, taken down), so they stop being read."""
    return (
        PlugRun.objects.filter(status=PlugRun.Status.WATCHING)
        .exclude(platform_post__status=PlatformPost.Status.PUBLISHED)
        .update(status=PlugRun.Status.CANCELLED, next_check_at=None, detail="The post is no longer published.")
    )


def _due_runs(now):
    return (
        PlugRun.objects.filter(status=PlugRun.Status.WATCHING)
        .filter(Q(next_check_at__isnull=True) | Q(next_check_at__lte=now))
        .filter(
            rule__is_active=True,
            rule__social_account__connection_status=SocialAccount.ConnectionStatus.CONNECTED,
            rule__workspace__is_archived=False,
            platform_post__status=PlatformPost.Status.PUBLISHED,
        )
        .select_related("rule__social_account", "rule__actor_account", "rule__workspace", "platform_post")
        .order_by("next_check_at", "created_at")
    )


def check_rules(*, now=None) -> CheckResult:
    """The periodic task: watch every open run, read each post once and fire the rules whose count is reached."""
    from apps.publisher.engine import _provider_and_access_token

    now = now or timezone.now()
    result = CheckResult(created=_create_runs(now))
    result.expired += _drop_unpublished()
    by_account: dict = {}
    for run in _due_runs(now):
        if _ends_at(run) < now:
            _finish(run, PlugRun.Status.EXPIRED, "The window ended before the count was reached.")
            result.expired += 1
            continue
        by_account.setdefault(run.rule.social_account_id, []).append(run)

    for runs in by_account.values():
        account = runs[0].rule.social_account
        capability = plug_capability_for(account.platform)
        try:
            provider, token = _provider_and_access_token(account)
        except Exception:
            logger.exception("Plugs: could not open %s", account.pk)
            _delay(runs, now, ACTION_RETRY_DELAY)
            continue
        counts_by_post: dict = {}
        reads = 0
        for run in runs:
            post = run.platform_post
            if run.rule.trigger not in capability.triggers:
                _finish(run, PlugRun.Status.FAILED, "This network no longer reports that number.")
                result.failed += 1
                continue
            counts = counts_by_post.get(post.pk)
            if counts is None:
                if reads >= MAX_READS_PER_ACCOUNT:
                    break
                reads += 1
                try:
                    counts = provider.plug_metrics(token, post.platform_post_id)
                except (QuotaExceededError, RateLimitError) as exc:
                    # The platform said "not now": every remaining post of the account waits.
                    wait = dt.timedelta(seconds=getattr(exc, "retry_after", 0) or 0) or RATE_LIMIT_PAUSE
                    resets_at = getattr(exc, "resets_at", None)
                    until = resets_at if resets_at else now + max(wait, RATE_LIMIT_PAUSE)
                    PlugRun.objects.filter(pk__in=[r.pk for r in runs]).update(next_check_at=until)
                    break
                except NotImplementedError:
                    _finish(run, PlugRun.Status.FAILED, "This network can't report that number.")
                    result.failed += 1
                    continue
                except ProviderError as exc:
                    if exc.retryable:
                        _read_failed(run, now, result)
                    else:
                        # Not found or refused for good (usually deleted on the network): stop instead of re-reading.
                        _finish(
                            run, PlugRun.Status.FAILED, f"The post's numbers could not be read: {_message_for(exc)}"
                        )
                        result.failed += 1
                    continue
                except Exception as exc:
                    logger.warning("Plugs: counts for %s failed: %s", post.pk, type(exc).__name__)
                    _read_failed(run, now, result)
                    continue
                counts_by_post[post.pk] = counts
                result.read += 1
            run.last_counts = counts
            run.last_checked_at = now
            run.check_failures = 0
            run.next_check_at = now + dt.timedelta(minutes=run.rule.interval_minutes)
            run.save(update_fields=["last_counts", "last_checked_at", "check_failures", "next_check_at", "updated_at"])
            value = int(counts.get(run.rule.trigger, 0))
            if value >= run.rule.threshold:
                fire(run, value=value, now=now)
                result.fired += 1
    return result


def _read_failed(run: PlugRun, now, result: CheckResult) -> None:
    run.check_failures += 1
    if run.check_failures >= MAX_CHECK_FAILURES:
        _finish(run, PlugRun.Status.FAILED, "The post's numbers could not be read.")
        result.failed += 1
        return
    run.next_check_at = _backoff(run, now, dt.timedelta(minutes=run.rule.interval_minutes))
    run.save(update_fields=["check_failures", "next_check_at", "updated_at"])


def _delay(runs, now, wait) -> None:
    PlugRun.objects.filter(pk__in=[r.pk for r in runs]).update(next_check_at=now + wait)


def _finish(run: PlugRun, status: str, detail: str) -> None:
    run.status = status
    run.detail = detail
    run.next_check_at = None
    run.save(update_fields=["status", "detail", "next_check_at", "updated_at"])


# ---------------------------------------------------------------------------
# Firing
# ---------------------------------------------------------------------------


def _action_type(rule: PlugRule) -> str:
    return ActionType.REPLY if rule.action == PlugAction.REPLY else ActionType.PUBLISH_POSTS


def _text_for(run: PlugRun) -> str:
    rule = run.rule
    return rule.message or run.platform_post.effective_caption or ""


def fire(run: PlugRun, *, value: int, now=None) -> PlugRun:
    """The count was reached: ask the autonomy policy, then block, hold, queue or act. At most once per run."""
    now = now or timezone.now()
    perform_now = False
    with transaction.atomic():
        run = (
            PlugRun.objects.select_for_update(of=("self",))
            .select_related(
                "rule__social_account", "rule__actor_account", "rule__workspace__organization", "platform_post__post"
            )
            .get(pk=run.pk)
        )
        if run.status != PlugRun.Status.WATCHING:
            return run
        rule = run.rule
        actor_account = rule.acting_account
        run.reached_value = value
        run.next_check_at = None
        problem = _actor_problem(rule)
        if problem:
            run.status, run.detail = PlugRun.Status.FAILED, problem
            run.save()
            return run
        who = system(WHO)
        action_type = _action_type(rule)
        text = _text_for(run)
        # One account's automated actions are counted under its row lock, so racing workers can't pass the cap.
        SocialAccount.objects.select_for_update().filter(pk=actor_account.pk).first()
        capability = plug_capability_for(actor_account.platform)
        used = actions_in_last_day(actor_account, now)
        if used >= capability.max_actions_per_day:
            run.status = PlugRun.Status.BLOCKED
            run.detail = (
                f"{actor_account.account_name or 'The account'} already took {used} automated actions in the last "
                f"24 hours, the most {actor_account.get_platform_display()} gets, so this one was skipped."
            )
            run.save()
            service.record(
                workspace=rule.workspace,
                actor=who,
                action_type=action_type,
                operation=f"plug.{rule.action}",
                decision=Decision.BLOCKED,
                summary=f"Blocked because {run.detail[0].lower()}{run.detail[1:]}",
                account=actor_account,
                post=run.platform_post.post,
                platform_post=run.platform_post,
                plug_run=run,
                final_text=text,
            )
            return run
        verdict = service.evaluate(
            workspace=rule.workspace,
            actor=who,
            action_type=action_type,
            account=actor_account,
            text=text,
            when=now,
            source="system",
        )
        verdict = _with_platform_hold(verdict, rule, capability)
        links = {
            "workspace": rule.workspace,
            "actor": who,
            "action_type": action_type,
            "operation": f"plug.{rule.action}",
            "verdict": verdict,
            "account": actor_account,
            "post": run.platform_post.post,
            "platform_post": run.platform_post,
            "plug_run": run,
            "requested_at": now,
            "final_text": text,
        }
        if verdict.blocked:
            run.status, run.detail = PlugRun.Status.BLOCKED, verdict.summary
            run.save()
            service.record(**links)
        elif verdict.held:
            run.status, run.detail = PlugRun.Status.AWAITING_APPROVAL, verdict.summary
            run.save()
            service.record(**links)
        else:
            action = service.record(**links)
            if action.release_at is not None:
                run.status = PlugRun.Status.QUEUED
                run.detail = f"Goes out after {action.release_at:%Y-%m-%d %H:%M} UTC unless undone."
            else:
                run.status, perform_now = PlugRun.Status.FIRING, True
            run.save()
    if perform_now:
        perform(run)
    return run


def actions_in_last_day(account: SocialAccount, now=None) -> int:
    """Automated actions ``account`` took or has waiting for approval in the last 24 hours (a blocked one isn't one)."""
    now = now or timezone.now()
    return (
        AgentAction.objects.filter(
            plug_run__isnull=False,
            social_account=account,
            created_at__gte=now - DAY,
            decision__in=(Decision.EXECUTED, Decision.HELD),
        )
        .values("plug_run")
        .distinct()
        .count()
    )


def _with_platform_hold(verdict, rule: PlugRule, capability):
    """Turn an allowed action into one held for a person where the platform's rules want a person to decide."""
    if not verdict.executes:
        return verdict
    if capability.requires_approval:
        label = rule.acting_account.get_platform_display()
        reason = f"{label}'s API terms don't allow automated posting, so a person approves each one"
    elif rule.is_cross_account:
        reason = "another account boosting the post needs a person's approval on every network"
    else:
        return verdict
    row = {"name": "platform", "label": "Platform rules", "passed": False, "reason": reason, "blocking": False}
    return dataclasses.replace(
        verdict,
        outcome=service.HOLD,
        reasons=[reason, *verdict.reasons],
        results=[row, *verdict.results],
        undo_delay=dt.timedelta(0),
    )


def _actor_problem(rule: PlugRule) -> str:
    """Why the rule's acting account can't act, or "" when it can."""
    actor = rule.acting_account
    if actor.connection_status != SocialAccount.ConnectionStatus.CONNECTED:
        return f"{actor.account_name or 'The account'} is not connected."
    if actor.workspace_id != rule.workspace_id or actor.platform != rule.social_account.platform:
        return "The account that reposts is not in this workspace on the same network."
    if not plug_capability_for(actor.platform).allows(rule.action, cross_account=rule.is_cross_account):
        return "This network no longer offers that action."
    return ""


def perform(run: PlugRun) -> PlugRun:
    """Do the rule's action on the platform. ``run`` must already be claimed (``firing``); records how it went."""
    from apps.publisher.engine import _provider_and_access_token

    rule, post = run.rule, run.platform_post
    account = rule.acting_account
    run.attempts += 1
    target = PlugTarget(
        platform_post_id=post.platform_post_id,
        permalink=post.public_url() if post.status == PlatformPost.Status.PUBLISHED else "",
        cross_account=rule.is_cross_account,
    )
    try:
        provider, token = _provider_and_access_token(account)
        if rule.action == PlugAction.REPOST:
            outcome = provider.repost_post(token, target).platform_post_id
        elif rule.action == PlugAction.QUOTE:
            outcome = provider.quote_post(token, target, rule.message).platform_post_id
        else:
            outcome = provider.publish_comment(token, post.platform_post_id, rule.message).platform_comment_id
    except Exception as exc:
        return _failed(run, exc)
    run.status = PlugRun.Status.DONE
    run.fired_at = timezone.now()
    run.result_post_id = str(outcome or "")[:500]
    run.detail = {"repost": "Reposted.", "reply": "Replied.", "quote": "Quoted."}[rule.action]
    run.save()
    return run


def _failed(run: PlugRun, exc: Exception) -> PlugRun:
    """Record a failed attempt: unknown outcomes stay unknown, transient errors retry, the rest end."""
    message = _message_for(exc)
    if isinstance(exc, APIError) and exc.ambiguous_write:
        run.status = PlugRun.Status.UNCONFIRMED
        run.detail = f"{message} It may have gone out; check the account before trying again."
    elif isinstance(exc, ProviderError) and exc.retryable and run.attempts < MAX_ATTEMPTS:
        run.status = PlugRun.Status.RETRY
        run.next_check_at = timezone.now() + ACTION_RETRY_DELAY * run.attempts
        run.detail = f"{message} Trying again."
    else:
        run.status = PlugRun.Status.FAILED
        run.detail = message
    run.save()
    return run


def _message_for(exc: Exception) -> str:
    if isinstance(exc, NotImplementedError):
        return "This network can't do that."
    if isinstance(exc, ProviderError):
        return str(exc)[:300] or "The network refused the request."
    logger.exception("Plugs: unexpected failure", exc_info=exc)
    return "Something went wrong while acting."


# ---------------------------------------------------------------------------
# After the decision: the undo window, retries, approvals
# ---------------------------------------------------------------------------


def release_due(*, now=None) -> int:
    """Act on queued runs whose undo window ended, retry failed attempts and settle runs left mid-request."""
    now = now or timezone.now()
    done = 0
    stale = PlugRun.objects.filter(status=PlugRun.Status.FIRING, updated_at__lt=now - FIRING_STALE_AFTER)
    stale.update(
        status=PlugRun.Status.UNCONFIRMED,
        detail="The worker stopped while acting, so it is not known whether it went out.",
    )
    for pk in list(PlugRun.objects.filter(status=PlugRun.Status.QUEUED).values_list("pk", flat=True)):
        with transaction.atomic():
            run = (
                PlugRun.objects.select_for_update(skip_locked=True, of=("self",))
                .select_related("rule__social_account", "rule__actor_account", "rule__workspace", "platform_post")
                .filter(pk=pk, status=PlugRun.Status.QUEUED)
                .first()
            )
            if run is None:
                continue
            if not release.has_live_plug_action(run):
                _finish(run, PlugRun.Status.CANCELLED, "Undone before it went out.")
                continue
            if release.released_plug_action(run, now) is None:
                continue
            rule = run.rule
            blocker = release.plug_send_blocker(rule.workspace, rule.acting_account, _action_type(rule))
            if blocker:
                release.hold_plug_at_send(run, blocker)
                continue
            run.status = PlugRun.Status.FIRING
            run.save(update_fields=["status", "updated_at"])
        perform(run)
        done += 1
    retries = PlugRun.objects.filter(status=PlugRun.Status.RETRY, next_check_at__lte=now)
    for pk in list(retries.values_list("pk", flat=True)):
        with transaction.atomic():
            run = (
                PlugRun.objects.select_for_update(skip_locked=True, of=("self",))
                .select_related("rule__social_account", "rule__actor_account", "rule__workspace", "platform_post")
                .filter(pk=pk, status=PlugRun.Status.RETRY)
                .first()
            )
            if run is None:
                continue
            run.status = PlugRun.Status.FIRING
            run.save(update_fields=["status", "updated_at"])
        perform(run)
        done += 1
    return done


def approve_run(run: PlugRun, user) -> PlugRun:
    """A person approved a held automation: act now. The caller holds the review lock."""
    with transaction.atomic():
        run = (
            PlugRun.objects.select_for_update(of=("self",))
            .select_related("rule__social_account", "rule__actor_account", "rule__workspace", "platform_post")
            .get(pk=run.pk)
        )
        if run.status != PlugRun.Status.AWAITING_APPROVAL:
            raise PlugError("This is not waiting for approval.")
        problem = _actor_problem(run.rule)
        if problem:
            raise PlugError(problem)
        run.status = PlugRun.Status.FIRING
        run.save(update_fields=["status", "updated_at"])
    run = perform(run)
    if run.status in (PlugRun.Status.FAILED, PlugRun.Status.RETRY):
        raise PlugError(run.detail or "The network did not accept it.")
    return run


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def runs_for_workspace(workspace, *, status: str | None = None):
    qs = PlugRun.objects.filter(rule__workspace=workspace).select_related(
        "rule__social_account", "rule__actor_account", "platform_post__post"
    )
    return qs.filter(status=status) if status else qs


def rules_for_workspace(workspace):
    return PlugRule.objects.for_workspace(workspace.id).select_related(
        "social_account", "actor_account", "platform_post__post"
    )


def decision_of(run: PlugRun):
    """The latest audit entry for ``run`` (what the policy decided), or None."""
    from apps.autonomy.models import AgentAction

    return AgentAction.objects.filter(plug_run=run).order_by("-created_at").first()


__all__ = [
    "PlugError",
    "approve_run",
    "check_rules",
    "create_rule",
    "delete_rule",
    "fire",
    "perform",
    "release_due",
    "update_rule",
]
