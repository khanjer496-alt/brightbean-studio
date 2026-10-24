"""RSS auto-posting: poll a feed, decide what to do with each new item, and make the posts.

:func:`poll_due` is the periodic task. For each active feed it asks the server only for what changed
(``ETag`` / ``Last-Modified``), records every item once (:class:`FeedItem`, unique per feed), and for each
new, matching item makes one post per target account:

* link mode posts the title and link; AI modes write the post in the workspace's brand voice (and, in
  ``ai_image`` mode, attach the article's own image) and fall back to the plain link whenever the AI is
  unavailable, over its limit or fails;
* the destination is the account's next free queue slot, a fixed delay from now, or a draft;
* the post goes through the autonomy policy as the "system" actor: blocked, held (saved as a draft that
  Activity can approve), or allowed, and the workspace's approval workflow is never skipped.

A feed that was just added never floods the accounts: the first poll records the items already there and
only items published after the feed was added are posted. A feed that fails backs off exponentially.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.ai_writer.client import AIError
from apps.ai_writer.models import AIRequestLog
from apps.ai_writer.services import AIContext, truncate_to_limit, write_feed_post
from apps.autonomy import service as autonomy_service
from apps.autonomy.actors import system
from apps.autonomy.models import ActionType, Decision
from apps.billing.entitlements import PlanLimitError, check_can_schedule_posts
from apps.calendar import services as queue_services
from apps.calendar.models import Queue
from apps.composer import services as composer_services
from apps.composer.models import Feed, PlatformPost
from apps.social_accounts.models import SocialAccount

from . import feeds
from .models import FeedAutopost, FeedItem

logger = logging.getLogger(__name__)

POLL_INTERVAL = dt.timedelta(minutes=30)
BACKOFF_CAP = dt.timedelta(hours=24)
# A poll claims the feed this long, so two workers never read it together.
CLAIM = dt.timedelta(minutes=10)
MAX_ITEMS_PER_POLL = 5
MAX_KEYWORDS = 20
MAX_KEYWORD_LENGTH = 60
MAX_FEEDS_PER_WORKSPACE = 20
MAX_DAILY = 50
MIN_DELAY_MINUTES = 1
MAX_DELAY_MINUTES = 7 * 24 * 60
WHO = "RSS autopost"

# An article already shared to an account from another of the workspace's feeds this recently is not shared again.
DUPLICATE_WINDOW = dt.timedelta(days=7)
_MADE = ("scheduled", "draft", "held")
_URL = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)

IMAGE_EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}


# A person's "check now" is refused when the feed was read this recently.
CHECK_NOW_COOLDOWN = dt.timedelta(minutes=1)


class AutopostError(ValueError):
    """A setting or request the caller can't have; the message is written for the person."""


class TooSoonError(AutopostError):
    """The feed was read a moment ago."""


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def clean_keywords(raw) -> list[str]:
    """Keywords from a list or a comma / line separated string: lowercase, trimmed, unique, bounded."""
    if isinstance(raw, str):
        raw = raw.replace("\n", ",").split(",")
    out: list[str] = []
    for item in raw or []:
        word = " ".join(str(item).split()).lower()
        if not word:
            continue
        if len(word) > MAX_KEYWORD_LENGTH:
            raise AutopostError(f"A keyword can be at most {MAX_KEYWORD_LENGTH} characters.")
        if word not in out:
            out.append(word)
    if len(out) > MAX_KEYWORDS:
        raise AutopostError(f"At most {MAX_KEYWORDS} keywords are allowed per list.")
    return out


