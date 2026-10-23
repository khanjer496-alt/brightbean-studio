"""Evergreen recycling: re-post proven content into a queue's empty slots.

An :class:`EvergreenItem` is added from a published post (or a template or an
idea), with a minimum interval, an optional repeat cap and an optional "reword
with AI". :func:`fill_queues` is the periodic task: for each account it looks at
the next few open posting slots and, for each, picks the item that is due —
categories take turns, and within a category the item that went out longest ago
goes first — then makes a fresh post from it.

A repeat is never created when:

* the item is paused, has used up ``max_repeats``, or went out less than
  ``interval_days`` before the slot;
* its media is gone or no longer passes the platform's media rules;
* the organization's plan has no scheduled-post allowance left for that month,
  or the account hit its daily post cap;
* the account is disconnected, the organization is being deleted, or the
  workspace is archived;
* the original post was deleted here or is no longer published (the item is
  paused), or the network has stopped returning it (analytics keep failing on
  it, the only sign we get that it was deleted on the platform);
* the text doesn't fit the account's character limit.

When the workspace requires approval, a repeat is made as a draft and sent to
review instead of being scheduled, unless the item was pre-approved for
recycling by someone who can approve posts *and* nothing has changed since:
the content (media files included) still matches the fingerprint taken at
pre-approval and it isn't reworded by AI (new wording is new content). Only one
repeat per account waits for review at a time, so an unattended queue doesn't
fill with drafts.

Every repeat is recorded as an :class:`EvergreenRepeat` with the exact text
written and how approval was settled, the audit trail of what went out
unattended.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from django.db import transaction
from django.utils import timezone
from ninja.errors import HttpError

from apps.billing.entitlements import PlanLimitError, check_can_schedule_posts

from .models import EvergreenItem, EvergreenRepeat, Queue

logger = logging.getLogger(__name__)

# Only slots this close are filled, so a queue is topped up shortly before its
# posts go out and content the team queues meanwhile still gets its turn.
FILL_HORIZON = timedelta(days=3)
# A slot sooner than this is left alone: there is no time to review or fix it.
FILL_LEAD = timedelta(hours=1)
MAX_FILLS_PER_ACCOUNT = 3
# A repeat sent for review but never acted on stops blocking the next one after this long.
REVIEW_BACKLOG_WINDOW = timedelta(days=14)

MIN_INTERVAL_DAYS = 1
MAX_INTERVAL_DAYS = 3650
MAX_REPEATS_LIMIT = 1000
# Analytics failing on the original this many syncs in a row means the network
# no longer returns it: most likely deleted there.
ORIGINAL_GONE_FAILURES = 3
# Links, @mentions and #hashtags an AI rewording must keep, or the original is used.
_KEPT_TOKENS = re.compile(r"https?://\S+|[@#][\w.]+")

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class EvergreenError(ValueError):
    """The item can't be added or changed as asked; ``str(exc)`` says why."""


class SkipError(Exception):
    """This item can't be recycled right now; ``str(exc)`` is the reason kept on the item."""


@dataclass
class FillResult:
    scheduled: int = 0
    awaiting_approval: int = 0
    skipped: int = 0


# ---------------------------------------------------------------------------
# Adding and changing items
# ---------------------------------------------------------------------------


def clean_rules(interval_days, max_repeats):
    """Validated ``(interval_days, max_repeats)``; ``max_repeats`` of ``None`` or 0 means no limit."""
    try:
        interval_days = int(interval_days)
    except (TypeError, ValueError) as exc:
        raise EvergreenError("The minimum interval must be a whole number of days.") from exc
    if not MIN_INTERVAL_DAYS <= interval_days <= MAX_INTERVAL_DAYS:
        raise EvergreenError(f"The minimum interval must be between {MIN_INTERVAL_DAYS} and {MAX_INTERVAL_DAYS} days.")
    if max_repeats in (None, "", 0, "0"):
        return interval_days, None
    try:
        max_repeats = int(max_repeats)
    except (TypeError, ValueError) as exc:
        raise EvergreenError("Max repeats must be a whole number.") from exc
    if not 1 <= max_repeats <= MAX_REPEATS_LIMIT:
        raise EvergreenError(f"Max repeats must be between 1 and {MAX_REPEATS_LIMIT}.")
    return interval_days, max_repeats