def clean_settings(
    workspace, *, accounts, mode, destination, delay_minutes, max_posts_per_day, include, exclude
) -> dict:
    """Validated settings, or :class:`AutopostError` saying what to change."""
    if mode not in FeedAutopost.Mode.values:
        raise AutopostError("Choose how the posts are written.")
    if destination not in FeedAutopost.Destination.values:
        raise AutopostError("Choose where the posts go.")
    accounts = list(accounts)
    if not accounts:
        raise AutopostError("Choose at least one account to post to.")
    for account in accounts:
        if account.workspace_id != workspace.id:
            raise AutopostError("Every account must be in this workspace.")
        if account.connection_status != SocialAccount.ConnectionStatus.CONNECTED:
            raise AutopostError(f"{account.account_name or 'An account'} is not connected.")
    try:
        delay_minutes = int(delay_minutes)
        max_posts_per_day = int(max_posts_per_day)
    except (TypeError, ValueError) as exc:
        raise AutopostError("The delay and the daily limit must be whole numbers.") from exc
    if not MIN_DELAY_MINUTES <= delay_minutes <= MAX_DELAY_MINUTES:
        raise AutopostError("The delay must be between 1 minute and 7 days.")
    if not 1 <= max_posts_per_day <= MAX_DAILY:
        raise AutopostError(f"The daily limit must be between 1 and {MAX_DAILY}.")
    return {
        "accounts": accounts,
        "mode": mode,
        "destination": destination,
        "delay_minutes": delay_minutes,
        "max_posts_per_day": max_posts_per_day,
        "include_keywords": clean_keywords(include),
        "exclude_keywords": clean_keywords(exclude),
    }


def create_feed(workspace, user, *, name: str, url: str, website_url: str = "") -> Feed:
    """A new feed subscription after checking that its address is a public RSS/Atom feed."""
    from django.core.exceptions import ValidationError
    from django.core.validators import URLValidator

    url = (url or "").strip()
    try:
        URLValidator()(url)
    except ValidationError as exc:
        raise AutopostError("Enter the feed's web address.") from exc
    existing = Feed.objects.for_workspace(workspace.id).filter(url=url).first()
    if existing is not None:
        return existing
    if Feed.objects.for_workspace(workspace.id).count() >= MAX_FEEDS_PER_WORKSPACE:
        raise AutopostError(f"A workspace can follow at most {MAX_FEEDS_PER_WORKSPACE} feeds.")
    checked = feeds.fetch(url)
    if checked.outcome != "ok":
        raise AutopostError(checked.error or "The address could not be read as a feed.")
    return Feed.objects.create(
        workspace=workspace,
        name=(name or checked.title or url).strip()[:255],
        url=url,
        website_url=(website_url or checked.website_url or "")[:500],
        added_by=user,
    )


def enable(workspace, feed: Feed, user, **settings) -> FeedAutopost:
    """Turn auto-posting on for ``feed``. Only items published from now on are posted."""
    if feed.workspace_id != workspace.id:
        raise AutopostError("That feed is not in this workspace.")
    cleaned = clean_settings(workspace, **settings)
    accounts = cleaned.pop("accounts")
    existing = FeedAutopost.objects.filter(feed=feed).first()
    if existing is not None:
        raise AutopostError("This feed already posts automatically; change its settings instead.")
    autopost = FeedAutopost.objects.create(
        workspace=workspace,
        feed=feed,
        created_by=user,
        started_at=timezone.now(),
        next_check_at=timezone.now(),
        **cleaned,
    )
    autopost.accounts.set(accounts)
    return autopost


def update(autopost: FeedAutopost, *, is_active: bool | None = None, **settings) -> FeedAutopost:
    """Change settings (all of them, validated together) and/or switch the feed on or off."""
    if settings:
        current = {
            "accounts": autopost.accounts.all(),
            "mode": autopost.mode,
            "destination": autopost.destination,
            "delay_minutes": autopost.delay_minutes,
            "max_posts_per_day": autopost.max_posts_per_day,
            "include": autopost.include_keywords,
            "exclude": autopost.exclude_keywords,
        }
        current.update(settings)
        cleaned = clean_settings(autopost.workspace, **current)
        accounts = cleaned.pop("accounts")
        for key, value in cleaned.items():
            setattr(autopost, key, value)
        autopost.accounts.set(accounts)
    if is_active is not None:
        if is_active and not autopost.is_active:
            # Switched back on after a pause: items from the pause are not posted, only newer ones.
            autopost.started_at = timezone.now()
            autopost.baselined_at = None
            autopost.consecutive_failures = 0
            autopost.next_check_at = timezone.now()
        autopost.is_active = is_active
    autopost.save()
    return autopost


def disable(autopost: FeedAutopost) -> None:
    """Stop auto-posting; the feed stays followed and its history is kept with the autopost's removal."""
    autopost.delete()


# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------


@dataclass
class PollResult:
    status: str = "ok"  # ok, not_modified, error, skipped
    new_items: int = 0
    posted: int = 0
    error: str = ""


def poll_due(*, now=None) -> int:
    """The periodic task: poll every active feed that is due. One failing feed never stops the others."""
    now = now or timezone.now()
    due = FeedAutopost.objects.filter(
        is_active=True,
        workspace__is_archived=False,
        workspace__organization__deletion_requested_at__isnull=True,
        workspace__organization__deleted_at__isnull=True,
    ).filter(Q(next_check_at__isnull=True) | Q(next_check_at__lte=now))
    polled = 0
    for pk in list(due.values_list("pk", flat=True)):
        try:
            result = poll(FeedAutopost.objects.get(pk=pk), now=now)
        except Exception:
            logger.exception("Feed autopost %s failed", pk)
            continue
        polled += result.status != "skipped"
    return polled


def check_now(autopost: FeedAutopost, *, now=None) -> PollResult:
    """Read the feed now at a person's (or agent's) request, at most once per ``CHECK_NOW_COOLDOWN``."""
    now = now or timezone.now()
    if autopost.last_checked_at and now - autopost.last_checked_at < CHECK_NOW_COOLDOWN:
        raise TooSoonError("It was checked a moment ago. Try again in a minute.")
    return poll(autopost, now=now, force=True)


def _claim(autopost: FeedAutopost, now) -> bool:
    """Take the feed for this poll with a compare-and-set, so two workers never read it together."""
    claimed = FeedAutopost.objects.filter(pk=autopost.pk, next_check_at=autopost.next_check_at).update(
        next_check_at=now + CLAIM
    )
    return bool(claimed)


def poll(autopost: FeedAutopost, *, now=None, force: bool = False) -> PollResult:
    """Read the feed and handle its new items. ``force`` is a person's "check now": it ignores the schedule."""
    now = now or timezone.now()
    autopost = FeedAutopost.objects.select_related("feed", "workspace__organization").get(pk=autopost.pk)
    if not autopost.is_active:
        return PollResult("skipped")
    if not force and autopost.next_check_at and autopost.next_check_at > now:
        return PollResult("skipped")
    if not _claim(autopost, now):
        return PollResult("skipped")
    fetched = feeds.fetch(autopost.feed.url, etag=autopost.etag, last_modified=autopost.last_modified)
    autopost.last_checked_at = now
    if fetched.outcome == "error":
        return _failed(autopost, fetched.error, now)
    result = PollResult(fetched.outcome)
    if fetched.outcome == "ok":
        autopost.etag, autopost.last_modified = fetched.etag, fetched.last_modified
        _handle_entries(autopost, fetched.entries, now, result)
    autopost.consecutive_failures = 0
    # A plan limit that stopped the run is shown next to the feed; a clean run clears it.
    autopost.last_error = result.error
    autopost.last_success_at = now
    autopost.next_check_at = now + POLL_INTERVAL
    autopost.save(
        update_fields=[
            "etag",
            "last_modified",
            "last_checked_at",
            "last_success_at",
            "next_check_at",
            "consecutive_failures",
            "last_error",
            "baselined_at",
            "updated_at",
        ]
    )
    return result