def get_or_create_category(workspace, name):
    """The workspace's content category called ``name`` (created when new)."""
    from apps.composer.models import ContentCategory

    name = (name or "").strip()
    if not name:
        return None
    if len(name) > 100:
        raise EvergreenError("A category name can be at most 100 characters.")
    return ContentCategory.objects.get_or_create(workspace=workspace, name=name)[0]


def _new_item(
    *, workspace, social_account, user, category, interval_days, max_repeats, vary_with_ai, pre_approved, **content
):
    interval_days, max_repeats = clean_rules(interval_days, max_repeats)
    if not (content.get("caption") or content.get("media_asset_ids")):
        raise EvergreenError("There is nothing to recycle: the post has no text and no media.")
    item = EvergreenItem(
        workspace=workspace,
        social_account=social_account,
        category=category,
        interval_days=interval_days,
        max_repeats=max_repeats,
        vary_with_ai=vary_with_ai,
        created_by=user,
        **content,
    )
    set_pre_approval(item, pre_approved, user)
    return item


def add_from_platform_post(
    platform_post,
    *,
    user=None,
    category=None,
    interval_days=30,
    max_repeats=None,
    vary_with_ai=False,
    pre_approved=False,
) -> EvergreenItem:
    """Make an evergreen item from one published platform post (its account is the target)."""
    from apps.composer.models import PlatformPost

    post = platform_post.post
    if platform_post.status != PlatformPost.Status.PUBLISHED:
        raise EvergreenError("Only a published post can be added to evergreen.")
    if post.thread_parts.exists():
        raise EvergreenError("Threads can't be recycled yet.")
    rule = post.series_rule
    if rule is not None and rule.is_active:
        raise EvergreenError("This post already repeats on a schedule; stop the series before recycling it.")
    if post.evergreen_repeats.exists():
        raise EvergreenError("This post is itself an evergreen repeat; recycle the original instead.")
    if EvergreenItem.objects.filter(social_account=platform_post.social_account, source_post=post).exists():
        raise EvergreenError("This post is already in the evergreen library for that account.")
    item = _new_item(
        workspace=post.workspace,
        social_account=platform_post.social_account,
        user=user,
        category=category,
        interval_days=interval_days,
        max_repeats=max_repeats,
        vary_with_ai=vary_with_ai,
        pre_approved=pre_approved,
        source_kind=EvergreenItem.SourceKind.POST,
        source_post=post,
        title=platform_post.effective_title or "",
        caption=post.caption,
        first_comment=platform_post.effective_first_comment or "",
        tags=list(post.tags or []),
        media_asset_ids=[
            str(pk) for pk in post.media_attachments.order_by("position", "id").values_list("media_asset_id", flat=True)
        ],
        platform_caption=platform_post.platform_specific_caption,
        platform_extra=dict(platform_post.platform_extra or {}),
        last_posted_at=platform_post.published_at or post.published_at,
    )
    item.save()
    return item


def add_from_template(template, social_account, *, user=None, category=None, **rules) -> EvergreenItem:
    """Make an evergreen item from a post template, to be posted to ``social_account``."""
    data = template.template_data or {}
    item = _new_item(
        workspace=template.workspace,
        social_account=social_account,
        user=user,
        category=category,
        interval_days=rules.get("interval_days", 30),
        max_repeats=rules.get("max_repeats"),
        vary_with_ai=rules.get("vary_with_ai", False),
        pre_approved=rules.get("pre_approved", False),
        source_kind=EvergreenItem.SourceKind.TEMPLATE,
        title=template.name,
        caption=data.get("caption", ""),
        first_comment=data.get("first_comment", ""),
        tags=list(data.get("tags") or []),
        media_asset_ids=[str(i) for i in data.get("media_asset_ids") or []],
    )
    item.save()
    return item