def _failed(autopost: FeedAutopost, error: str, now) -> PollResult:
    autopost.consecutive_failures += 1
    autopost.last_error = error[:300]
    autopost.next_check_at = now + min(POLL_INTERVAL * (2 ** min(autopost.consecutive_failures, 10)), BACKOFF_CAP)
    autopost.save(
        update_fields=["last_checked_at", "next_check_at", "consecutive_failures", "last_error", "updated_at"]
    )
    return PollResult("error", error=error)


def _handle_entries(autopost: FeedAutopost, entries: list[feeds.Entry], now, result: PollResult) -> None:
    seen = set(autopost.items.filter(key__in=[e.key for e in entries]).values_list("key", flat=True))
    fresh = [e for e in entries if e.key not in seen]
    # Oldest first, so a burst goes out in the order it was published.
    fresh.sort(key=lambda e: e.published_at or now)
    first_run = autopost.baselined_at is None
    candidates: list[feeds.Entry] = []
    for entry in fresh:
        if entry.published_at is not None and entry.published_at < autopost.started_at:
            _record_item(autopost, entry, FeedItem.Status.OLDER, "Published before this feed was added.")
        elif first_run and entry.published_at is None:
            # No date and already there on the first read: can't tell it is new, so it is not posted.
            _record_item(autopost, entry, FeedItem.Status.OLDER, "Already in the feed when it was added.")
        else:
            candidates.append(entry)
    if first_run:
        autopost.baselined_at = now
    result.new_items = len(candidates)
    posted_today = _posted_in_last_day(autopost, now)
    handled = 0
    for entry in candidates:
        if handled >= MAX_ITEMS_PER_POLL or posted_today >= autopost.max_posts_per_day:
            # Left unrecorded: still in the feed next time, when the day's allowance has room.
            break
        try:
            item = _record_item(autopost, entry, FeedItem.Status.PROCESSING, "")
        except IntegrityError:
            continue
        handled += 1
        try:
            used = _process(autopost, item, entry, now)
        except PlanLimitError as exc:
            item.delete()
            result.error = exc.message[:300]
            logger.info("Feed autopost %s stopped: %s", autopost.pk, exc.message)
            break
        except Exception:
            logger.exception("Feed autopost item %s failed", item.pk)
            _settle(item, FeedItem.Status.FAILED, "Something went wrong while making the post.", item.outcomes)
            continue
        posted_today += used
        result.posted += used


def _record_item(autopost, entry, status, reason) -> FeedItem:
    with transaction.atomic():
        return FeedItem.objects.create(
            autopost=autopost,
            key=entry.key,
            title=entry.title,
            link=entry.link,
            published_at=entry.published_at,
            status=status,
            reason=reason,
        )


def _posted_in_last_day(autopost: FeedAutopost, now) -> int:
    return autopost.items.filter(
        created_at__gte=now - dt.timedelta(days=1),
        status__in=[FeedItem.Status.SCHEDULED, FeedItem.Status.DRAFT, FeedItem.Status.HELD],
    ).count()


def matches(entry: feeds.Entry, include: list[str], exclude: list[str]) -> str:
    """Why ``entry`` is filtered out, or "" when it passes the keyword filters."""
    text = f"{entry.title} {entry.summary}".lower()
    hit = next((w for w in exclude if w in text), "")
    if hit:
        return f'Contains the excluded keyword "{hit}".'
    if include and not any(w in text for w in include):
        return "Contains none of the keywords to include."
    return ""


# ---------------------------------------------------------------------------
# Making the posts
# ---------------------------------------------------------------------------


def link_caption(account, title: str, link: str) -> str | None:
    """The title and link within the account's limit (title trimmed to fit), or None when the link alone is too long."""
    if not link:
        return title if account.caption_wire_length(title) <= account.char_limit else None
    suffix = f"\n\n{link}"
    if not title:
        return link if account.caption_wire_length(link) <= account.char_limit else None
    fitted = truncate_to_limit(title, account.char_limit, lambda t: account.caption_wire_length(t + suffix))
    return fitted + suffix if fitted and account.caption_wire_length(fitted + suffix) <= account.char_limit else None


def _write(autopost, entry, account) -> tuple[str | None, bool, str]:
    """``(text, ai_used, note)``: the AI's post when asked for and it works, else the plain link post."""
    plain = link_caption(account, entry.title, entry.link)
    if autopost.mode == FeedAutopost.Mode.LINK:
        return plain, False, ""
    ctx = AIContext(workspace=autopost.workspace, user=None, source=AIRequestLog.Source.SYSTEM)
    try:
        text = write_feed_post(ctx, title=entry.title, summary=entry.summary, url=entry.link, account=account)
        if _foreign_links(text, entry.link):
            # The article text is data, never instructions: a post pointing anywhere but the article is not used.
            raise ValueError("the AI's post linked somewhere other than the article")
        if entry.link and entry.link not in text:
            with_link = f"{text}\n\n{entry.link}"
            text = with_link if account.caption_wire_length(with_link) <= account.char_limit else plain or text
        if account.caption_wire_length(text) > account.char_limit:
            raise ValueError("too long")
        return text, True, ""
    except (AIError, PlanLimitError, ValueError) as exc:
        note = getattr(exc, "message", str(exc))
        logger.info("Feed autopost %s: AI unavailable, linking instead: %s", autopost.pk, note)
        return plain, False, f"The AI writer was unavailable, so the link was posted as is ({note})."
    except Exception:
        logger.exception("Feed autopost %s: the AI request failed unexpectedly", autopost.pk)
        return plain, False, "The AI writer failed, so the link was posted as is."


def _foreign_links(text: str, link: str) -> list[str]:
    """Web addresses in ``text`` other than the article's own ``link``."""
    allowed = link.rstrip("/.,").lower()
    return [u for u in _URL.findall(text or "") if u.rstrip("/.,").lower() != allowed]


def _already_shared(autopost: FeedAutopost, entry: feeds.Entry, now) -> set:
    """Ids of accounts that got this article from another of the workspace's feeds in the last week."""
    if not entry.link:
        return set()
    others = FeedItem.objects.filter(
        autopost__workspace=autopost.workspace,
        link=entry.link,
        created_at__gte=now - DUPLICATE_WINDOW,
    ).exclude(autopost=autopost)
    return {
        o.get("account_id")
        for outcomes in others.values_list("outcomes", flat=True)
        for o in outcomes or []
        if o.get("status") in _MADE and o.get("account_id")
    }


def _image_asset(autopost: FeedAutopost, entry: feeds.Entry, key: str):
    """The article's image as a media-library asset, or None (with the post going out without it)."""
    from apps.media_library.services import create_asset

    url = entry.image_url or (feeds.article_image(entry.link) if entry.link else "")
    downloaded = feeds.download_image(url) if url else None
    if downloaded is None:
        return None
    content, content_type = downloaded
    extension = IMAGE_EXTENSIONS.get(content_type, "jpg")
    upload = SimpleUploadedFile(f"feed-{key[:12]}.{extension}", content, content_type=content_type)
    try:
        return create_asset(
            autopost.workspace.organization,
            autopost.workspace,
            upload,
            autopost.created_by,
            alt_text=entry.title[:200],
            title=entry.title[:200],
        )
    except Exception:
        logger.info("Feed autopost %s: the article image was refused", autopost.pk, exc_info=True)
        return None