def add_from_idea(idea, social_account, *, user=None, category=None, **rules) -> EvergreenItem:
    """Make an evergreen item from a kanban idea, to be posted to ``social_account``."""
    item = _new_item(
        workspace=idea.workspace,
        social_account=social_account,
        user=user,
        category=category,
        interval_days=rules.get("interval_days", 30),
        max_repeats=rules.get("max_repeats"),
        vary_with_ai=rules.get("vary_with_ai", False),
        pre_approved=rules.get("pre_approved", False),
        source_kind=EvergreenItem.SourceKind.IDEA,
        title=idea.title,
        caption=idea.description,
        tags=list(idea.tags or []),
        media_asset_ids=[str(idea.media_asset_id)] if idea.media_asset_id else [],
    )
    item.save()
    return item


def content_fingerprint(item) -> str:
    """A hash of everything a repeat would publish, media files included."""
    from apps.media_library.models import MediaAsset

    files = {
        str(pk): name
        for pk, name in MediaAsset.objects.filter(
            id__in=item.media_asset_ids or [], workspace_id=item.workspace_id
        ).values_list("id", "file")
    }
    payload = {
        "title": item.title,
        "caption": item.caption,
        "platform_caption": item.platform_caption,
        "first_comment": item.first_comment,
        "platform_extra": item.platform_extra,
        "media": [[str(i), files.get(str(i))] for i in item.media_asset_ids or []],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def set_pre_approval(item, approved: bool, user) -> None:
    """Pre-approve ``item`` for recycling (fingerprinting its content as of now) or withdraw it. Not saved."""
    item.pre_approved = approved
    item.pre_approved_by = user if approved else None
    item.approved_fingerprint = content_fingerprint(item) if approved else ""


def set_paused(item, paused: bool) -> EvergreenItem:
    if item.is_paused != paused:
        item.is_paused = paused
        item.save(update_fields=["is_paused", "updated_at"])
    return item


# ---------------------------------------------------------------------------
# Selection rules
# ---------------------------------------------------------------------------


def is_due(item, when) -> bool:
    """Whether ``item`` may go out at ``when``: active, repeats left, interval since it last went out passed."""
    if item.is_paused:
        return False
    if item.max_repeats is not None and item.repeat_count >= item.max_repeats:
        return False
    due_at = item.next_eligible_at
    return due_at is None or due_at <= when


def choose_next(items, rotation):
    """Pick the item to post next from the due ``items``.

    ``rotation`` maps a category id to when that category last supplied a post;
    the category waiting longest (never-used first) goes first, and inside it
    the item that last went out longest ago.
    """
    by_category = defaultdict(list)
    for item in items:
        by_category[item.category_id].append(item)
    category = min(by_category, key=lambda cid: (rotation.get(cid) or _EPOCH, str(cid)))
    return min(by_category[category], key=lambda i: (i.last_posted_at or _EPOCH, str(i.id)))


# ---------------------------------------------------------------------------
# Recycling
# ---------------------------------------------------------------------------


def _assets(item):
    """The item's media assets in order, or a reason they can't be used."""
    from apps.media_library.models import MediaAsset

    ids = item.media_asset_ids or []
    if not ids:
        return []
    found = {str(a.id): a for a in MediaAsset.objects.filter(id__in=ids, workspace_id=item.workspace_id)}
    if any(str(i) not in found for i in ids):
        raise SkipError("Some of its media was deleted.")
    return [found[str(i)] for i in ids]


def media_problem(item) -> str:
    """Why the item's media can't be posted to its account today, or "" when it can."""
    from apps.composer.services import media_rules_blocker, publish_blocker, usable_media_types

    try:
        assets = _assets(item)
    except SkipError as exc:
        return str(exc)
    return publish_blocker(
        item.social_account, media_types=usable_media_types(assets), platform_extra=item.platform_extra
    ) or media_rules_blocker(item.social_account, assets, platform_extra=item.platform_extra)


def original_problem(item) -> tuple[str, bool]:
    """``(reason, permanent)`` when the post an item came from is gone, else ``("", False)``.

    A permanent problem pauses the item: the original was deleted here or is
    no longer published. Analytics failing on it repeatedly is the only sign
    of deletion on the platform; that only skips, as it may recover.
    """
    from apps.composer.models import PlatformPost

    if item.source_kind != EvergreenItem.SourceKind.POST:
        return "", False
    if item.source_post_id is None:
        return "The original post was deleted.", True
    original = (
        PlatformPost.objects.filter(post_id=item.source_post_id, social_account_id=item.social_account_id)
        .only("status", "analytics_failure_count")
        .first()
    )
    if original is None or original.status != PlatformPost.Status.PUBLISHED:
        return "The original post is no longer published.", True
    if original.analytics_failure_count >= ORIGINAL_GONE_FAILURES:
        return (
            f"{item.social_account.get_platform_display()} no longer returns the original post; it may have been "
            "deleted there.",
            False,
        )
    return "", False


def _keeps_tokens(original: str, reworded: str) -> bool:
    """Whether ``reworded`` still has every link, @mention and #hashtag of ``original``."""
    return set(_KEPT_TOKENS.findall(original)) <= set(_KEPT_TOKENS.findall(reworded))


def _reworded(item):
    """``(text, wording)``: the AI's rewording when asked for and available, else the original."""
    from apps.ai_writer.client import AIError
    from apps.ai_writer.models import AIRequestLog
    from apps.ai_writer.services import AIContext, vary_caption

    base = item.platform_caption if item.platform_caption is not None else item.caption
    if not item.vary_with_ai or not base.strip():
        return base, EvergreenRepeat.Wording.ORIGINAL
    ctx = AIContext(workspace=item.workspace, user=None, source=AIRequestLog.Source.SYSTEM)
    try:
        text = vary_caption(ctx, base, item.social_account)
        if text.strip() and _keeps_tokens(base, text):
            return text, EvergreenRepeat.Wording.AI
        logger.info("Evergreen %s posted as written: the rewording dropped a link, mention or hashtag", item.pk)
        return base, EvergreenRepeat.Wording.ORIGINAL
    except (AIError, PlanLimitError, ValueError) as exc:
        logger.info("Evergreen %s posted as written: %s", item.pk, exc)
    except Exception:
        # Rewording is a nicety: whatever the provider does, the repeat still goes out as written.
        logger.exception("Evergreen %s posted as written: the AI request failed unexpectedly", item.pk)
    return base, EvergreenRepeat.Wording.ORIGINAL


def _approval_basis(item) -> str:
    """How a repeat of ``item`` is approved: not needed, pre-approved, or sent for review.

    Pre-approval only covers the content as it was approved: an AI rewording
    or any change since (its fingerprint) goes to review.
    """
    from apps.approvals.policy import APPROVAL_REQUIRED_MODES

    if getattr(item.workspace, "approval_workflow_mode", "none") not in APPROVAL_REQUIRED_MODES:
        return EvergreenRepeat.Approval.NOT_REQUIRED
    if item.pre_approved and not item.vary_with_ai and item.approved_fingerprint == content_fingerprint(item):
        return EvergreenRepeat.Approval.PRE_APPROVED
    return EvergreenRepeat.Approval.REVIEW


def _needs_approval(item) -> bool:
    return _approval_basis(item) == EvergreenRepeat.Approval.REVIEW


def _review_backlog(account, now) -> bool:
    """Whether a repeat for ``account`` is still waiting on review."""
    return EvergreenRepeat.objects.filter(
        item__social_account=account,
        outcome=EvergreenRepeat.Outcome.AWAITING_APPROVAL,
        created_at__gte=now - REVIEW_BACKLOG_WINDOW,
        platform_post__status__in=("draft", "pending_review", "pending_client", "changes_requested", "approved"),
    ).exists()


def _make_post(item, queue, slot_dt, text, wording, needs_approval):
    """Create the repeat's post and give it its slot (or send it for review). Raises ``SkipError`` to roll back."""
    from apps.approvals.policy import mark_final_approval
    from apps.approvals.services import submit_for_review
    from apps.composer import services as composer_services
    from apps.composer.models import PlatformPost

    from . import services as queue_services

    account = item.social_account
    post = composer_services.create_post(
        workspace=item.workspace,
        social_account=account,
        caption=item.caption if item.platform_caption is not None else text,
        media_asset_ids=item.media_asset_ids,
        title=item.title,
        first_comment=item.first_comment,
        author=item.created_by,
        status="draft",
        proposed_publish_at=slot_dt if needs_approval else None,
    )
    post.tags = list(item.tags or [])
    post.category = item.category
    post.save(update_fields=["tags", "category", "updated_at"])
    pp = post.platform_posts.get(social_account=account)
    pp.platform_extra = dict(item.platform_extra or {})
    if item.platform_caption is not None:
        pp.platform_specific_caption = text
    pp.save(update_fields=["platform_extra", "platform_specific_caption", "updated_at"])

    if needs_approval:
        if item.created_by is not None:
            try:
                submit_for_review(pp, item.created_by, item.workspace)
            except ValueError:
                logger.info("Evergreen %s left as a draft: couldn't be sent for review", item.pk)
        pp.refresh_from_db()
        return pp, EvergreenRepeat.Outcome.AWAITING_APPROVAL, slot_dt

    if _approval_basis(item) == EvergreenRepeat.Approval.PRE_APPROVED:
        mark_final_approval(pp)
    queue_services._lock_channel(queue)
    if slot_dt in queue_services._occupied_datetimes(account):
        raise SkipError("The slot was taken by another post.")
    entry = queue_services._ensure_entry(post, queue)
    queue_services._write_slot(entry, pp, slot_dt)
    pp.refresh_from_db()
    try:
        composer_services.transition_platform_post(pp, PlatformPost.Status.SCHEDULED, scheduled_at=slot_dt)
    except ValueError as exc:
        raise SkipError(str(exc)) from exc
    return pp, EvergreenRepeat.Outcome.SCHEDULED, slot_dt


def recycle(item, queue, slot_dt, *, now=None) -> EvergreenRepeat:
    """Post ``item`` again at ``slot_dt`` through ``queue``; raises ``SkipError`` / ``PlanLimitError`` when it can't."""
    from apps.api.limits import check_platform_quota
    from apps.social_accounts.models import SocialAccount

    now = now or timezone.now()
    if item.social_account.connection_status != SocialAccount.ConnectionStatus.CONNECTED:
        raise SkipError("The account is disconnected.")
    problem, permanent = original_problem(item)
    if problem:
        if permanent:
            EvergreenItem.objects.filter(pk=item.pk).update(is_paused=True)
            item.is_paused = True
            problem += " Paused."
        raise SkipError(problem)
    problem = media_problem(item)
    if problem:
        raise SkipError(problem)
    approval = _approval_basis(item)
    needs_approval = approval == EvergreenRepeat.Approval.REVIEW
    if not needs_approval:
        check_can_schedule_posts(item.workspace.organization, count=1, when=slot_dt)
        try:
            check_platform_quota(item.social_account)
        except HttpError as exc:
            raise SkipError(exc.message) from exc
    text, wording = _reworded(item)
    account = item.social_account
    if account.caption_wire_length(text) > account.char_limit:
        raise SkipError(f"The text is longer than {account.get_platform_display()} allows ({account.char_limit}).")
    with transaction.atomic():
        locked = (
            EvergreenItem.objects.select_for_update(of=("self",))
            .select_related("workspace__organization", "social_account", "category", "created_by")
            .get(pk=item.pk)
        )
        if not is_due(locked, slot_dt):
            raise SkipError("It was already recycled.")
        pp, outcome, when = _make_post(locked, queue, slot_dt, text, wording, needs_approval)
        locked.repeat_count += 1
        locked.last_posted_at = when
        locked.last_recycled_at = now
        locked.last_skip_reason = ""
        locked.save(
            update_fields=["repeat_count", "last_posted_at", "last_recycled_at", "last_skip_reason", "updated_at"]
        )
        repeat = EvergreenRepeat.objects.create(
            item=locked,
            post=pp.post,
            platform_post=pp,
            scheduled_for=when,
            outcome=outcome,
            wording=wording,
            text=text,
            approval=approval,
        )
    logger.info(
        "Evergreen %s: repeat %s %s for %s on account %s (%s, %s)",
        item.pk,
        repeat.pk,
        outcome,
        when.isoformat(),
        account.pk,
        wording,
        approval,
    )
    for field in ("repeat_count", "last_posted_at", "last_recycled_at", "last_skip_reason"):
        setattr(item, field, getattr(locked, field))
    return repeat


def _active_queue(account):
    return Queue.objects.filter(social_account=account, is_active=True).order_by("created_at", "id").first()


def fill_account(account, *, now=None) -> FillResult:
    """Fill ``account``'s open slots in the next few days from its evergreen items."""
    from . import services as queue_services

    now = now or timezone.now()
    result = FillResult()
    queue = _active_queue(account)
    if queue is None:
        return result
    items = list(
        EvergreenItem.objects.filter(social_account=account)
        .select_related("workspace__organization", "social_account", "category", "created_by")
        .order_by("id")
    )
    if not any(not i.is_paused for i in items):
        return result
    rotation: dict = {}
    for i in items:
        if i.last_recycled_at and i.last_recycled_at > (rotation.get(i.category_id) or _EPOCH):
            rotation[i.category_id] = i.last_recycled_at

    slots = [
        s
        for s in queue_services.next_available_slots(account, MAX_FILLS_PER_ACCOUNT, after=now + FILL_LEAD)
        if s <= now + FILL_HORIZON
    ]
    blocked: set = set()
    for slot_dt in slots:
        due = [i for i in items if i.pk not in blocked and is_due(i, slot_dt)]
        while due:
            item = choose_next(due, rotation)
            if _needs_approval(item) and _review_backlog(account, now):
                return result
            try:
                repeat = recycle(item, queue, slot_dt, now=now)
            except SkipError as exc:
                item.last_skip_reason = str(exc)[:255]
                EvergreenItem.objects.filter(pk=item.pk).update(last_skip_reason=item.last_skip_reason)
                due.remove(item)
                blocked.add(item.pk)
                result.skipped += 1
                continue
            except PlanLimitError as exc:
                logger.info("Evergreen stopped for %s: %s", account.pk, exc.message)
                return result
            except queue_services.QueueFullError:
                return result
            rotation[item.category_id] = now
            if repeat.outcome == EvergreenRepeat.Outcome.SCHEDULED:
                result.scheduled += 1
            else:
                result.awaiting_approval += 1
            break
    return result


def fill_queues(*, now=None) -> FillResult:
    """The periodic task: fill every account that has active evergreen items. One account failing never stops the rest."""
    from apps.social_accounts.models import SocialAccount

    now = now or timezone.now()
    total = FillResult()
    accounts = SocialAccount.objects.filter(
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        evergreen_items__is_paused=False,
        workspace__is_archived=False,
        workspace__organization__deletion_requested_at__isnull=True,
        workspace__organization__deleted_at__isnull=True,
    ).distinct()
    for account in accounts:
        try:
            part = fill_account(account, now=now)
        except Exception:
            logger.exception("Evergreen fill failed for account %s", account.pk)
            continue
        total.scheduled += part.scheduled
        total.awaiting_approval += part.awaiting_approval
        total.skipped += part.skipped
    return total


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def repeat_performance(repeats) -> dict:
    """``{repeat id: {"engagements", "reach", "rate"}}`` from each repeat's latest post insights.

    ``reach`` is the first of views, reach, impressions or plays the network
    reports (``None`` when none), ``rate`` the engagement rate as a percentage
    (``None`` when it can't be measured). A repeat that hasn't published or
    synced yet has no entry.
    """
    from apps.analytics.best_times import _latest_metrics, post_engagement_rate
    from apps.analytics.metrics import ENGAGEMENT_DENOMINATORS, ENGAGEMENT_PARTS

    repeats = [r for r in repeats if r.platform_post_id]
    metrics = _latest_metrics([r.platform_post_id for r in repeats])
    out = {}
    for r in repeats:
        m = metrics.get(r.platform_post_id)
        if not m:
            continue
        rate = post_engagement_rate(m, r.item.social_account.follower_count)
        out[r.id] = {
            "engagements": int(sum(m.get(k, 0) for k in ENGAGEMENT_PARTS)),
            "reach": next((int(m[d]) for d in ENGAGEMENT_DENOMINATORS if m.get(d)), None),
            "rate": None if rate is None else round(rate * 100, 2),
        }
    return out