def _process(autopost: FeedAutopost, item: FeedItem, entry: feeds.Entry, now) -> int:
    """Make the posts for one item. Returns 1 when something was made (counts against the day's allowance)."""
    why = matches(entry, autopost.include_keywords, autopost.exclude_keywords)
    if why:
        _settle(item, FeedItem.Status.SKIPPED, why, [])
        return 0
    accounts = list(
        autopost.accounts.filter(
            connection_status=SocialAccount.ConnectionStatus.CONNECTED, workspace=autopost.workspace
        )
    )
    if not accounts:
        _settle(item, FeedItem.Status.FAILED, "None of the chosen accounts is connected.", [])
        return 0
    shared = _already_shared(autopost, entry, now)
    if shared:
        accounts = [a for a in accounts if str(a.id) not in shared]
        if not accounts:
            _settle(item, FeedItem.Status.SKIPPED, "Already shared to these accounts from another feed this week.", [])
            return 0
    asset = None
    notes: list[str] = []
    if autopost.mode == FeedAutopost.Mode.AI_IMAGE:
        asset = _image_asset(autopost, entry, item.key)
        if asset is None:
            notes.append("No usable image was found, so it was posted without one.")
    outcomes = []
    ai_used = False
    for account in accounts:
        outcome = _post_to(autopost, item, entry, account, asset, now)
        ai_used = ai_used or outcome.pop("ai_used", False)
        outcomes.append(outcome)
    item.ai_used = ai_used
    made = [o for o in outcomes if o["status"] in ("scheduled", "draft", "held")]
    if any(o["status"] == "scheduled" for o in outcomes):
        status = FeedItem.Status.SCHEDULED
    elif any(o["status"] == "held" for o in outcomes):
        status = FeedItem.Status.HELD
    elif made:
        status = FeedItem.Status.DRAFT
    elif any(o["status"] == "failed" for o in outcomes):
        status = FeedItem.Status.FAILED
    else:
        status = FeedItem.Status.SKIPPED
    reasons = notes + [f"{o['account']}: {o['reason']}" for o in outcomes if o["reason"]]
    _settle(item, status, " ".join(reasons), outcomes)
    return 1 if made else 0


def _settle(item: FeedItem, status: str, reason: str, outcomes: list) -> None:
    item.status, item.reason, item.outcomes = status, reason[:2000], outcomes
    item.save(update_fields=["status", "reason", "outcomes", "ai_used"])


def _requires_approval(workspace) -> bool:
    from apps.approvals.policy import APPROVAL_REQUIRED_MODES

    return getattr(workspace, "approval_workflow_mode", "none") in APPROVAL_REQUIRED_MODES


def _slot(autopost: FeedAutopost, account, now):
    """``(queue, when)`` for the destination, or ``(None, None)`` for a draft."""
    if autopost.destination == FeedAutopost.Destination.DELAY:
        return None, now + dt.timedelta(minutes=autopost.delay_minutes)
    if autopost.destination == FeedAutopost.Destination.QUEUE:
        queue = Queue.objects.filter(social_account=account, is_active=True).order_by("created_at", "id").first()
        if queue is None:
            raise AutopostError("This account has no active queue.")
        slots = queue_services.next_available_slots(account, 1, after=now)
        if not slots:
            raise AutopostError("The queue has no open slot in the coming weeks.")
        return queue, slots[0]
    return None, None


def _post_to(autopost, item, entry, account, asset, now) -> dict:
    """The outcome for one account: scheduled, saved as a draft, held, skipped or failed (never raises for a refusal)."""
    from apps.approvals.services import submit_for_review

    outcome = {
        "account": account.account_name or account.account_handle,
        "account_id": str(account.id),
        "status": "failed",
        "reason": "",
        "post_id": "",
    }
    text, ai_used, note = _write(autopost, entry, account)
    if not text:
        outcome["reason"] = "The link is longer than this network allows."
        return outcome
    outcome["reason"] = note
    drafting = autopost.destination == FeedAutopost.Destination.DRAFT
    try:
        queue, when = _slot(autopost, account, now)
    except AutopostError as exc:
        outcome["reason"] = f"{outcome['reason']} {exc}".strip()
        return outcome

    who = system(WHO)
    action_type = ActionType.DRAFT_CONTENT if drafting else ActionType.PUBLISH_POSTS
    verdict = autonomy_service.evaluate(
        workspace=autopost.workspace, actor=who, action_type=action_type, account=account, text=text, when=when or now,
        source="system",
    )  # fmt: skip
    target_key = item.key[:64]
    links = {
        "workspace": autopost.workspace, "actor": who, "action_type": action_type, "operation": "feed.autopost",
        "verdict": verdict, "account": account, "requested_at": when, "source_text": entry.title, "final_text": text,
        "ai_used": ai_used, "target_key": target_key,
    }  # fmt: skip
    if verdict.blocked:
        outcome["status"], outcome["reason"] = "skipped", f"Blocked by the autonomy policy: {verdict.reason_text}."
        autonomy_service.record(**links)
        return outcome

    needs_approval = verdict.held or (not drafting and _requires_approval(autopost.workspace))
    if not drafting and not needs_approval:
        try:
            check_can_schedule_posts(autopost.workspace.organization, count=1, when=when)
            from apps.api.limits import check_platform_quota

            check_platform_quota(account)
        except PlanLimitError:
            raise
        except Exception as exc:
            outcome["reason"] = f"{outcome['reason']} {getattr(exc, 'message', str(exc))}".strip()
            return outcome

    media_ids = [asset.id] if asset is not None and getattr(account, "accepts_media", True) else []
    if drafting or needs_approval:
        with transaction.atomic():
            post, pp = _build(autopost, account, entry, text, media_ids, proposed=when if needs_approval else None)
            if autopost.created_by is not None and (needs_approval or _requires_approval(autopost.workspace)):
                try:
                    submit_for_review(pp, autopost.created_by, autopost.workspace)
                except ValueError:
                    logger.info("Feed autopost %s: left as a draft, couldn't be sent for review", autopost.pk)
            pp.refresh_from_db()
            held = needs_approval and not drafting
            reasons = verdict.reasons or ["the workspace requires a person's approval first"]
            autonomy_service.record(
                **{
                    **links,
                    "decision": Decision.HELD if held else Decision.EXECUTED,
                    "summary": f"Held for review because {'; '.join(reasons)}." if held else "Saved as a draft.",
                    "post": post,
                    "platform_post": pp,
                }
            )
        outcome.update(status="held" if held else "draft", ai_used=ai_used, post_id=str(post.id))
        return outcome

    # The network may refuse the image at scheduling time: then the text goes out alone rather than losing the item.
    last_error = ""
    for ids in ([media_ids] if media_ids else []) + [[]]:
        try:
            with transaction.atomic():
                post, pp = _build(autopost, account, entry, text, ids, proposed=None)
                _schedule(pp, queue, when, post)
                autonomy_service.record(**{**links, "post": post, "platform_post": pp})
        except (ValueError, queue_services.QueueFullError) as exc:
            last_error = str(exc)
            continue
        if media_ids and not ids:
            outcome["reason"] = f"{outcome['reason']} Posted without the image ({last_error}).".strip()
        outcome.update(status="scheduled", ai_used=ai_used, post_id=str(post.id))
        return outcome
    outcome["reason"] = f"{outcome['reason']} {last_error}".strip()
    return outcome


def _build(autopost, account, entry, text, media_ids, *, proposed):
    post = composer_services.create_post(
        workspace=autopost.workspace,
        social_account=account,
        caption=text,
        media_asset_ids=media_ids,
        title=entry.title[:255],
        internal_notes=f"From the feed {autopost.feed.name}: {entry.link}".strip(),
        author=autopost.created_by,
        status="draft",
        proposed_publish_at=proposed,
    )
    return post, post.platform_posts.get(social_account=account)


def _schedule(pp: PlatformPost, queue, when, post) -> None:
    if queue is not None:
        queue_services._lock_channel(queue)
        entry = queue_services._ensure_entry(post, queue)
        queue_services._write_slot(entry, pp, when)
        pp.refresh_from_db()
    composer_services.transition_platform_post(pp, PlatformPost.Status.SCHEDULED, scheduled_at=when)


def recent_items(autopost: FeedAutopost, limit: int = 50):
    return autopost.items.exclude(status=FeedItem.Status.OLDER)[:limit]
