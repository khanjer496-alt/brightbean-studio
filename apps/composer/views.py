"""Views for the Post Composer (F-2.1)."""

import base64
import contextlib
import json
import logging
import re
import time
import uuid
from datetime import UTC, datetime
from urllib.parse import urljoin

import httpx
from dateutil import parser as date_parser
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.cache import cache, caches
from django.core.exceptions import PermissionDenied, SuspiciousOperation, ValidationError
from django.db import models, transaction
from django.http import FileResponse, Http404, HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.html import strip_tags
from django.utils.text import get_text_list
from django.views.decorators.http import require_GET, require_POST
from django_ratelimit.decorators import ratelimit

from apps.ai_writer.views import composer_context as ai_composer_context
from apps.autonomy.actors import human as human_actor
from apps.autonomy.release import resolve_paused_thread
from apps.autonomy.service import record_scheduled
from apps.billing.entitlements import (
    COUNTED_POST_STATUSES,
    PlanLimitError,
    billing_enabled,
    check_can_schedule_posts,
)
from apps.common.validators import (
    is_safe_url,
    parse_and_truncate_tag_string,
    parse_and_truncate_youtube_tag_string,
    safe_xml_fromstring,
)
from apps.media_library.models import MediaAsset
from apps.members.decorators import require_permission
from apps.members.models import WorkspaceMembership
from apps.social_accounts.models import SocialAccount
from apps.workspaces import brand_kit
from apps.workspaces.models import Workspace
from providers import (
    accepts_media_for,
    is_video_only,
    mention_lookup_for,
    post_settings_error_for,
    post_settings_for,
    requires_media,
    thread_capability_for,
)
from providers.tiktok import VALID_PRIVACY_LEVELS as TIKTOK_PRIVACY_LEVELS

from .forms import ContentCategoryForm, PostForm
from .models import (
    ContentCategory,
    Feed,
    HashtagSet,
    Idea,
    IdeaGroup,
    IdeaMedia,
    PlatformPost,
    PlatformPostPart,
    Post,
    PostMedia,
    PostTemplate,
    PostVersion,
    Signature,
    Tag,
)
from .services import (
    MENTION_QUERY_MAX,
    default_signature,
    media_blocker,
    media_change_blocker,
    media_rule_issues,
    mention_suggestions,
    parse_post_settings,
    parse_thread_parts,
    post_media_assets,
    post_media_types,
    post_thread_parts,
    publish_blocker,
    recent_hashtags,
    replace_post_settings,
    require_publishable,
    resolve_thread_part_assets,
    set_post_parts,
    spec_thread_parts,
    thread_blocker,
    thread_started,
    thread_stopped_state,
    usable_media_types,
    with_signature,
)
from .status import READONLY_STATUSES, derive_post_status

logger = logging.getLogger(__name__)

MAX_CSV_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB cap on CSV planner imports

# Shown when every posting slot within the lookahead horizon is already taken.
_QUEUE_FULL_MSG = "No open posting slot within the scheduling horizon — add posting slots or free one up."


def _get_workspace(request, workspace_id):
    """Resolve workspace and enforce membership check."""
    workspace = get_object_or_404(Workspace, id=workspace_id)
    if not request.user.is_authenticated:
        raise PermissionDenied("Authentication required.")
    has_membership = WorkspaceMembership.objects.filter(
        user=request.user,
        workspace=workspace,
    ).exists()
    if not has_membership:
        raise PermissionDenied("You are not a member of this workspace.")
    return workspace


def _is_valid_uuid(value):
    try:
        uuid.UUID(value)
    except (ValueError, TypeError):
        return False
    return True


def _parse_selected_account_ids(raw):
    """Split a comma-separated ``selected_accounts`` value into account IDs.

    Non-UUID entries are dropped: they can only come from a crafted or
    corrupted request, and letting them reach a UUIDField lookup raises
    ValidationError (an unhandled 500) instead of being ignored.
    """
    return [s.strip() for s in (raw or "").split(",") if s.strip() and _is_valid_uuid(s.strip())]


def _get_account_scope(request):
    """Return the validated ``account_scope`` POST value, or ``None`` when unscoped.

    The hidden input is server-rendered from a UUID-validated ``?account=``
    param, so a malformed value means a crafted or corrupted request —
    reject it outright (HTTP 400) rather than guessing which rows to touch.
    """
    scope = request.POST.get("account_scope", "").strip()
    if not scope:
        return None
    if not _is_valid_uuid(scope):
        raise SuspiciousOperation("Malformed account_scope.")
    return scope


def _remove_deselected_platform_posts(request, post, selected_ids):
    """Delete PlatformPosts the user deselected in the composer form.

    When the composer was opened scoped to a single account
    (``?account=`` → hidden ``account_scope`` input), the form only renders
    that account, so ``selected_ids`` is NOT the complete desired set —
    restrict deletion to the scoped account to keep siblings intact.
    Published/publishing rows are never deleted by deselection (explicit
    post deletion remains the user's call — see PlatformPost.PROTECTED_STATUSES).
    """
    qs = post.platform_posts.exclude(social_account_id__in=selected_ids)
    scope = _get_account_scope(request)
    if scope:
        qs = qs.filter(social_account_id=scope)
    qs.exclude(status__in=PlatformPost.PROTECTED_STATUSES).delete()


def _scoped_platform_post_ids(request, post):
    """PlatformPost IDs inside the composer's ``account_scope``, or ``None`` when unscoped."""
    scope = _get_account_scope(request)
    if not scope:
        return None
    return list(post.platform_posts.filter(social_account_id=scope).values_list("id", flat=True))


def _match_scope(children, scope):
    """The PlatformPosts in ``children`` belonging to ``scope``, an account UUID.

    Returns every child when ``scope`` is empty, and none when it is set but
    unparseable. Compares UUID *objects*, never their strings: ``uuid.UUID``
    accepts any case, but ``str()`` of one is always lowercase, so a
    differently-cased ``?account=`` would silently match nothing.
    """
    if not scope:
        return list(children)
    try:
        wanted = uuid.UUID(str(scope))
    except (ValueError, TypeError):
        return []
    return [pp for pp in children if pp.social_account_id == wanted]


def _gated_children(children, scope):
    """The children a read-only decision should weigh, given ``scope``.

    Like ``_match_scope``, except an unmatched scope falls back to the whole
    list. That fallback is load-bearing: without it ``?account=<id of an
    account with no row on this post>`` would leave the caller looking at an
    empty list, which reads as "nothing published here" and reopens a live post
    for editing. An unmatched scope must never be *more* permissive than no
    scope at all.
    """
    return _match_scope(children, scope) or list(children)


def _children_are_locked(children):
    """Whether every one of ``children`` is published or mid-publish.

    The *server's* rule, used by the save and autosave endpoints: a row in
    ``PROTECTED_STATUSES`` can no longer be written, so when they all are there
    is nothing left to save. Deliberately narrower than
    ``_children_are_readonly`` — see there.
    """
    return bool(children) and all(pp.status in PlatformPost.PROTECTED_STATUSES for pp in children)


def _children_are_readonly(children):
    """Whether the composer should stop *offering* to change ``children``.

    The *UI's* rule, and wider than ``_children_are_locked``: a
    partially-published post still holds a writable failed child, so the server
    keeps accepting scoped writes to it, but the aggregate is a finished
    publish and the action bar steps back and points at Clone instead.

    The two rules disagree on exactly that case, on purpose. The UI is the
    stricter side, so the asymmetry only ever hides an affordance — it never
    lets a write through that ``_children_are_locked`` would refuse. Change one
    and you almost certainly need to think about the other.
    """
    return derive_post_status([pp.status for pp in children]) in READONLY_STATUSES


def _is_readonly_write(request, post):
    """Whether every PlatformPost this request would touch has already published.

    Backs the composer's read-only banner on the server: once a post is live (or
    mid-publish) there is nothing left to write, so the save and autosave paths
    must stop rewriting its content and re-stamping ``scheduled_at``.

    Scoped requests (``account_scope``) weigh only that child, mirroring the
    scoped composer: a failed channel on a partially-published post stays
    workable, and Clone remains the retry path for the published ones. A post
    with no children yet (a brand-new draft) is never read-only.
    """
    # Only three columns are needed; skip hydrating captions and platform_extra.
    children = list(post.platform_posts.only("id", "status", "social_account_id"))
    return _children_are_locked(_gated_children(children, _get_account_scope(request)))


def _readonly_rejection(request, post):
    """A 400 for a write to an already-published post, or ``None`` to proceed.

    ``{"errors": {...}}`` is the only shape the composer renders as a readable
    message (see ``onFormSaved`` in compose.html); ``PermissionDenied`` would
    return Django's HTML 403 and degrade to the generic fallback toast.
    """
    if not _is_readonly_write(request, post):
        return None
    return JsonResponse(
        {
            "errors": {
                "status": "This post has already published and can't be edited. Use Clone to repost an editable copy."
            }
        },
        status=400,
    )


def _sync_platform_posts(request, post, workspace, initial_status=None):
    """Sync platform post selections from form data.

    When ``initial_status`` is given, newly-created PlatformPost rows are
    initialised to that status (e.g. ``"draft"``, ``"scheduled"``,
    ``"pending_review"``). Existing rows are not touched here — call
    ``_transition_post_children`` separately if you want to move them.
    """
    selected_ids = _parse_selected_account_ids(request.POST.get("selected_accounts", ""))
    _remove_deselected_platform_posts(request, post, selected_ids)
    for acc_id in selected_ids:
        try:
            account = SocialAccount.objects.get(id=acc_id, workspace=workspace)
        except SocialAccount.DoesNotExist:
            continue
        defaults = {}
        if initial_status:
            defaults["status"] = initial_status
        pp, _created = PlatformPost.objects.get_or_create(
            post=post,
            social_account=account,
            defaults=defaults,
        )
        override_title = request.POST.get(f"override_title_{acc_id}", "").strip()
        override_caption = request.POST.get(f"override_caption_{acc_id}", "").strip()
        override_comment = request.POST.get(f"override_comment_{acc_id}", "").strip()
        pp.platform_specific_title = override_title if override_title else None
        pp.platform_specific_caption = override_caption if override_caption else None
        pp.platform_specific_first_comment = override_comment if override_comment else None

        # Per-platform extras
        if account.platform == "facebook" and f"facebook_panel_{acc_id}" in request.POST:
            # Gated on the panel's hidden marker, which the composer renders for
            # every selected Facebook account regardless of media: that keeps a
            # non-composer save from wiping extras it never rendered, while
            # still letting a composer save clear a hint whose video is gone.
            #
            # The Reel selector itself is omitted when the attachment set is not
            # exactly one video. Clearing an earlier choice in that case is what
            # stops removing or replacing media from leaving a stale Reel hint
            # that sends a text or image post to Facebook's Reel endpoint.
            extra = {**(pp.platform_extra or {})}
            facebook_post_type = request.POST.get(f"facebook_post_type_{acc_id}", "").strip()
            media_types = list(post.media_attachments.values_list("media_asset__media_type", flat=True)[:2])
            has_exactly_one_video = media_types == ["video"]
            if facebook_post_type == "reel" and has_exactly_one_video:
                extra["post_type"] = "reel"
            else:
                # Only the deviation from the default is worth storing. A lone
                # video already infers PostType.VIDEO at publish time, so a
                # "video" hint restates what the media says and can only go
                # wrong: if the attachment is later swapped for an image
                # through the media endpoints, the hint would still route it to
                # Facebook's video endpoint. Choosing regular video therefore
                # clears the hint rather than recording it.
                extra.pop("post_type", None)
            pp.platform_extra = extra

        elif account.platform == "youtube":
            tags_list = parse_and_truncate_youtube_tag_string(request.POST.get(f"yt_tags_{acc_id}", ""))
            privacy_status = request.POST.get(f"yt_privacy_status_{acc_id}", "public")
            if privacy_status not in ("public", "unlisted", "private"):
                privacy_status = "public"
            thumb_id = request.POST.get(f"yt_thumbnail_asset_id_{acc_id}", "").strip() or None
            pp.platform_extra = {
                "privacy_status": privacy_status,
                "self_declared_made_for_kids": request.POST.get(f"yt_made_for_kids_{acc_id}") == "true",
                "tags": tags_list,
                "thumbnail_asset_id": thumb_id,
            }

        elif account.platform == "pinterest":
            board_id = request.POST.get(f"pin_board_id_{acc_id}", "").strip()
            if not board_id:
                board_id = (pp.platform_extra or {}).get("board_id") or None
            pp.platform_extra = {
                "board_id": board_id,
                "link_url": request.POST.get(f"pin_link_url_{acc_id}", "").strip() or None,
                "alt_text": request.POST.get(f"pin_alt_text_{acc_id}", "").strip() or None,
                "tag_products": request.POST.get(f"pin_tag_products_{acc_id}", "").strip() or None,
                "allow_comments": request.POST.get(f"pin_allow_comments_{acc_id}") == "true",
                "show_similar_products": request.POST.get(f"pin_show_similar_{acc_id}") == "true",
                "cover_image_asset_id": request.POST.get(f"pin_cover_image_asset_id_{acc_id}", "").strip() or None,
            }

        elif account.platform == "tiktok" and f"tiktok_privacy_level_{acc_id}" in request.POST:
            # Only rebuild extras when the TikTok panel was part of the form,
            # so non-composer saves can't wipe a previously chosen privacy level.
            privacy = request.POST.get(f"tiktok_privacy_level_{acc_id}", "").strip()
            if privacy not in TIKTOK_PRIVACY_LEVELS:
                # An empty/invalid submit (required-validation bypassed) must
                # not wipe a previously saved choice.
                privacy = (pp.platform_extra or {}).get("privacy_level", "")
            # Comment / Duet / Stitch are independent interaction settings —
            # TikTok's UX guidelines require a separate toggle per interaction,
            # each greyed out on its own when the creator disabled it.
            extra = {
                "disable_comment": request.POST.get(f"tiktok_allow_comment_{acc_id}") != "true",
                "disable_duet": request.POST.get(f"tiktok_allow_duet_{acc_id}") != "true",
                "disable_stitch": request.POST.get(f"tiktok_allow_stitch_{acc_id}") != "true",
                "brand_organic_toggle": request.POST.get(f"tiktok_brand_organic_{acc_id}") == "true",
                "brand_content_toggle": request.POST.get(f"tiktok_brand_content_{acc_id}") == "true",
                "is_aigc": request.POST.get(f"tiktok_is_aigc_{acc_id}") == "true",
            }
            if privacy:
                extra["privacy_level"] = privacy
            # Cover frame timestamp from the composer's frame picker; omitted
            # when blank/invalid so TikTok falls back to the first frame.
            # Parse with int() rather than str.isdigit() — isdigit() accepts
            # non-ASCII digits (e.g. "²", "١٢") that int() then rejects with an
            # unhandled ValueError.
            cover_ms = request.POST.get(f"tiktok_video_cover_timestamp_ms_{acc_id}", "").strip()
            if cover_ms:
                try:
                    cover_ms_val = int(cover_ms)
                except ValueError:
                    cover_ms_val = -1
                if cover_ms_val >= 0:
                    extra["video_cover_timestamp_ms"] = cover_ms_val
            pp.platform_extra = extra

        elif post_settings_for(account.platform) and f"ps_panel_{acc_id}" in request.POST:
            # The platform declares its own per-post fields (a subreddit, a
            # flair); rebuild only those keys so anything else already stored
            # stays, and gate on the panel marker so a save from somewhere that
            # never rendered the panel can't wipe them.
            pp.platform_extra = replace_post_settings(
                account.platform, pp.platform_extra, parse_post_settings(account.platform, request.POST.get, acc_id)
            )

        pp.save()


def _validate_post_settings(request, workspace, action):
    """Refuse to schedule a post whose platform wants settings the form leaves unfilled."""
    if action not in _PUBLISH_COMMITTING_ACTIONS:
        return None
    selected_ids = _parse_selected_account_ids(request.POST.get("selected_accounts", ""))
    if not selected_ids:
        return None
    for account in SocialAccount.objects.filter(id__in=selected_ids, workspace=workspace):
        if not post_settings_for(account.platform) or f"ps_panel_{account.id}" not in request.POST:
            continue
        values = parse_post_settings(account.platform, request.POST.get, account.id)
        error = post_settings_error_for(account.platform, values)
        if error:
            return JsonResponse(
                {"errors": {"post_settings": f"{account.get_platform_display()}: {error}"}},
                status=400,
            )
    return None


def _validate_pinterest_board_selection(request, post, workspace):
    """Selected Pinterest accounts need a board before composer save/submit."""
    selected_ids = _parse_selected_account_ids(request.POST.get("selected_accounts", ""))
    if not selected_ids:
        return None

    accounts = SocialAccount.objects.filter(id__in=selected_ids, workspace=workspace, platform="pinterest")
    for account in accounts:
        acc_id = str(account.id)
        board_id = request.POST.get(f"pin_board_id_{acc_id}", "").strip()
        if not board_id and post.pk:
            board_id = (
                PlatformPost.objects.filter(post=post, social_account=account)
                .values_list("platform_extra__board_id", flat=True)
                .first()
                or ""
            )
        if not board_id:
            return JsonResponse(
                {"errors": {"pinterest_board": f"Select a Pinterest board for {account.account_name}."}},
                status=400,
            )
    return None


# Per-account form fields that name a media asset, by the platform that reads them.
_PLATFORM_ASSET_FIELDS = {
    "youtube": "yt_thumbnail_asset_id_",
    "pinterest": "pin_cover_image_asset_id_",
}


def _validate_platform_asset_selection(request, workspace):
    """Thumbnail/cover ids must name an asset this workspace may use.

    They are stored in ``platform_extra`` as-is and resolved at publish time,
    so an id from another workspace would publish someone else's file.
    """
    selected_ids = _parse_selected_account_ids(request.POST.get("selected_accounts", ""))
    if not selected_ids:
        return None

    accounts = SocialAccount.objects.filter(
        id__in=selected_ids, workspace=workspace, platform__in=_PLATFORM_ASSET_FIELDS
    )
    usable_assets = MediaAsset.objects.for_workspace_with_shared(workspace.id, workspace.organization_id)
    for account in accounts:
        asset_id = request.POST.get(f"{_PLATFORM_ASSET_FIELDS[account.platform]}{account.id}", "").strip()
        if not asset_id:
            continue
        try:
            asset_uuid = uuid.UUID(asset_id)
        except ValueError:
            asset_uuid = None
        if asset_uuid is None or not usable_assets.filter(id=asset_uuid).exists():
            return JsonResponse(
                {"errors": {"media": f"The selected image for {account.account_name} is not in this media library."}},
                status=400,
            )
    return None


# Actions that commit a post to publishing. A draft may still be waiting for
# its media, so saving one is never blocked on it.
_PUBLISH_COMMITTING_ACTIONS = frozenset(
    {
        "schedule",
        "publish_now",
        "add_to_queue",
        "add_to_queue_priority",
        "submit_for_approval",
        "resubmit_for_approval",
    }
)


def _composer_media_types(request, post, workspace) -> set[str]:
    """Usable media types on the post plus the uploads waiting to be attached."""
    media_types = post_media_types(post)
    # A post that has never been saved keeps its uploads in the session until
    # save_post attaches them, after this check has run.
    if post._state.adding:
        pending_ids = request.session.get(f"pending_media_{workspace.id}", [])
        media_types |= usable_media_types(MediaAsset.objects.filter(id__in=pending_ids, workspace=workspace))
    return media_types


_SCHEDULING_ACTIONS = ("schedule", "publish_now", "add_to_queue", "add_to_queue_priority")


def _scheduled_post_allowance_error(request, post, workspace, action, form):
    """Refuse up front when scheduling would exceed the plan's monthly posts.

    Checked before anything is saved so a refusal leaves the post exactly as
    it was. Children already scheduled don't count twice; a queued post is
    counted against this month, where the soonest slot almost always falls.
    """
    if action not in _SCHEDULING_ACTIONS or not billing_enabled():
        return None
    selected = set(_parse_selected_account_ids(request.POST.get("selected_accounts", "")))
    if post.pk:
        counted = PlatformPost.objects.filter(post=post, status__in=COUNTED_POST_STATUSES)
        selected -= {str(acc_id) for acc_id in counted.values_list("social_account_id", flat=True)}
    when = None
    if action == "schedule":
        when = _combine_schedule_dt(
            workspace,
            form.cleaned_data.get("scheduled_date"),
            form.cleaned_data.get("scheduled_time"),
        )
    try:
        check_can_schedule_posts(workspace.organization, count=len(selected), when=when)
    except PlanLimitError as exc:
        return JsonResponse({"errors": {"plan": exc.message}, "upgrade_url": exc.upgrade_url}, status=402)
    return None


def _validate_required_media(request, post, workspace, action):
    """Refuse to schedule a post without the media a selected platform needs.

    TikTok and YouTube publish only video; Instagram and Pinterest need an
    image or a video. Their providers refuse anything else, so such a post can
    only fail — usually after its other platforms have gone out, leaving it
    half-published for the user to repair by hand.
    """
    if action not in _PUBLISH_COMMITTING_ACTIONS:
        return None
    selected_ids = _parse_selected_account_ids(request.POST.get("selected_accounts", ""))
    if not selected_ids:
        return None

    accounts = list(SocialAccount.objects.filter(id__in=selected_ids, workspace=workspace))
    # Skip the media lookup when no selected platform cares about it.
    if not any(requires_media(account.platform) or not accepts_media_for(account.platform) for account in accounts):
        return None
    media_types = _composer_media_types(request, post, workspace)
    blocked = [account for account in accounts if media_blocker(account, media_types=media_types)]
    if not blocked:
        return None

    # A platform that publishes no media at all is a different fix from the rest:
    # the media has to go, not be added.
    text_only = [a for a in blocked if not accepts_media_for(a.platform)]
    if text_only:
        names = sorted({a.get_platform_display() for a in text_only})
        return JsonResponse(
            {
                "errors": {
                    "media": f"{get_text_list(names, 'and')} can only publish text or links. "
                    "Remove the media from this post, or deselect "
                    f"{'that account' if len(text_only) == 1 else 'those accounts'}."
                }
            },
            status=400,
        )

    # Worded apart from media_blocker's because this form can also be fixed by
    # deselecting the account, and it names every platform at once.
    video_only = sorted({a.get_platform_display() for a in blocked if is_video_only(a.platform)})
    any_media = sorted({a.get_platform_display() for a in blocked if not is_video_only(a.platform)})
    sentences = []
    if video_only:
        sentences.append(f"{get_text_list(video_only, 'and')} can only publish videos.")
    if any_media:
        verb = "needs" if len(any_media) == 1 else "need"
        sentences.append(f"{get_text_list(any_media, 'and')} {verb} an image or a video.")
    # A video satisfies every platform on the list, so ask for one when any
    # video-only platform is among them.
    wanted = "a video" if video_only else "an image or a video"
    which = "that account" if len(blocked) == 1 else "those accounts"
    sentences.append(f"Add {wanted} to this post, or deselect {which}.")
    return JsonResponse({"errors": {"media": " ".join(sentences)}}, status=400)


def _recurrence_from_form(request):
    """The composer Repeat control's setting, or None when the form has none.

    ``{"frequency": "never"}`` asks to stop a series; anything else is the
    keyword arguments for ``save_recurrence``, not yet validated. The legacy
    ``make_recurring`` flag still turns repeating on for older form posts.
    Raises ``ValueError`` for a value that can't be read.
    """
    data = request.POST
    legacy = bool(data.get("make_recurring"))
    if "recurrence_panel" not in data and not legacy:
        return None
    frequency = data.get("recurrence_frequency", "weekly" if legacy else "never")
    if frequency == "never":
        return {"frequency": "never"}

    end = data.get("recurrence_end") or ("on" if data.get("recurrence_end_date") else "never")
    end_date = None
    count = None
    if end == "on":
        from datetime import date as date_cls

        try:
            end_date = date_cls.fromisoformat(data.get("recurrence_end_date", ""))
        except ValueError:
            raise ValueError("Pick the date the series ends.") from None
    elif end == "after":
        count = data.get("recurrence_count", "")
        if not count.strip().isdigit():
            raise ValueError("Enter how many times the post repeats.")
    return {
        "frequency": frequency,
        "interval": data.get("recurrence_interval") or 1,
        "weekdays": data.getlist("recurrence_weekdays"),
        "end_date": end_date,
        "count": count,
    }


def _recurrence_error(recurrence, post, workspace):
    """A 400 response if ``recurrence`` can't repeat ``post``, else None.

    The series starts at the post's scheduled time or, for a post sent for
    approval, its proposed one. Checked before anything is written.
    """
    import zoneinfo

    from apps.calendar.services import clean_recurrence

    start = post.scheduled_at or post.proposed_publish_at
    if start is None:
        return JsonResponse(
            {"errors": {"recurrence": "Pick a date and time for the first post to repeat."}}, status=400
        )
    ws_tz = zoneinfo.ZoneInfo(workspace.effective_timezone or "UTC")
    try:
        clean_recurrence(**recurrence, start=start.astimezone(ws_tz))
    except ValueError as exc:
        return JsonResponse({"errors": {"recurrence": str(exc)}}, status=400)
    return None


def _save_approval_recurrence(post, recurrence):
    """Keep the Repeat setting of a post sent for approval.

    The rule is saved now and its copies start once the post is approved and
    scheduled (``generate_occurrences`` waits for a committed first post), each
    carrying that approval. ``never`` stops a series the post already had.
    """
    from apps.calendar.services import save_recurrence, stop_recurrence

    if recurrence is None:
        return
    if recurrence["frequency"] == "never":
        stop_recurrence(post)
    else:
        save_recurrence(post, **recurrence)


def _composer_media_assets(request, post, workspace) -> list:
    """The post's media assets in order — for a post never saved, its uploads.

    The asset-level counterpart of ``_composer_media_types``.
    """
    if not post._state.adding:
        return post_media_assets(post)
    # A post that has never been saved keeps its uploads in the session until
    # save_post attaches them, after this check has run.
    assets = []
    pending_ids = request.session.get(f"pending_media_{workspace.id}", [])
    if pending_ids:
        by_id = {str(a.id): a for a in MediaAsset.objects.filter(id__in=pending_ids, workspace=workspace)}
        assets = [by_id[str(i)] for i in pending_ids if str(i) in by_id]
    return assets


def _composer_platform_extras(request, accounts, post=None) -> dict:
    """Each account's platform_extra as this form would leave it, for media checks.

    Starts from the saved rows and applies the form's post-type choice
    (``<platform>_post_type_<account id>``, e.g. a Facebook Reel), which
    decides which of the platform's media rules apply. Keyed by account id.
    """
    saved = {}
    if post is not None and not post._state.adding:
        saved = dict(
            PlatformPost.objects.filter(post=post, social_account__in=accounts).values_list(
                "social_account_id", "platform_extra"
            )
        )
    extras = {}
    for account in accounts:
        extra = dict(saved.get(account.id) or {})
        hint = request.POST.get(f"{account.platform}_post_type_{account.id}", "").strip()
        if hint:
            extra["post_type"] = hint
        extras[account.id] = extra
    return extras


def _composer_media_issues(request, post, workspace, accounts) -> dict:
    """``media_rule_issues`` per selected account, keyed by account id."""
    assets = _composer_media_assets(request, post, workspace)
    if not assets:
        return {}
    extras = _composer_platform_extras(request, accounts, post)
    return {
        str(account.id): media_rule_issues(account, assets, platform_extra=extras[account.id]) for account in accounts
    }


def _validate_platform_media_rules(request, post, workspace, action):
    """Refuse to schedule media a selected platform's rules say it will reject.

    Covers what ``_validate_required_media`` doesn't: shape, resolution,
    length, size and item count, as each provider declares them
    (``SocialProvider.media_rules``). Warnings never block; the composer's
    preview shows them as the post is edited.
    """
    if action not in _PUBLISH_COMMITTING_ACTIONS:
        return None
    selected_ids = _parse_selected_account_ids(request.POST.get("selected_accounts", ""))
    if not selected_ids:
        return None
    accounts = list(SocialAccount.objects.filter(id__in=selected_ids, workspace=workspace))
    issues = _composer_media_issues(request, post, workspace, accounts)
    errors = [issue.message for account_issues in issues.values() for issue in account_issues if issue.is_error]
    if not errors:
        return None
    # Two accounts on one platform hit the same rule; say it once.
    return JsonResponse({"errors": {"media": " ".join(dict.fromkeys(errors))}}, status=400)


def _composer_thread_parts(request, workspace):
    """The thread's later parts from the composer form, with their media.

    Returns ``(specs, asset_map)``, or ``(None, {})`` when the form carried no
    thread editor — a save from anywhere else must leave the parts alone.
    Raises ``ValueError`` on a payload the composer never sends.
    """
    if "thread_parts" not in request.POST:
        return None, {}
    try:
        raw = json.loads(request.POST.get("thread_parts") or "[]")
    except ValueError as exc:
        raise ValueError("The thread couldn't be read. Reload the page and try again.") from exc
    specs = parse_thread_parts(raw)
    return specs, resolve_thread_part_assets(workspace, specs)


def _validate_thread_parts(request, workspace, action, specs, asset_map):
    """Refuse to schedule a thread a selected platform can't publish.

    Each account is held to its platform's ``ThreadCapability``: whether it
    takes a thread at all, how many parts and how long, and the media rules for
    a part's own media. Drafts are never blocked, like media.
    """
    if action not in _PUBLISH_COMMITTING_ACTIONS or not specs:
        return None
    selected_ids = _parse_selected_account_ids(request.POST.get("selected_accounts", ""))
    accounts = SocialAccount.objects.filter(id__in=selected_ids, workspace=workspace)
    errors = [
        reason
        for account in accounts
        if (
            reason := thread_blocker(
                account,
                spec_thread_parts(account, specs, asset_map),
                first_comment=_composer_first_comment(request, account),
            )
        )
    ]
    if not errors:
        return None
    return JsonResponse({"errors": {"thread": " ".join(dict.fromkeys(errors))}}, status=400)


def _composer_first_comment(request, account) -> str:
    """The first comment the composer form would post on ``account``, as ``_sync_platform_posts`` stores it."""
    if not account.supports_first_comment():
        return ""
    override = request.POST.get(f"override_comment_{account.id}", "").strip()
    return override or request.POST.get("first_comment", "")


def _thread_parts_changed(post, specs) -> bool:
    """Whether saving ``specs`` would change ``post``'s stored thread."""
    if post.pk is None:
        return bool(specs)
    current = [
        (
            part["caption"],
            part["delay_seconds"],
            part["is_finisher"],
            [m["id"] for m in part["media"]],
            {acc: text for acc, text in part["caption_overrides"].items()},
        )
        for part in _thread_parts_payload(post)
    ]
    wanted = [
        (
            spec.caption,
            spec.delay_seconds,
            spec.is_finisher,
            [str(mid) for mid in spec.media_asset_ids],
            {acc: text for acc, text in spec.caption_overrides.items() if text is not None},
        )
        for spec in sorted(specs, key=lambda spec: spec.is_finisher)
    ]
    return current != wanted


def _save_thread_parts(post, specs, asset_map):
    """Store the composer's thread on ``post``, or a 400 when it can't be changed.

    A no-op when the form carried no thread editor or the thread is unchanged,
    so saving a sibling of an already-threaded channel never touches the
    parts that channel published.
    """
    if specs is None or not _thread_parts_changed(post, specs):
        return None
    try:
        set_post_parts(post, specs, asset_map=asset_map)
    except ValueError as exc:
        return JsonResponse({"errors": {"thread": str(exc)}}, status=400)
    return None


def _thread_parts_payload(post) -> list[dict]:
    """The composer's thread editor state for an existing post's parts."""
    if post is None:
        return []
    overrides: dict = {}
    for state in PlatformPostPart.objects.filter(
        platform_post__post=post, platform_specific_caption__isnull=False
    ).select_related("platform_post"):
        overrides.setdefault(state.part_id, {})[str(state.platform_post.social_account_id)] = (
            state.platform_specific_caption
        )
    return [
        {
            "caption": part.caption,
            "delay_seconds": part.delay_seconds,
            "is_finisher": part.is_finisher,
            "caption_overrides": overrides.get(part.id, {}),
            "media": [
                {
                    "id": str(pm.media_asset_id),
                    "url": pm.media_asset.file.url if pm.media_asset.file else "",
                    "is_video": pm.media_asset.is_video,
                    "filename": pm.media_asset.filename,
                }
                for pm in part.media_attachments.all()
            ],
        }
        for part in post_thread_parts(post)
    ]


def _save_version(post, user):
    """Create a PostVersion snapshot."""
    from apps.social_accounts.youtube_compliance import authored_youtube_settings

    version_number = (post.versions.count()) + 1
    snapshot = {
        "title": post.title,
        "caption": post.caption,
        "first_comment": post.first_comment,
        "internal_notes": post.internal_notes,
        "tags": post.tags,
        "status": post.status,
        "scheduled_at": post.scheduled_at.isoformat() if post.scheduled_at else None,
        "platform_posts": [
            {
                "social_account_id": str(pp.social_account_id),
                "platform": pp.social_account.platform,
                "title_override": pp.platform_specific_title,
                "caption_override": pp.platform_specific_caption,
                "first_comment_override": pp.platform_specific_first_comment,
                "platform_extra": authored_youtube_settings(pp.platform_extra)
                if pp.social_account.platform == "youtube"
                else pp.platform_extra or {},
            }
            for pp in post.platform_posts.select_related("social_account")
        ],
        "media": [
            {
                "media_asset_id": str(pm.media_asset_id),
                "position": pm.position,
                "alt_text": pm.alt_text,
            }
            for pm in post.media_attachments.all()
        ],
        "parts": [
            {
                "caption": part.caption,
                "delay_seconds": part.delay_seconds,
                "is_finisher": part.is_finisher,
                "media_asset_ids": [str(pm.media_asset_id) for pm in part.media_attachments.all()],
            }
            for part in post_thread_parts(post)
        ],
    }
    PostVersion.objects.create(
        post=post,
        version_number=version_number,
        snapshot=snapshot,
        created_by=user,
    )


def _resolve_queues_for_post(queue_id, workspace, post_data):
    """Resolve the Queues to add a post to.

    Returns a list of Queue objects - one per selected social account when
    no explicit ``queue_id`` is supplied by the composer form. If an explicit
    queue_id is provided it is used exclusively.
    """
    from apps.calendar.models import Queue

    if queue_id:
        q = Queue.objects.filter(id=queue_id, workspace=workspace, is_active=True).first()
        return [q] if q else []

    account_ids = _parse_selected_account_ids(post_data.get("selected_accounts", ""))
    if not account_ids:
        return []

    queues = list(
        Queue.objects.filter(workspace=workspace, is_active=True, social_account_id__in=account_ids).order_by(
            "created_at"
        )
    )
    # De-duplicate by social_account (one queue per account)
    seen = set()
    unique = []
    for q in queues:
        if q.social_account_id in seen:
            continue
        seen.add(q.social_account_id)
        unique.append(q)
    return unique


def _resolve_template_data(template_id, workspace):
    """Resolve a ?template= value into a template_data dict.

    Accepts either a numeric ID for a built-in template (defined in
    apps.composer.builtin_templates.TEMPLATES) or a UUID for a saved
    PostTemplate row. Returns ``None`` if the value is missing, malformed,
    or does not match any template.
    """
    if not template_id:
        return None
    # Built-in templates use numeric IDs.
    try:
        numeric_id = int(template_id)
    except (TypeError, ValueError):
        numeric_id = None
    if numeric_id is not None:
        from apps.composer.builtin_templates import TEMPLATES as BUILTIN_TEMPLATES

        for tpl in BUILTIN_TEMPLATES:
            if tpl.get("id") == numeric_id:
                return {
                    "caption": tpl.get("body", ""),
                    "tags": list(tpl.get("tags", [])),
                }
        return None
    # Fall back to PostTemplate UUID lookup.
    try:
        tpl = PostTemplate.objects.get(id=template_id, workspace=workspace)
    except (PostTemplate.DoesNotExist, ValidationError):
        return None
    return tpl.template_data


@login_required
@require_permission("create_posts")
def compose(request, workspace_id, post_id=None):
    """Render the full-page composer for creating or editing a post."""
    workspace = _get_workspace(request, workspace_id)

    # ?account= scopes the composer to one connected account (calendar links).
    # Validate it up front: a malformed value must behave as "unscoped" rather
    # than leak into UUID lookups (ValidationError → 500) or the hidden
    # account_scope input the save endpoints trust.
    account_filter = request.GET.get("account", "").strip()
    if account_filter and not _is_valid_uuid(account_filter):
        account_filter = ""

    # True when the Schedule Post panel is pre-filled from a draft's *proposed*
    # time rather than a committed schedule — the composer uses this to keep the
    # primary action as "Save Draft" instead of arming "schedule for real".
    schedule_prefill_is_proposed = False

    # The sign-off a new post starts with; the page also keeps it last when an
    # AI result replaces or extends the caption.
    signature = default_signature(workspace, request.user)

    # Load existing post or prepare a blank one
    if post_id:
        post = get_object_or_404(Post, id=post_id, workspace=workspace)
        # Enforce edit permissions: authors can edit their own posts,
        # but editing another user's post requires edit_others_posts.
        membership = request.workspace_membership
        perms = membership.effective_permissions if membership else {}
        if post.author != request.user and not perms.get("edit_others_posts", False):
            raise PermissionDenied("You do not have permission to edit this post.")
        form = PostForm(instance=post)
        # Prefer a committed schedule; fall back to a draft-stage proposal so
        # the Schedule Post panel shows whichever time the post carries.
        prefill_dt = post.scheduled_at or post.proposed_publish_at
        if prefill_dt:
            import zoneinfo

            tz = zoneinfo.ZoneInfo(workspace.effective_timezone or "UTC")
            local_dt = prefill_dt.astimezone(tz)
            form.initial["scheduled_date"] = local_dt.strftime("%Y-%m-%d")
            form.initial["scheduled_time"] = local_dt.strftime("%H:%M")
            schedule_prefill_is_proposed = post.scheduled_at is None
        # One fetch serves selected ids, extras, and the status checks below.
        platform_post_list = list(post.platform_posts.select_related("social_account"))
        # Matched strictly: the scoped form renders and selects only this account,
        # so an unmatched ?account= must select nothing. The read-only gate below
        # reuses the same match but falls back to every child — see _gated_children.
        scoped_platform_posts = _match_scope(platform_post_list, account_filter)
        selected_account_ids = [pp.social_account_id for pp in scoped_platform_posts]
        media_attachments = post.media_attachments.select_related("media_asset").all()
        platform_extras = {str(pp.social_account_id): (pp.platform_extra or {}) for pp in platform_post_list}
        template_data = None
    else:
        post = None
        # Pre-fill scheduled date/time from query params (e.g. when coming from calendar "+" CTA)
        initial = {}
        qs_date = request.GET.get("scheduled_date")
        qs_time = request.GET.get("scheduled_time")
        if qs_date:
            with contextlib.suppress(ValueError):
                initial["scheduled_date"] = datetime.strptime(qs_date, "%Y-%m-%d").date().isoformat()
        if qs_time:
            parsed_time = None
            for fmt in ("%H:%M", "%H:%M:%S"):
                try:
                    parsed_time = datetime.strptime(qs_time, fmt).time()
                    break
                except ValueError:
                    continue
            if parsed_time is not None:
                initial["scheduled_time"] = parsed_time.strftime("%H:%M")
        # Resolve ?template=<id> into template_data (supports both built-in int IDs
        # and PostTemplate UUIDs). Seed caption so it pre-fills the form.
        template_data = _resolve_template_data(request.GET.get("template"), workspace)
        if template_data and template_data.get("caption"):
            initial["caption"] = template_data["caption"]
        else:
            initial["caption"] = with_signature("", signature)
        form = PostForm(initial=initial)
        platform_post_list = []
        scoped_platform_posts = []
        selected_account_ids = []
        media_attachments = []
        platform_extras = {}

    # Clear any stale pending media from previous compose sessions.
    # Each compose page load starts fresh; the upload flow re-populates
    # the session as the user adds files.
    from apps.media_library.models import MediaAsset

    session_key = f"pending_media_{workspace.id}"
    request.session.pop(session_key, None)
    pending_assets = MediaAsset.objects.none()

    # Connected social accounts for this workspace
    social_accounts = (
        SocialAccount.objects.for_workspace(workspace.id)
        .filter(
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        .order_by("platform", "account_name")
    )

    # When opening from calendar with a specific account, show only that account.
    if account_filter:
        social_accounts = social_accounts.filter(id=account_filter)
        if not post_id and social_accounts.exists():
            selected_account_ids = [account_filter]

    # Platform character limits for JS
    char_limits = {}
    for acc in social_accounts:
        cfg = dict(acc.field_config)
        cfg["supports_first_comment"] = acc.supports_first_comment()
        thread = thread_capability_for(acc.platform)
        char_limits[str(acc.id)] = {
            "platform": acc.platform,
            "limit": acc.char_limit,
            # The thread editor counts and checks each part against these.
            "thread_mode": thread.mode.value,
            "thread_max_parts": thread.max_parts,
            "thread_part_limit": thread.part_max_length or acc.char_limit,
            "thread_media_per_part": thread.media_per_part,
            # Each of these costs two characters once the provider escapes it,
            # so the live counter can charge for them as the user types.
            "escaped_chars": acc.escaped_chars,
            # "weighted": count as twitter-text does (URLs 23, CJK and emoji 2).
            "caption_counting": acc.caption_counting,
            # Whether typing "@" can suggest accounts to mention on this platform.
            "mention_lookup": mention_lookup_for(acc.platform),
            "name": acc.account_name or acc.account_handle,
            "youtube_url": f"https://www.youtube.com/channel/{acc.account_platform_id}"
            if acc.platform == "youtube"
            else "",
            "post_settings": acc.post_settings,
            **cfg,
        }

    # Workspace defaults
    default_first_comment = workspace.default_first_comment
    default_hashtags = workspace.default_hashtags

    # Categories for dropdown
    categories = ContentCategory.objects.for_workspace(workspace.id)

    # Queues for "Add to Queue" action
    from apps.calendar.models import Queue

    queues = (
        Queue.objects.for_workspace(workspace.id).filter(is_active=True).select_related("social_account", "category")
    )

    # Permissions for action buttons
    membership = request.workspace_membership
    perms = membership.effective_permissions if membership else {}
    can_publish = perms.get("publish_directly", False)
    can_approve = perms.get("approve_posts", False)
    ws_role = membership.workspace_role if membership else None
    can_view_internal_notes = ws_role not in ("client", "viewer") if ws_role else True

    # "Read-only" describes what the composer is actually *showing*. Opened
    # scoped to one account (``?account=``) only that child's status counts, so
    # a failed channel on a partially-published post stays workable; unscoped,
    # the aggregate decides. Clone is the escape hatch for the published ones.
    # _gated_children, not the strict match: an ?account= that names no row on
    # this post falls back to every child rather than reading as "nothing here".
    post_is_readonly = post is not None and _children_are_readonly(_gated_children(platform_post_list, account_filter))

    # Scoped away from the live channels, the composer would otherwise give no
    # sign they exist — and the caption being edited is shared with them. Name
    # them so the edit isn't made in ignorance of an already-published channel.
    scoped_ids = {pp.id for pp in scoped_platform_posts}
    live_siblings = (
        [pp for pp in platform_post_list if pp.id not in scoped_ids and pp.status in PlatformPost.PROTECTED_STATUSES]
        if not post_is_readonly
        else []
    )

    # Approval workflow context
    workflow_mode = workspace.approval_workflow_mode
    # A read-only post is done with the workflow — neither button applies.
    show_resubmit_button = not post_is_readonly and any(
        pp.status in ("changes_requested", "rejected", "approved") for pp in platform_post_list
    )
    # Fresh drafts get "Submit for Approval"; posts already in the workflow
    # (changes-requested / rejected / approved-but-edited) get "Resubmit" instead.
    show_submit_button = workflow_mode != "none" and not show_resubmit_button and not post_is_readonly
    # Once the post is committed to publishing, the Schedule Post panel re-times
    # a live schedule; while still a draft it captures a *proposed* time on save.
    # Mirror _capture_proposed_publish_at's guard exactly (scheduled_at OR a
    # committed child) so the hint, the JS schedule-arming, and the save path
    # never disagree.
    post_is_scheduled = post is not None and (
        post.scheduled_at is not None
        or any(pp.status in ("scheduled", "publishing", "published") for pp in platform_post_list)
    )

    # Approval history and comments for existing posts
    approval_history = []
    post_comments = []
    latest_feedback = None
    if post:
        from apps.approvals.models import ApprovalAction

        # Show recent history (bounded) — a heavily-cycled post can accumulate
        # unboundedly many actions; 50 is plenty for the timeline.
        approval_history = list(
            ApprovalAction.objects.filter(post=post).select_related("user").order_by("-created_at")[:50]
        )
        # Most recent reviewer feedback to surface in the edit banner.
        latest_feedback = next(
            (a for a in approval_history if a.action in ("changes_requested", "rejected") and a.comment),
            None,
        )
        from apps.approvals.comments import get_comments_for_post

        post_comments = get_comments_for_post(post, request.user)

    # Workspace tags for the tag dropdown
    all_tags = Tag.objects.for_workspace(workspace.id)

    # Failed platform posts with an error message — shown as a banner so the
    # user can see why a publish failed before retrying.
    failed_platform_posts = [
        pp for pp in platform_post_list if pp.status == PlatformPost.Status.FAILED and pp.publish_error
    ]

    # A first comment that failed leaves the post itself published, so it never
    # shows up in the banner above — without this the post looks fully
    # successful while the comment is silently missing.
    failed_first_comments = [
        pp for pp in platform_post_list if pp.first_comment_status == PlatformPost.FirstCommentStatus.FAILED
    ]

    # A thread that stopped part-way leaves its first post published, so it is
    # in neither banner above. Shown with a Resume action that restarts at the
    # stopped part and never re-sends the parts already live.
    partial_threads = [pp for pp in platform_post_list if pp.is_thread_partial]
    for pp in partial_threads:
        # An unconfirmed stopped part may be live already; the banner asks the
        # author to check before Resume re-sends it.
        stopped = thread_stopped_state(pp)
        pp.thread_stopped_unconfirmed = stopped is not None and stopped.status == PlatformPostPart.Status.UNCONFIRMED

    # Build media_items for the initial preview render
    media_items = []
    for att in media_attachments:
        asset = att.media_asset
        media_items.append(
            {
                "url": asset.file.url if asset.file else "",
                "is_video": asset.is_video,
                "filename": asset.filename,
            }
        )
    if not media_items:
        for asset in pending_assets:
            media_items.append(
                {
                    "url": asset.file.url if asset.file else "",
                    "is_video": asset.is_video,
                    "filename": asset.filename,
                }
            )

    # Resolve thumbnail/cover image URLs for per-account assets already saved
    thumb_ids = [
        extra.get("thumbnail_asset_id") for extra in platform_extras.values() if extra.get("thumbnail_asset_id")
    ]
    cover_ids = [
        extra.get("cover_image_asset_id") for extra in platform_extras.values() if extra.get("cover_image_asset_id")
    ]
    all_asset_ids = [aid for aid in thumb_ids + cover_ids if aid]
    asset_url_map = {}
    if all_asset_ids:
        for asset in MediaAsset.objects.for_workspace_with_shared(workspace.id, workspace.organization_id).filter(
            id__in=all_asset_ids
        ):
            url = ""
            if asset.thumbnail:
                url = asset.thumbnail.url
            elif asset.file:
                url = asset.file.url
            asset_url_map[str(asset.id)] = url
    # An id that no longer resolves here (deleted, or outside this workspace's
    # library) is dropped so the form doesn't resubmit it: the editor shows no
    # image for it and save_post would reject it.
    for _acc_id, extra in platform_extras.items():
        tid = extra.get("thumbnail_asset_id")
        if tid and tid in asset_url_map:
            extra["thumbnail_url"] = asset_url_map[tid]
        elif tid:
            extra["thumbnail_asset_id"] = None
        cid = extra.get("cover_image_asset_id")
        if cid and cid in asset_url_map:
            extra["cover_image_url"] = asset_url_map[cid]
        elif cid:
            extra["cover_image_asset_id"] = None

    # Repeat control: a series' first post edits the series; a copy it
    # generated links back to that post instead.
    from apps.calendar.services import WEEKDAY_NAMES, recurrence_initial

    series_rule = post.series_rule if post else None

    context = {
        "workspace": workspace,
        "post": post,
        "form": form,
        "recurrence_initial": recurrence_initial(post),
        "series_rule": series_rule if series_rule and series_rule.is_active else None,
        "weekday_names": WEEKDAY_NAMES,
        "social_accounts": social_accounts,
        "selected_account_ids": [str(aid) for aid in selected_account_ids],
        "platform_extras": platform_extras,
        "media_attachments": media_attachments,
        "media_items": media_items,
        "char_limits": char_limits,
        "default_first_comment": default_first_comment,
        "default_hashtags": json.dumps(default_hashtags),
        "signature_body": signature.body if signature else "",
        "can_publish": can_publish,
        "can_approve": can_approve,
        "can_view_internal_notes": can_view_internal_notes,
        "is_edit": post is not None,
        "schedule_prefill_is_proposed": schedule_prefill_is_proposed,
        "post_is_scheduled": post_is_scheduled,
        "post_is_readonly": post_is_readonly,
        "live_siblings": live_siblings,
        "categories": categories,
        "queues": queues,
        "template_data_json": json.dumps(template_data) if template_data else "null",
        "workflow_mode": workflow_mode,
        "show_submit_button": show_submit_button,
        "show_resubmit_button": show_resubmit_button,
        "approval_history": approval_history,
        "latest_feedback": latest_feedback,
        "post_comments": post_comments,
        "pending_assets": pending_assets,
        "all_tags": all_tags,
        # When opened scoped to one account (?account=), the form only renders
        # that account — the save endpoints use this to leave siblings alone.
        "account_scope": account_filter if (post_id and account_filter) else "",
        "failed_platform_posts": failed_platform_posts,
        "failed_first_comments": failed_first_comments,
        "partial_threads": partial_threads,
        "thread_parts": _thread_parts_payload(post),
        "unsplash_enabled": bool(settings.UNSPLASH_ACCESS_KEY),
        # AI writing assistant panel; hidden unless a provider key is set.
        **ai_composer_context(workspace.organization),
    }
    return render(request, "composer/compose.html", context)


def _transition_post_children(post, target, *, allow_via_draft=True, only=None, audit_user=None):
    """Transition every (or selected) PlatformPost child of *post* to *target*.

    Returns ``(moved, skipped)`` lists of PlatformPost instances. Children
    already in the target state are left alone (counted as moved). For states
    that don't allow a direct transition, an intermediate hop through
    ``draft`` is attempted when ``allow_via_draft`` is True.

    ``only`` may be an iterable of PlatformPost IDs to restrict the operation
    to a subset of children. With ``audit_user``, the children a person just
    scheduled are written to the workspace's activity trail.
    """
    children = post.platform_posts.select_related("social_account")
    if only is not None:
        only_ids = {str(x) for x in only}
        children = [pp for pp in children if str(pp.id) in only_ids]
    children = list(children)
    already = {pp.id for pp in children if pp.status == target}
    moved, skipped = [], []
    for pp in children:
        if pp.status == target:
            moved.append(pp)
            continue
        try:
            if target == "scheduled":
                from apps.composer.services import transition_platform_post as transition_service

                transition_service(pp, "scheduled", scheduled_at=pp.scheduled_at or post.scheduled_at)
                moved.append(pp)
                continue
            if pp.can_transition_to(target):
                pp.transition_to(target)
            elif allow_via_draft and pp.can_transition_to("draft") and target != "draft":
                pp.transition_to("draft")
                if pp.can_transition_to(target):
                    pp.transition_to(target)
                else:
                    skipped.append(pp)
                    continue
            else:
                skipped.append(pp)
                continue
            pp.save(update_fields=[*PlatformPost.TRANSITION_FIELDS, "updated_at"])
            moved.append(pp)
        except ValueError:
            skipped.append(pp)
    if audit_user is not None and target == "scheduled" and moved:
        record_scheduled(
            workspace=post.workspace,
            actor=human_actor(audit_user),
            operation="post.schedule",
            post=post,
            platform_posts=[pp for pp in moved if pp.id not in already and pp.status == "scheduled"],
            text=post.caption,
            summary="Scheduled in the composer.",
        )
    return moved, skipped


def _base_content_snapshot(post):
    """Reviewable base content used to detect edits to an approved post.

    Includes the thread's later parts: they publish too, so editing one has to
    send an approved post back for review like editing the caption does.
    """
    parts = (
        tuple(
            (
                part.caption,
                part.delay_seconds,
                part.is_finisher,
                tuple(pm.media_asset_id for pm in part.media_attachments.all()),
            )
            for part in post_thread_parts(post)
        )
        if post.pk
        else ()
    )
    return (post.title, post.caption, post.first_comment, tuple(post.tags or []), parts)


def _revert_approved_to_review(post):
    """Option A: editing an approved post's content sends it back for re-approval.

    Silently reverts any ``approved`` children to ``pending_review`` so edited
    content can't publish without a fresh review. (The explicit "Resubmit for
    review" button additionally notifies reviewers; this is the safety net for
    plain saves/autosaves.) Returns the reverted children.
    """
    reverted = []
    from apps.approvals.policy import clear_final_approval

    for pp in post.platform_posts.all():
        if pp.status == "approved" and pp.can_transition_to("pending_review"):
            pp.transition_to("pending_review")
            pp.save(update_fields=[*PlatformPost.TRANSITION_FIELDS, "updated_at"])
            clear_final_approval(pp)
            reverted.append(pp)
    return reverted


def _platform_status_map(post):
    """Return ``{platform_post_id: status}`` for HTMX response headers."""
    return {str(pp.id): pp.status for pp in post.platform_posts.all()}


def _combine_schedule_dt(workspace, sched_date, sched_time):
    """Combine the composer's date + time inputs into a workspace-tz-aware datetime.

    Returns ``None`` unless both parts are present. Shared by the real
    schedule (``action='schedule'``) and the draft-stage proposed time so both
    interpret the same Schedule Post panel inputs identically.
    """
    if not (sched_date and sched_time):
        return None
    import zoneinfo

    tz = zoneinfo.ZoneInfo(workspace.effective_timezone or "UTC")
    return datetime.combine(sched_date, sched_time).replace(tzinfo=tz)


def _capture_proposed_publish_at(post, post_id, workspace, form, *, clear_when_blank=True):
    """Set ``post.proposed_publish_at`` from the composer's Schedule Post panel.

    Used by the draft-stage save actions (save draft, submit/resubmit for
    approval) so a proposed time entered in the panel is persisted and shown in
    the drafts/approval lists. Mutates ``post`` in place; the caller persists it.

    A no-op once the post is committed to publishing — ``post.scheduled_at`` is
    set or any child is scheduled/publishing/published — because then the
    date/time inputs reflect the live schedule (pre-filled from ``scheduled_at``)
    and must not be reinterpreted as a proposal.

    ``clear_when_blank`` (default True, for Save Draft where the panel *is* the
    proposed-time editor) clears the proposal when the panel is empty. The
    approval submit/resubmit actions pass False so routing a draft through
    approval with an untouched/absent panel can't silently wipe a proposal an
    agent set via the REST/MCP API.
    """
    already_scheduled = post.scheduled_at is not None or (
        bool(post_id) and post.platform_posts.filter(status__in=["scheduled", "publishing", "published"]).exists()
    )
    if already_scheduled:
        return
    proposed = _combine_schedule_dt(
        workspace,
        form.cleaned_data.get("scheduled_date"),
        form.cleaned_data.get("scheduled_time"),
    )
    if proposed is None and not clear_when_blank:
        return
    post.proposed_publish_at = proposed


@login_required
@require_permission("create_posts")
@require_POST
def save_post(request, workspace_id, post_id=None):
    """Save or update a post (draft, schedule, or publish action)."""
    workspace = _get_workspace(request, workspace_id)
    action = request.POST.get("action", "save_draft")

    if action in _PUBLISH_COMMITTING_ACTIONS:
        from apps.onboarding.sample import SAMPLE_REFUSAL, is_sample_workspace

        # The example workspace holds sample drafts: nothing there is ever published.
        if is_sample_workspace(workspace.id):
            return JsonResponse({"errors": {"sample": SAMPLE_REFUSAL}}, status=400)

    if not post_id and action in {"schedule", "publish_now", "add_to_queue", "add_to_queue_priority"}:
        from apps.composer.services import _require_approval_gate_passes

        try:
            _require_approval_gate_passes(workspace)
        except ValueError as exc:
            return JsonResponse({"errors": {"approval": str(exc)}}, status=400)

    if post_id:
        post = get_object_or_404(Post, id=post_id, workspace=workspace)
        # Enforce edit permissions: authors can edit their own, others need edit_others_posts
        membership = request.workspace_membership
        perms = membership.effective_permissions if membership else {}
        if post.author != request.user and not perms.get("edit_others_posts", False):
            raise PermissionDenied("You do not have permission to edit this post.")
        # Reject before the form binds, so an already-published post is never
        # mutated in memory. Catches every action, including the ``save_draft``
        # default a POST with no ``action`` falls back to.
        readonly_error = _readonly_rejection(request, post)
        if readonly_error is not None:
            return readonly_error
        _orig_content = _base_content_snapshot(post)
        _orig_scheduled_at = post.scheduled_at
        form = PostForm(request.POST, instance=post)
    else:
        _orig_content = None
        _orig_scheduled_at = None
        form = PostForm(request.POST)

    if not form.is_valid():
        return JsonResponse({"errors": form.errors}, status=400)

    post = form.save(commit=False)
    post.workspace = workspace
    if not post_id:
        post.author = request.user

    pinterest_board_error = _validate_pinterest_board_selection(request, post, workspace)
    if pinterest_board_error is not None:
        return pinterest_board_error

    asset_error = _validate_platform_asset_selection(request, workspace)
    if asset_error is not None:
        return asset_error

    settings_error = _validate_post_settings(request, workspace, action)
    if settings_error is not None:
        return settings_error

    media_error = _validate_required_media(request, post, workspace, action)
    if media_error is not None:
        return media_error

    media_rules_error = _validate_platform_media_rules(request, post, workspace, action)
    if media_rules_error is not None:
        return media_rules_error

    try:
        thread_specs, thread_assets = _composer_thread_parts(request, workspace)
    except ValueError as exc:
        return JsonResponse({"errors": {"thread": str(exc)}}, status=400)
    thread_error = _validate_thread_parts(request, workspace, action, thread_specs, thread_assets)
    if thread_error is not None:
        return thread_error
    # Refused before anything is written: once a part is out on any channel,
    # the parts are that channel's record and can't be replaced.
    if thread_specs is not None and post.pk and _thread_parts_changed(post, thread_specs) and thread_started(post):
        return JsonResponse(
            {"errors": {"thread": "This thread has already started publishing, so its parts can't be changed."}},
            status=400,
        )

    allowance_error = _scheduled_post_allowance_error(request, post, workspace, action, form)
    if allowance_error is not None:
        return allowance_error

    try:
        recurrence = _recurrence_from_form(request)
    except ValueError as exc:
        return JsonResponse({"errors": {"recurrence": str(exc)}}, status=400)

    # Handle action — note that Post itself no longer carries an editorial
    # status: every transition below operates on the PlatformPost children,
    # which is why we sync those before/after running it.
    pending_target = None  # what to transition existing children to after sync
    initial_status = "draft"  # default status for newly created PlatformPosts

    if action == "schedule":
        aware_dt = _combine_schedule_dt(
            workspace,
            form.cleaned_data.get("scheduled_date"),
            form.cleaned_data.get("scheduled_time"),
        )
        if aware_dt:
            if aware_dt <= timezone.now():
                return JsonResponse(
                    {"errors": {"schedule": "Scheduled time must be in the future."}},
                    status=400,
                )
            post.scheduled_at = aware_dt
            # A committed schedule supersedes any draft-stage proposal.
            post.proposed_publish_at = None
            # Propagate the manually chosen time to every PlatformPost so all
            # selected platforms publish at the same moment.
            post._schedule_propagate_dt = aware_dt  # handled after post.save()
            pending_target = "scheduled"
            initial_status = "scheduled"
        else:
            return JsonResponse({"errors": {"schedule": "Date and time required."}}, status=400)
    elif action == "publish_now":
        # Server-side permission check - only roles with publish_directly can bypass approval
        membership = request.workspace_membership
        perms = membership.effective_permissions if membership else {}
        if not perms.get("publish_directly", False):
            raise PermissionDenied("You do not have permission to publish directly.")
        now_dt = timezone.now()
        post.scheduled_at = now_dt
        post.proposed_publish_at = None
        post._schedule_propagate_dt = now_dt  # handled after post.save()
        pending_target = "scheduled"
        initial_status = "scheduled"
    elif action == "add_to_queue":
        from django.db import transaction

        from apps.calendar.services import QueueFullError, add_to_queue

        queue_id = request.POST.get("queue_id")
        queues = _resolve_queues_for_post(queue_id, workspace, request.POST)
        if not queues:
            return JsonResponse({"errors": {"queue": "No active queue found for the selected channel."}}, status=400)
        # Queueing assigns real per-platform slots below — drop any proposal.
        post.proposed_publish_at = None
        post.save()
        # Ensure PlatformPost rows exist for every selected account before the
        # queue service writes per-platform scheduled_at values.
        _sync_platform_posts(request, post, workspace, initial_status="draft")
        thread_save_error = _save_thread_parts(post, thread_specs, thread_assets)
        if thread_save_error is not None:
            return thread_save_error
        # "Next Available" always places the post in the queue's soonest open
        # slot. It deliberately ignores the Schedule-panel date/time: those
        # inputs are prefilled from the post's own scheduled_at/proposed time
        # when editing, and treating them as a floor would push the post past
        # the true next slot.
        try:
            # One transaction across every queue: if a later queue is full, the
            # earlier queues' slot writes roll back instead of leaving a child
            # half-queued.
            with transaction.atomic():
                for q in queues:
                    add_to_queue(post, q)
                # Transition every child whose scheduled_at was filled in.
                _transition_post_children(
                    post, "scheduled", only=_scoped_platform_post_ids(request, post), audit_user=request.user
                )
        except QueueFullError:
            return JsonResponse({"errors": {"queue": _QUEUE_FULL_MSG}}, status=400)
        _save_version(post, request.user)
        if request.htmx:
            return HttpResponse(
                status=204,
                headers={
                    "HX-Trigger": json.dumps({"postSaved": {"postId": str(post.id), "status": post.status}}),
                    "X-Platform-Statuses": json.dumps(_platform_status_map(post)),
                },
            )
        return redirect("composer:compose_edit", workspace_id=workspace.id, post_id=post.id)
    elif action == "add_to_queue_priority":
        from django.db import transaction

        from apps.calendar.services import QueueFullError, add_to_queue

        queue_id = request.POST.get("queue_id")
        queues = _resolve_queues_for_post(queue_id, workspace, request.POST)
        if not queues:
            return JsonResponse({"errors": {"queue": "No active queue found for the selected channel."}}, status=400)
        # Queueing assigns real per-platform slots below — drop any proposal.
        post.proposed_publish_at = None
        post.save()
        _sync_platform_posts(request, post, workspace, initial_status="draft")
        thread_save_error = _save_thread_parts(post, thread_specs, thread_assets)
        if thread_save_error is not None:
            return thread_save_error
        try:
            # One transaction across every queue (see add_to_queue above).
            with transaction.atomic():
                for q in queues:
                    add_to_queue(post, q, priority=True)
                _transition_post_children(
                    post, "scheduled", only=_scoped_platform_post_ids(request, post), audit_user=request.user
                )
        except QueueFullError:
            return JsonResponse({"errors": {"queue": _QUEUE_FULL_MSG}}, status=400)
        _save_version(post, request.user)
        if request.htmx:
            return HttpResponse(
                status=204,
                headers={
                    "HX-Trigger": json.dumps({"postSaved": {"postId": str(post.id), "status": post.status}}),
                    "X-Platform-Statuses": json.dumps(_platform_status_map(post)),
                },
            )
        return redirect("composer:compose_edit", workspace_id=workspace.id, post_id=post.id)
    elif action == "submit_for_approval":
        # Save post first so it has a PK, then delegate to approval service
        _capture_proposed_publish_at(post, post_id, workspace, form, clear_when_blank=False)
        if recurrence is not None and recurrence["frequency"] != "never":
            recurrence_error = _recurrence_error(recurrence, post, workspace)
            if recurrence_error is not None:
                return recurrence_error
        post.save()
        # Sync platform posts before submitting
        _sync_platform_posts(request, post, workspace, initial_status="draft")
        thread_save_error = _save_thread_parts(post, thread_specs, thread_assets)
        if thread_save_error is not None:
            return thread_save_error
        _save_version(post, request.user)
        from apps.approvals.services import submit_for_review

        submit_for_review(post, request.user, workspace)
        _save_approval_recurrence(post, recurrence)
        if request.htmx:
            return HttpResponse(
                status=204,
                headers={
                    "HX-Trigger": json.dumps({"postSaved": {"postId": str(post.id), "status": post.status}}),
                    "X-Platform-Statuses": json.dumps(_platform_status_map(post)),
                },
            )
        return redirect("composer:compose_edit", workspace_id=workspace.id, post_id=post.id)
    elif action == "resubmit_for_approval":
        # Resubmit after changes requested or rejection
        _capture_proposed_publish_at(post, post_id, workspace, form, clear_when_blank=False)
        if recurrence is not None and recurrence["frequency"] != "never":
            recurrence_error = _recurrence_error(recurrence, post, workspace)
            if recurrence_error is not None:
                return recurrence_error
        post.save()
        _sync_platform_posts(request, post, workspace, initial_status="draft")
        thread_save_error = _save_thread_parts(post, thread_specs, thread_assets)
        if thread_save_error is not None:
            return thread_save_error
        _save_version(post, request.user)
        from apps.approvals.services import resubmit_post

        resubmit_post(post, request.user, workspace)
        _save_approval_recurrence(post, recurrence)
        if request.htmx:
            return HttpResponse(
                status=204,
                headers={
                    "HX-Trigger": json.dumps({"postSaved": {"postId": str(post.id), "status": post.status}}),
                    "X-Platform-Statuses": json.dumps(_platform_status_map(post)),
                },
            )
        return redirect("composer:compose_edit", workspace_id=workspace.id, post_id=post.id)
    elif action == "save_draft":
        # The Schedule Post panel doubles as the proposed-time picker for a
        # draft; capture it (or clear it when blank) before the save below.
        _capture_proposed_publish_at(post, post_id, workspace, form)
    # Fall through (save_draft / unknown action): persist below; existing
    # children are left as-is, new children default to draft via initial_status.

    # A series starts when the post is scheduled; one already repeating can be
    # re-set from any save that keeps its schedule. Checked before anything is
    # written so a bad setting leaves the post as it was.
    from apps.calendar.models import RecurrenceRule
    from apps.calendar.services import save_recurrence, stop_recurrence

    repeating = post.pk is not None and RecurrenceRule.objects.filter(post=post, is_active=True).exists()
    apply_recurrence = (
        recurrence is not None
        and recurrence["frequency"] != "never"
        and post.scheduled_at is not None
        and (action == "schedule" or repeating)
    )
    if apply_recurrence:
        recurrence_error = _recurrence_error(recurrence, post, workspace)
        if recurrence_error is not None:
            return recurrence_error

    post.save()

    # Attach pending session media for new posts
    if not post_id:
        from apps.media_library.models import MediaAsset as _MediaAsset

        session_key = f"pending_media_{workspace.id}"
        pending_ids = request.session.get(session_key, [])
        if pending_ids:
            for idx, asset_id in enumerate(pending_ids):
                try:
                    asset = _MediaAsset.objects.get(id=asset_id, workspace=workspace)
                    PostMedia.objects.get_or_create(
                        post=post,
                        media_asset=asset,
                        defaults={"position": idx},
                    )
                except _MediaAsset.DoesNotExist:
                    continue
            del request.session[session_key]

    # Sync any new tags to the Tag model
    _sync_tags_to_model(workspace, post.tags)

    # Sync platform posts (newly-created rows inherit ``initial_status`` for
    # this action — e.g. "scheduled" for schedule/publish_now, "draft" for
    # save_draft).
    _sync_platform_posts(request, post, workspace, initial_status=initial_status)
    thread_save_error = _save_thread_parts(post, thread_specs, thread_assets)
    if thread_save_error is not None:
        return thread_save_error

    # Propagate manually-chosen schedule/publish_now datetimes to every
    # PlatformPost now that they exist — except published/publishing rows
    # (their schedule is history) and, in scoped mode, siblings outside the
    # ``?account=`` scope.
    scoped_ids = _scoped_platform_post_ids(request, post)
    propagate_dt = getattr(post, "_schedule_propagate_dt", None)
    if propagate_dt is not None:
        propagate_qs = post.platform_posts.exclude(status__in=PlatformPost.PROTECTED_STATUSES)
        if scoped_ids is not None:
            propagate_qs = propagate_qs.filter(id__in=scoped_ids)
        propagate_qs.update(scheduled_at=propagate_dt)

    # Option A: editing an approved post's reviewable content sends it back for
    # re-approval so edits can't publish un-reviewed. Run this BEFORE applying any
    # publish target — otherwise a schedule/publish_now in the *same* save would
    # push the just-edited (no-longer-approved) content straight to scheduled.
    # Only ``approved`` children are reverted; anything else is a no-op.
    content_changed = _orig_content is not None and _base_content_snapshot(post) != _orig_content
    reverted_ids = {str(pp.id) for pp in _revert_approved_to_review(post)} if content_changed else set()

    # Move existing children to the requested target state (no-op for save_draft —
    # children that are already mid-workflow stay put). Children just reverted for
    # re-review are excluded so a same-save publish action can't drag them back
    # out of review.
    if pending_target:
        if reverted_ids:
            candidates = scoped_ids if scoped_ids is not None else [pp.id for pp in post.platform_posts.all()]
            target_only = [pid for pid in candidates if str(pid) not in reverted_ids]
        else:
            target_only = scoped_ids
        _transition_post_children(post, pending_target, only=target_only, audit_user=request.user)

    # The series is set up once the children have their final status, which
    # is what each generated copy inherits.
    if apply_recurrence:
        save_recurrence(post, **recurrence, refresh=_orig_scheduled_at != post.scheduled_at)
    elif recurrence is not None and recurrence["frequency"] == "never" and repeating:
        stop_recurrence(post)

    # Save version
    _save_version(post, request.user)

    # Return appropriate response
    if request.htmx:
        return HttpResponse(
            status=204,
            headers={
                "HX-Trigger": json.dumps(
                    {
                        "postSaved": {
                            "postId": str(post.id),
                            "status": post.status,
                        }
                    }
                ),
                "X-Platform-Statuses": json.dumps(_platform_status_map(post)),
            },
        )

    return redirect("composer:compose_edit", workspace_id=workspace.id, post_id=post.id)


@login_required
@require_POST
def transition_platform_post(request, workspace_id, post_id, platform_post_id):
    """Transition a single PlatformPost to a target editorial status.

    Used by the composer's per-account chip menu so the user can take a single
    social account in/out of draft, schedule, etc. without affecting siblings.
    Permission rules mirror save_post: ``approve_posts`` is required for any
    approval-stage target; ``publish_directly`` is required for scheduled.
    """
    workspace = _get_workspace(request, workspace_id)
    pp = get_object_or_404(
        PlatformPost.objects.select_related("post", "social_account"),
        id=platform_post_id,
        post_id=post_id,
        post__workspace=workspace,
    )
    target = (request.POST.get("target_status") or "").strip()
    if not target:
        return JsonResponse({"error": "target_status required"}, status=400)

    membership = request.workspace_membership
    perms = membership.effective_permissions if membership else {}
    approval_states = {"approved", "pending_review", "pending_client", "changes_requested", "rejected"}
    if target in ("scheduled", "publishing") and not perms.get("publish_directly", False):
        raise PermissionDenied("You do not have permission to schedule this post.")
    if target in approval_states and not perms.get("approve_posts", False) and target != "pending_review":
        raise PermissionDenied("You do not have permission to make approval decisions.")

    if pp.status == target:
        return JsonResponse({"ok": True, "status": pp.status, "noop": True})

    if target in ("scheduled", "publishing"):
        # save_post's own checks never see this endpoint: it moves one row
        # straight to the publisher, so a failed image-only TikTok row could
        # otherwise be "retried" into the same failure.
        try:
            require_publishable(pp)
        except ValueError as exc:
            return JsonResponse({"error": str(exc)}, status=400)

    try:
        if target == "scheduled":
            from apps.composer.services import transition_platform_post as transition_service

            transition_service(pp, "scheduled", scheduled_at=pp.scheduled_at or pp.post.scheduled_at)
            return JsonResponse({"ok": True, "status": pp.status, "platform_post_id": str(pp.id)})
        pp.transition_to(target)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    pp.save(update_fields=[*PlatformPost.TRANSITION_FIELDS, "updated_at"])
    # Committing a child to publishing obsoletes any draft-stage proposal.
    # Clear it directly rather than via sync_post_scheduled_at: this view sets
    # ``scheduled`` WITHOUT a ``scheduled_at``, and the publisher relies on the
    # ``Coalesce(scheduled_at, post__scheduled_at)`` fallback — recomputing the
    # Post.scheduled_at aggregate here could strand a post that depends on it.
    if target in ("scheduled", "publishing", "published") and pp.post.proposed_publish_at is not None:
        pp.post.proposed_publish_at = None
        pp.post.save(update_fields=["proposed_publish_at", "updated_at"])
    return JsonResponse({"ok": True, "status": pp.status, "platform_post_id": str(pp.id)})


@login_required
@require_permission("create_posts")
@require_POST
def autosave(request, workspace_id, post_id=None):
    """Auto-save endpoint called every 30 seconds via HTMX.

    On first save for a new post (no post_id), creates the draft and returns
    an HX-Trigger with the new post ID so the client can switch the autosave
    URL to the edit endpoint, preventing duplicate drafts on subsequent ticks.
    """
    workspace = _get_workspace(request, workspace_id)

    is_new = False
    orig_content = None
    if post_id:
        post = get_object_or_404(Post, id=post_id, workspace=workspace)
        # Enforce edit permissions on existing posts
        membership = request.workspace_membership
        perms = membership.effective_permissions if membership else {}
        if post.author != request.user and not perms.get("edit_others_posts", False):
            raise PermissionDenied("You do not have permission to edit this post.")
        # A published post is read-only: silently stop saving rather than
        # overwriting live content every 30 seconds. This returns 200 with the
        # indicator markup on purpose — the response swaps into #autosave-status,
        # so a 4xx here would fire the global error toast on every tick.
        if _is_readonly_write(request, post):
            return HttpResponse('<span class="text-xs text-gray-400">Read-only — not saved</span>')
        orig_content = _base_content_snapshot(post)
    else:
        # Check if a previous autosave already created a draft for this session
        # by looking for the post_id passed from the client
        client_post_id = request.POST.get("_autosave_post_id", "").strip()
        if client_post_id:
            try:
                post = Post.objects.get(id=client_post_id, workspace=workspace)
            except Post.DoesNotExist:
                post = Post(workspace=workspace, author=request.user)
                is_new = True
        else:
            post = Post(workspace=workspace, author=request.user)
            is_new = True

    post.title = request.POST.get("title", "")
    post.caption = request.POST.get("caption", "")
    post.first_comment = request.POST.get("first_comment", "")
    post.internal_notes = request.POST.get("internal_notes", "")

    post.tags = parse_and_truncate_tag_string(request.POST.get("tags", ""))

    post.save()

    # Attach pending session media when creating a new post
    if is_new:
        from apps.media_library.models import MediaAsset

        session_key = f"pending_media_{workspace.id}"
        pending_ids = request.session.get(session_key, [])
        if pending_ids:
            for idx, asset_id in enumerate(pending_ids):
                try:
                    asset = MediaAsset.objects.get(id=asset_id, workspace=workspace)
                    PostMedia.objects.get_or_create(
                        post=post,
                        media_asset=asset,
                        defaults={"position": idx},
                    )
                except MediaAsset.DoesNotExist:
                    continue
            del request.session[session_key]

    # Sync platform selections
    selected_ids = _parse_selected_account_ids(request.POST.get("selected_accounts", ""))
    _remove_deselected_platform_posts(request, post, selected_ids)
    for acc_id in selected_ids:
        PlatformPost.objects.get_or_create(
            post=post,
            social_account_id=acc_id,
        )

    # The thread editor's parts ride along like the caption does. A payload
    # that can't be stored is skipped rather than failing the tick: the
    # explicit save reports it.
    try:
        thread_specs, thread_assets = _composer_thread_parts(request, workspace)
    except ValueError:
        thread_specs, thread_assets = None, {}
    _save_thread_parts(post, thread_specs, thread_assets)

    # Option A: an autosave that changed an approved post's content reverts it to
    # pending_review so edited content can't publish without a fresh review.
    if orig_content is not None and _base_content_snapshot(post) != orig_content:
        _revert_approved_to_review(post)

    return HttpResponse(
        f'<span class="text-xs text-gray-400">Saved {timezone.now().strftime("%H:%M")}</span>',
        headers={"HX-Trigger": json.dumps({"autosaved": {"postId": str(post.id), "isNew": is_new}})},
    )


@login_required
@require_POST
def preview(request, workspace_id):
    """Live preview endpoint - renders platform-specific preview from form state.

    Called via HTMX with debounced POST from the composer.
    Stateless - no DB queries except social account lookup.
    """
    workspace = _get_workspace(request, workspace_id)
    title = request.POST.get("title", "")
    caption = request.POST.get("caption", "")
    first_comment = request.POST.get("first_comment", "")
    selected_ids = _parse_selected_account_ids(request.POST.get("selected_accounts", ""))

    # Build preview data per platform
    previews = []
    if selected_ids:
        accounts = SocialAccount.objects.filter(
            id__in=selected_ids,
            workspace=workspace,
        ).order_by("platform")
        for account in accounts:
            override_title_key = f"override_title_{account.id}"
            override_key = f"override_caption_{account.id}"
            effective_title = request.POST.get(override_title_key, "") or title
            effective_caption = request.POST.get(override_key, "") or caption
            char_limit = account.char_limit
            # Counted after escaping, so the badge matches what gets published.
            char_count = account.caption_wire_length(effective_caption)
            field_config = account.field_config
            previews.append(
                {
                    "account": account,
                    "title": effective_title,
                    "caption": effective_caption,
                    "first_comment": first_comment,
                    "char_count": char_count,
                    "char_limit": char_limit,
                    "is_over_limit": char_count > char_limit,
                    "truncated_caption": effective_caption[:char_limit]
                    if len(effective_caption) > char_limit
                    else effective_caption,
                    "needs_title": field_config["needs_title"],
                }
            )

    # Gather media for preview - check pending session media or post attachments
    from apps.media_library.models import MediaAsset

    media_items = []
    post_id_str = request.POST.get("_autosave_post_id", "")
    # The post the media checks below read from; unsaved until autosave runs.
    rules_post = Post(workspace=workspace)

    if post_id_str:
        try:
            post_obj = Post.objects.get(id=post_id_str, workspace=workspace)
            rules_post = post_obj
            for att in post_obj.media_attachments.select_related("media_asset").all():
                asset = att.media_asset
                media_items.append(
                    {
                        "url": asset.file.url if asset.file else "",
                        "is_video": asset.is_video,
                        "filename": asset.filename,
                    }
                )
        except Post.DoesNotExist:
            pass

    if not media_items:
        # Check session pending media
        session_key = f"pending_media_{workspace.id}"
        pending_ids = request.session.get(session_key, [])
        if pending_ids:
            for asset in MediaAsset.objects.filter(id__in=pending_ids, workspace=workspace):
                media_items.append(
                    {
                        "url": asset.file.url if asset.file else "",
                        "is_video": asset.is_video,
                        "filename": asset.filename,
                    }
                )

    # The thread as each network will publish it: every part's caption after
    # the account's override, counted against the platform's per-part limit,
    # with the reason a platform would refuse the thread, if any.
    try:
        thread_specs, thread_assets = _composer_thread_parts(request, workspace)
    except ValueError:
        thread_specs, thread_assets = None, {}
    for p in previews:
        p["thread"] = _thread_preview(
            p["account"], thread_specs or [], thread_assets, _composer_first_comment(request, p["account"])
        )

    # Each selected platform's media rules, re-checked on every round-trip so
    # the composer shows errors and warnings as media and accounts change.
    issues_by_account = _composer_media_issues(request, rules_post, workspace, [p["account"] for p in previews])
    media_issue_groups = []
    for p in previews:
        issues = issues_by_account.get(str(p["account"].id), [])
        if issues:
            media_issue_groups.append(
                {"account": p["account"], "issues": issues, "has_error": any(i.is_error for i in issues)}
            )

    return render(
        request,
        "composer/partials/preview_panel.html",
        {
            "previews": previews,
            "workspace": workspace,
            "media_items": media_items,
            "media_issue_groups": media_issue_groups,
            "media_issues_oob": True,
        },
    )


def _thread_preview(account, specs, asset_map, first_comment="") -> dict | None:
    """What the preview panel shows of the thread for one account, or None for a single post."""
    if not specs:
        return None
    capability = thread_capability_for(account.platform)
    limit = capability.part_max_length or account.char_limit
    parts = spec_thread_parts(account, specs, asset_map)
    return {
        "mode": capability.mode.value,
        "blocker": thread_blocker(account, parts, first_comment=first_comment),
        "parts": [
            {
                "number": number,
                "caption": caption,
                "char_count": account.caption_wire_length(caption),
                "char_limit": limit,
                "is_over_limit": account.caption_wire_length(caption) > limit,
                "media": [{"url": a.file.url if a.file else "", "is_video": a.is_video} for a in assets],
            }
            for number, (caption, assets) in enumerate(parts, start=2)
        ],
    }


@login_required
@require_GET
def media_picker(request, workspace_id, post_id=None):
    """Modal picker for selecting media from the library."""
    workspace = _get_workspace(request, workspace_id)
    from apps.media_library.models import MediaAsset

    post = None
    if post_id:
        post = get_object_or_404(Post, id=post_id, workspace=workspace)

    assets = MediaAsset.objects.for_workspace_with_shared(workspace.id, workspace.organization_id).order_by(
        "-created_at"
    )[:50]
    return render(
        request,
        "composer/partials/media_picker.html",
        {
            "assets": assets,
            "workspace": workspace,
            "post": post,
        },
    )


@login_required
@require_GET
def thumbnail_picker(request, workspace_id):
    """Modal picker for selecting an image asset as a YouTube thumbnail.

    Image-only, selection dispatches a client-side event; does not attach
    anything server-side.
    """
    workspace = _get_workspace(request, workspace_id)
    from apps.media_library.models import MediaAsset

    assets = (
        MediaAsset.objects.for_workspace(workspace.id)
        .filter(media_type=MediaAsset.MediaType.IMAGE)
        .order_by("-created_at")[:50]
    )
    return render(
        request,
        "composer/partials/thumbnail_picker.html",
        {"assets": assets, "workspace": workspace},
    )


@login_required
@require_GET
def thread_media_picker(request, workspace_id):
    """Modal picker for a thread part's media.

    Like ``thumbnail_picker``, selection dispatches a client-side event and
    attaches nothing server-side: the part's media are saved with the thread.
    """
    workspace = _get_workspace(request, workspace_id)
    assets = (
        MediaAsset.objects.for_workspace(workspace.id)
        .filter(media_type__in=[MediaAsset.MediaType.IMAGE, MediaAsset.MediaType.VIDEO])
        .order_by("-created_at")[:50]
    )
    return render(
        request,
        "composer/partials/thread_media_picker.html",
        {"assets": assets, "workspace": workspace},
    )


@login_required
@require_POST
def resume_thread(request, workspace_id, post_id, platform_post_id):
    """Resume a thread that stopped part-way, from the part where it stopped.

    Publishes the rest of the thread, so it takes the same ``publish_directly``
    permission as publishing now. The parts already live are never re-sent.
    """
    from apps.publisher.engine import resume_thread as resume

    workspace = _get_workspace(request, workspace_id)
    membership = request.workspace_membership
    perms = membership.effective_permissions if membership else {}
    if not perms.get("publish_directly", False):
        raise PermissionDenied("You do not have permission to publish directly.")
    platform_post = get_object_or_404(
        PlatformPost.objects.select_related("social_account", "post"),
        id=platform_post_id,
        post_id=post_id,
        post__workspace=workspace,
    )
    try:
        resume(platform_post, confirm_unconfirmed=request.POST.get("confirm_unconfirmed") == "1")
    except ValueError as exc:
        return JsonResponse({"errors": {"thread": str(exc)}}, status=400)
    # The person did what the "thread paused" entry in Activity was waiting for.
    resolve_paused_thread(platform_post, request.user)
    if request.htmx:
        return HttpResponse(status=204, headers={"HX-Refresh": "true"})
    return redirect("composer:compose_edit", workspace_id=workspace.id, post_id=post_id)


@login_required
@require_POST
def thumbnail_upload(request, workspace_id):
    """Upload an image from the local machine to the media library and return
    its id + URL so the composer can wire it as a YouTube thumbnail.

    Image-only. Returns JSON with asset_id, url, filename.
    """
    workspace = _get_workspace(request, workspace_id)
    uploaded_file = request.FILES.get("file")

    if not uploaded_file:
        return JsonResponse({"error": "No file provided"}, status=400)

    content_type = uploaded_file.content_type or ""
    if not content_type.startswith("image/"):
        return JsonResponse({"error": "Only image files are allowed"}, status=400)

    from apps.media_library.models import MediaAsset

    asset = MediaAsset.objects.create(
        organization=workspace.organization,
        workspace=workspace,
        uploaded_by=request.user,
        file=uploaded_file,
        filename=uploaded_file.name,
        media_type=MediaAsset.MediaType.IMAGE,
        mime_type=content_type,
        file_size=uploaded_file.size,
        source="upload",
    )

    url = ""
    if asset.thumbnail:
        url = asset.thumbnail.url
    elif asset.file:
        url = asset.file.url

    return JsonResponse(
        {
            "asset_id": str(asset.id),
            "url": url,
            "filename": asset.filename,
        }
    )


_RANGE_HEADER_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


# Response chunk size for streamed media. Matches _RangeFileIterator's default
# so the local and object-storage paths behave alike.
_STREAM_CHUNK_SIZE = 64 * 1024


def _iter_and_close(body, chunk_size=_STREAM_CHUNK_SIZE):
    """Yield an object-storage body's chunks, closing it however iteration ends.

    botocore's ``StreamingBody.iter_chunks`` is a bare ``while True: yield``
    with no ``try/finally``, and Django only registers *this* generator's
    ``close`` on the response — so closing the response unwinds the generator
    without ever calling ``StreamingBody.close()``, and the underlying urllib3
    connection is never returned to the pool. A seeking ``<video>`` aborts range
    requests constantly, which is exactly how a pool of 10 runs dry.
    """
    try:
        yield from body.iter_chunks(chunk_size)
    finally:
        with contextlib.suppress(Exception):
            body.close()


# Frame-picker filmstrips are derived from immutable bytes, so this only bounds
# how long a cache entry squats on memory, not how stale it can get.
FILMSTRIP_CACHE_SECONDS = 60 * 60


class _RangeFileIterator:
    """Iterate a bounded byte window of an already-positioned file handle."""

    def __init__(self, file_handle, remaining, chunk_size=64 * 1024):
        self.file_handle = file_handle
        self.remaining = remaining
        self.chunk_size = chunk_size

    def __iter__(self):
        return self

    def __next__(self):
        if self.remaining <= 0:
            raise StopIteration
        data = self.file_handle.read(min(self.chunk_size, self.remaining))
        if not data:
            raise StopIteration
        self.remaining -= len(data)
        return data

    def close(self):
        if hasattr(self.file_handle, "close"):
            self.file_handle.close()


@login_required
@require_GET
def media_stream(request, workspace_id, asset_id):
    """Stream a media asset through the app's own origin, with Range support.

    The composer's frame picker draws video frames onto a canvas, which the
    browser only allows for same-origin (or CORS-approved) media. Object
    storage like S3/R2 serves signed URLs from another origin, usually
    without CORS headers, so the raw file URL would taint the canvas.

    Byte-range requests matter here: without them the browser can't seek a
    <video> beyond what it has buffered (the scrubber and filmstrip clicks
    silently do nothing) and has to download the whole file up front.
    """
    workspace = _get_workspace(request, workspace_id)

    from apps.media_library.models import MediaAsset

    asset = get_object_or_404(
        MediaAsset.objects.for_workspace_with_shared(
            workspace_id=workspace.id,
            organization_id=workspace.organization_id,
        ),
        pk=asset_id,
    )
    if not asset.file:
        raise Http404
    from apps.media_library.storage import is_s3_backend, open_object_range

    # On object storage, ask for exactly the window the browser asked for.
    # Opening the FieldFile instead would pull the ENTIRE video down before
    # serving a 64 KiB slice — and a seeking <video> issues many such requests,
    # which is how one frame-picker session could exhaust a web dyno.
    remote = is_s3_backend()

    # The DB row can outlive the stored object (lifecycle rule, manual S3
    # deletion); opening/stat-ing it then raises a backend error rather than
    # returning an empty FieldFile, so map that to 404 instead of a 500.
    try:
        size = asset.file.size
        file_handle = None if remote else asset.file.open("rb")
    except Exception:  # noqa: BLE001 - storage backends raise varied errors (OSError, botocore ClientError)
        raise Http404 from None

    content_type = asset.mime_type or "application/octet-stream"
    range_match = _RANGE_HEADER_RE.match(request.headers.get("Range", ""))

    try:
        if range_match and size:
            start_str, end_str = range_match.groups()
            if not start_str:
                # Suffix range: the last N bytes.
                length = min(int(end_str or 0), size)
                start = size - length
                end = size - 1
            else:
                start = int(start_str)
                end = min(int(end_str), size - 1) if end_str else size - 1
            if start >= size or start > end:
                if file_handle is not None:
                    file_handle.close()
                response = HttpResponse(status=416)
                response["Content-Range"] = f"bytes */{size}"
                return response
            if remote:
                body = open_object_range(asset.file.name, start, end)
                response = StreamingHttpResponse(
                    _iter_and_close(body),
                    status=206,
                    content_type=content_type,
                )
            else:
                file_handle.seek(start)
                response = StreamingHttpResponse(
                    _RangeFileIterator(file_handle, end - start + 1),
                    status=206,
                    content_type=content_type,
                )
            response["Content-Length"] = str(end - start + 1)
            response["Content-Range"] = f"bytes {start}-{end}/{size}"
        elif remote:
            body = open_object_range(asset.file.name)
            response = StreamingHttpResponse(
                _iter_and_close(body),
                content_type=content_type,
            )
            response["Content-Length"] = str(size)
        else:
            response = FileResponse(file_handle, content_type=content_type)
    except FileNotFoundError:
        # Only a vanished object is a 404. Any other exception here is a bug in
        # the range arithmetic or the response construction, and swallowing it
        # as a 404 would hide it from the logs while the frame picker silently
        # did nothing.
        if file_handle is not None:
            file_handle.close()
        raise Http404 from None
    except Exception:
        if file_handle is not None:
            file_handle.close()
        logger.exception("media_stream failed for asset %s", asset.pk)
        raise

    response["Accept-Ranges"] = "bytes"
    # Asset files are immutable per id - let the browser cache the stream so
    # reopening the frame picker doesn't re-download the whole video.
    response["Cache-Control"] = "private, max-age=3600"
    return response


@login_required
@require_GET
def media_filmstrip(request, workspace_id, asset_id):
    """Return evenly-spaced thumbnail frames for the frame-picker filmstrip.

    Extracted server-side with ffmpeg, which seeks each frame via byte ranges
    instead of making the browser download (and decode) most of the video to
    build the strip client-side.
    """
    workspace = _get_workspace(request, workspace_id)

    import os
    import tempfile

    from apps.media_library.models import MediaAsset
    from apps.media_library.services import extract_video_frames, extract_video_metadata
    from apps.media_library.storage import download_to_path

    asset = get_object_or_404(
        MediaAsset.objects.for_workspace_with_shared(
            workspace_id=workspace.id,
            organization_id=workspace.organization_id,
        ),
        pk=asset_id,
    )
    if asset.media_type != MediaAsset.MediaType.VIDEO or not asset.file:
        raise Http404

    # An asset's bytes never change once stored, so the strip for a given id is
    # fixed. Worth caching: building it costs a full download plus eight ffmpeg
    # runs inside a single web request, and the picker gets reopened a lot.
    # Its own bounded alias, not the shared default cache: these entries are
    # base64 JPEGs, big enough that 300 of them would be a memory problem of
    # exactly the kind this endpoint was changed to avoid.
    filmstrip_cache = caches["filmstrip"]
    cache_key = f"composer:filmstrip:{asset.pk}"
    cached = filmstrip_cache.get(cache_key)
    if cached is not None:
        return JsonResponse(cached)

    # Mirror media_library's video pipeline: pull the file to one local temp
    # file, then run all the ffmpeg seeks against it. Extracting each frame
    # straight from the (remote) signed URL re-opens the connection and
    # re-reads the moov atom every time - on R2 that was ~1.5s per frame.
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=f".{asset.file_extension}", delete=False) as tmp:
            tmp_path = tmp.name
        download_to_path(asset.file, tmp_path)
    except Exception:  # noqa: BLE001 - storage backends raise varied errors when the object is gone
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise Http404 from None

    try:
        duration = asset.duration or extract_video_metadata(tmp_path).get("duration_seconds") or 0
        if not duration:
            return JsonResponse({"error": "Could not read video duration."}, status=502)

        count = 8
        timestamps = [round(duration * (i + 0.5) / count, 3) for i in range(count)]
        jpegs = extract_video_frames(tmp_path, timestamps, width=160)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)

    frames = [
        {"time": t, "dataUrl": "data:image/jpeg;base64," + base64.b64encode(jpg).decode()}
        for t, jpg in zip(timestamps, jpegs, strict=False)
        if jpg
    ]
    if not frames:
        return JsonResponse({"error": "Could not extract frames."}, status=502)
    payload = {"frames": frames, "duration": duration}
    filmstrip_cache.set(cache_key, payload, FILMSTRIP_CACHE_SECONDS)
    return JsonResponse(payload)


UNSPLASH_API_BASE = "https://api.unsplash.com"
UNSPLASH_IMAGE_HOST = "https://images.unsplash.com/"
UNSPLASH_NOT_CONFIGURED = (
    "Unsplash is not configured. Add UNSPLASH_ACCESS_KEY to your environment to enable stock-photo search."
)
UNSPLASH_MAX_IMPORT = 10
UNSPLASH_MAX_IMAGE_BYTES = 15 * 1024 * 1024


def _as_str(value):
    """Coerce an external/client JSON field to a string.

    Client- and Unsplash-supplied payloads can carry ``null`` (or numbers)
    where we expect a URL/text, so guard before ``.startswith()`` or feeding
    a model's non-null string column - otherwise an ``AttributeError`` (or
    IntegrityError) turns a should-fail-gracefully path into a 500.
    """
    return value if isinstance(value, str) else ""


@login_required
@require_GET
def unsplash_search(request, workspace_id):
    """Proxy an Unsplash photo search so the API key stays server-side.

    Returns results trimmed to what the composer modal needs. Grid images are
    hotlinked from the Unsplash CDN per their API guidelines.
    """
    _get_workspace(request, workspace_id)

    if not settings.UNSPLASH_ACCESS_KEY:
        return JsonResponse({"error": UNSPLASH_NOT_CONFIGURED}, status=503)

    query = request.GET.get("q", "").strip()
    if not query:
        return JsonResponse({"error": "Missing search query"}, status=400)

    try:
        resp = httpx.get(
            f"{UNSPLASH_API_BASE}/search/photos",
            params={"query": query, "page": 1, "per_page": 24},
            headers={
                "Authorization": f"Client-ID {settings.UNSPLASH_ACCESS_KEY}",
                "Accept-Version": "v1",
            },
            timeout=10.0,
        )
    except httpx.RequestError:
        return JsonResponse({"error": "Could not reach Unsplash. Try again."}, status=502)

    if resp.status_code in (401, 403):
        return JsonResponse({"error": "Unsplash rejected the API key. Check UNSPLASH_ACCESS_KEY."}, status=502)
    if resp.status_code == 429:
        return JsonResponse({"error": "Unsplash rate limit reached. Try again in a few minutes."}, status=429)
    if resp.status_code != 200:
        return JsonResponse({"error": "Unsplash search failed. Try again."}, status=502)

    # A 200 with a malformed body or an unexpected result shape (e.g. a bare
    # list or null instead of an object) must surface as a friendly 502, not
    # an unhandled 500.
    try:
        data = resp.json()
        if not isinstance(data, dict):
            raise ValueError("Unexpected Unsplash response shape")
        results = [
            {
                "id": p["id"],
                "thumb": p["urls"]["small"],
                "full": p["urls"]["regular"],
                "width": p.get("width"),
                "height": p.get("height"),
                "color": p.get("color"),
                "alt": p.get("alt_description") or p.get("description") or "",
                "photographer": p["user"]["name"],
                "photographer_url": p["user"]["links"]["html"],
                "photo_url": p["links"]["html"],
                "download_location": p["links"]["download_location"],
            }
            for p in data.get("results", [])
        ]
        total = data.get("total", 0)
    except (ValueError, KeyError, TypeError, AttributeError):
        return JsonResponse({"error": "Unexpected response from Unsplash. Try again."}, status=502)
    return JsonResponse({"results": results, "total": total})


@login_required
@require_POST
def unsplash_import(request, workspace_id, post_id=None):
    """Download selected Unsplash photos server-side and attach them as media.

    Mirrors upload_media: attaches to the post when post_id is given, else
    queues in the pending-media session. Hits each photo's download_location
    first, as Unsplash's guidelines require when a photo is actually used.
    """
    workspace = _get_workspace(request, workspace_id)

    if not settings.UNSPLASH_ACCESS_KEY:
        return JsonResponse({"error": UNSPLASH_NOT_CONFIGURED}, status=503)

    try:
        payload = json.loads(request.body)
        photos = payload["photos"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return JsonResponse({"error": "Invalid request body"}, status=400)

    if not isinstance(photos, list) or not photos:
        return JsonResponse({"error": "No photos selected"}, status=400)
    if len(photos) > UNSPLASH_MAX_IMPORT:
        return JsonResponse({"error": f"Select at most {UNSPLASH_MAX_IMPORT} photos per import."}, status=400)

    from django.core.files.base import ContentFile

    from apps.media_library.models import MediaAsset
    from apps.media_library.quotas import StorageQuotaExceededError, enforce_storage_quota

    post = None
    if post_id:
        post = get_object_or_404(Post, id=post_id, workspace=workspace)

    auth_headers = {"Authorization": f"Client-ID {settings.UNSPLASH_ACCESS_KEY}"}
    new_assets = []
    attachments = []
    failed = 0
    quota_exceeded = False

    # follow_redirects is OFF: the host allowlist below is only meaningful if a
    # 30x can't bounce the request to an internal address (SSRF). One pooled
    # client reuses connections across the per-photo registration + download.
    with httpx.Client(follow_redirects=False, timeout=30.0) as client:
        for photo in photos:
            if not isinstance(photo, dict):
                failed += 1
                continue
            photo_id = _as_str(photo.get("id")).strip()
            download_location = _as_str(photo.get("download_location"))
            fallback_url = _as_str(photo.get("full"))
            # The client supplies these URLs - only ever fetch from Unsplash hosts.
            if not photo_id or not download_location.startswith(f"{UNSPLASH_API_BASE}/"):
                failed += 1
                continue

            content = None
            content_type = ""
            try:
                # Register the download (required by Unsplash API guidelines);
                # the response carries the actual file URL to fetch. A non-JSON
                # body here must fail this one photo, not the whole request.
                dl_resp = client.get(download_location, headers=auth_headers, timeout=10.0)
                image_url = ""
                if dl_resp.status_code == 200:
                    # A non-JSON or non-object body must fail this one photo,
                    # not 500 the whole request.
                    with contextlib.suppress(ValueError, AttributeError):
                        image_url = _as_str(dl_resp.json().get("url"))
                if not image_url:
                    image_url = fallback_url
                if not image_url.startswith(UNSPLASH_IMAGE_HOST):
                    failed += 1
                    continue

                # Stream and cap the download so a huge (or mis-declared) body
                # can't be buffered whole - reject before exceeding the limit.
                with client.stream("GET", image_url) as img_resp:
                    content_type = img_resp.headers.get("content-type", "")
                    declared = img_resp.headers.get("content-length", "")
                    if (
                        img_resp.status_code != 200
                        or not content_type.startswith("image/")
                        or (declared.isdigit() and int(declared) > UNSPLASH_MAX_IMAGE_BYTES)
                    ):
                        failed += 1
                        continue
                    buf = bytearray()
                    for chunk in img_resp.iter_bytes():
                        buf += chunk
                        if len(buf) > UNSPLASH_MAX_IMAGE_BYTES:
                            break
                    if len(buf) > UNSPLASH_MAX_IMAGE_BYTES:
                        failed += 1
                        continue
                    content = bytes(buf)
            except httpx.HTTPError:
                failed += 1
                continue

            try:
                enforce_storage_quota(workspace.organization, len(content))
            except StorageQuotaExceededError:
                quota_exceeded = True
                break

            filename = f"unsplash-{photo_id}.jpg"
            asset = MediaAsset.objects.create(
                organization=workspace.organization,
                workspace=workspace,
                uploaded_by=request.user,
                file=ContentFile(content, name=filename),
                filename=filename,
                media_type=MediaAsset.MediaType.IMAGE,
                mime_type=content_type,
                file_size=len(content),
                source="unsplash",
                source_url=_as_str(photo.get("photo_url")),
                attribution=f"Photo by {_as_str(photo.get('photographer')) or 'Unknown'} on Unsplash",
                alt_text=_as_str(photo.get("alt")),
            )
            new_assets.append(asset)
            attachment = _attach_asset_for_composer(request, workspace, asset, post)
            if attachment is not None:
                attachments.append(attachment)

    if not new_assets:
        if quota_exceeded:
            return JsonResponse(
                {"error": "Storage quota exceeded. Free up space or upgrade your plan."},
                status=413,
            )
        return JsonResponse({"error": "Could not import the selected photos."}, status=502)

    if post is not None:
        html = render_to_string(
            "composer/partials/media_list.html",
            {"media_attachments": attachments, "post": post, "workspace": workspace},
            request=request,
        )
    else:
        html = render_to_string(
            "composer/partials/media_list_pending.html",
            {"pending_assets": new_assets, "workspace": workspace},
            request=request,
        )

    return JsonResponse(
        {
            "html": html,
            "assets": [{"id": str(a.id), "url": a.file.url} for a in new_assets],
            "failed": failed,
        }
    )


@login_required
@require_GET
def pinterest_boards(request, workspace_id, account_id):
    """Fetch Pinterest boards for board selection in the composer."""
    workspace = _get_workspace(request, workspace_id)
    account = get_object_or_404(SocialAccount, id=account_id, workspace=workspace, platform="pinterest")

    from apps.credentials.models import resolve_platform_credentials
    from providers import get_provider

    # .env is dominant; admin-entered org credentials are the fallback.
    credentials = resolve_platform_credentials("pinterest", workspace.organization_id)

    provider = get_provider("pinterest", credentials)

    # Refresh token if expiring
    access_token = account.oauth_access_token
    if account.token_expires_at and account.is_token_expiring_soon:
        try:
            access_token = account.refresh_oauth_token(provider, enqueue_backfill=False)
        except Exception:
            return JsonResponse({"error": "Token refresh failed"}, status=502)

    try:
        boards = provider.get_boards(access_token)
    except Exception:
        return JsonResponse({"error": "Failed to fetch boards"}, status=502)

    return JsonResponse({"boards": [{"id": b.get("id"), "name": b.get("name")} for b in boards]})


@login_required
@require_GET
def post_setting_options(request, workspace_id, account_id, key):
    """Live options for a platform's per-post setting (the composer's select fields)."""
    workspace = _get_workspace(request, workspace_id)
    account = get_object_or_404(SocialAccount, id=account_id, workspace=workspace)
    declared = {field.key: field for field in post_settings_for(account.platform) if field.options_from_provider}
    field = declared.get(key)
    if field is None:
        raise Http404("Unknown setting")

    from apps.publisher.engine import _provider_and_access_token

    try:
        provider, access_token = _provider_and_access_token(account)
        options = provider.get_setting_options(access_token, key, request.GET.get("depends", ""))
    except Exception:
        logger.warning("Could not load %s options for %s", key, account, exc_info=True)
        return JsonResponse({"error": "Could not load options"}, status=502)
    return JsonResponse({"options": [{"id": o["id"], "name": o["name"]} for o in options]})


@login_required
@require_GET
def tiktok_creator_info(request, workspace_id, account_id):
    """Fetch TikTok creator info for the composer's TikTok settings panel.

    TikTok's integration guidelines require querying this fresh before each
    post: the allowed privacy levels depend on the app's audit status and the
    creator's account settings.
    """
    workspace = _get_workspace(request, workspace_id)
    account = get_object_or_404(SocialAccount, id=account_id, workspace=workspace, platform="tiktok")

    from apps.credentials.models import resolve_platform_credentials
    from providers import get_provider

    credentials = resolve_platform_credentials("tiktok", workspace.organization_id)
    provider = get_provider("tiktok", credentials)

    # This endpoint enriches the composer panel; the real privacy/audit gate is
    # enforced at publish time. When the creator-info lookup can't run (no
    # token, expired token, TikTok API down), degrade to a 200 with empty
    # options and let the panel fall back to its defaults, rather than logging
    # a 5xx in the browser on every composer load.
    def _unavailable(reason):
        return JsonResponse(
            {
                "available": False,
                "error": reason,
                "creator_nickname": "",
                "privacy_level_options": [],
                "comment_disabled": False,
                "duet_disabled": False,
                "stitch_disabled": False,
                "max_video_post_duration_sec": None,
            }
        )

    # Refresh token if expiring
    access_token = account.oauth_access_token
    if account.token_expires_at and account.is_token_expiring_soon:
        try:
            access_token = account.refresh_oauth_token(provider, enqueue_backfill=False)
        except Exception:
            return _unavailable("Token refresh failed")

    try:
        info = provider.query_creator_info(access_token)
    except Exception:
        return _unavailable("Failed to fetch creator info")

    return JsonResponse(
        {
            "available": True,
            "creator_nickname": info.get("creator_nickname", ""),
            "privacy_level_options": info.get("privacy_level_options") or [],
            "comment_disabled": bool(info.get("comment_disabled")),
            "duet_disabled": bool(info.get("duet_disabled")),
            "stitch_disabled": bool(info.get("stitch_disabled")),
            "max_video_post_duration_sec": info.get("max_video_post_duration_sec"),
        }
    )


@login_required
@require_POST
def attach_media(request, workspace_id, post_id):
    """Attach a media asset to a post."""
    workspace = _get_workspace(request, workspace_id)
    post = get_object_or_404(Post, id=post_id, workspace=workspace)
    media_asset_id = request.POST.get("media_asset_id")

    if not media_asset_id:
        return JsonResponse({"error": "media_asset_id required"}, status=400)

    from apps.media_library.models import MediaAsset

    asset = get_object_or_404(
        MediaAsset.objects.for_workspace_with_shared(workspace.id, workspace.organization_id),
        id=media_asset_id,
    )

    max_pos = post.media_attachments.aggregate(models.Max("position"))["position__max"]
    position = (max_pos or 0) + 1

    attachment, _ = PostMedia.objects.get_or_create(
        post=post,
        media_asset=asset,
        defaults={"position": position},
    )

    # Option A: changing media on an approved post sends it back for re-approval.
    _revert_approved_to_review(post)

    response = render(
        request,
        "composer/partials/media_list.html",
        {
            "media_attachments": [attachment],
            "post": post,
            "workspace": workspace,
        },
    )
    response["HX-Trigger"] = "previewUpdate"
    return response


@login_required
@require_POST
def attach_pending_media(request, workspace_id):
    """Attach a library media asset as pending (before post is saved)."""
    workspace = _get_workspace(request, workspace_id)
    media_asset_id = request.POST.get("media_asset_id")

    if not media_asset_id:
        return JsonResponse({"error": "media_asset_id required"}, status=400)

    from apps.media_library.models import MediaAsset

    asset = get_object_or_404(
        MediaAsset.objects.for_workspace_with_shared(workspace.id, workspace.organization_id),
        id=media_asset_id,
    )

    session_key = f"pending_media_{workspace.id}"
    pending = request.session.get(session_key, [])
    asset_id_str = str(asset.id)
    if asset_id_str not in pending:
        pending.append(asset_id_str)
        request.session[session_key] = pending

    response = render(
        request,
        "composer/partials/media_list_pending.html",
        {
            "pending_assets": [asset],
            "workspace": workspace,
        },
    )
    response["HX-Trigger"] = "previewUpdate"
    return response


def _attach_asset_for_composer(request, workspace, asset, post=None):
    """Attach an asset to a post, or queue it in the pending-media session.

    Post path: migrates any session-pending media first (can happen when the
    media picker still uses attach_pending_media after autosave created the
    post and updated the upload URL), then appends the asset at the next
    position. Returns the PostMedia attachment, or None on the pending path.
    """
    from apps.media_library.models import MediaAsset

    session_key = f"pending_media_{workspace.id}"

    if post is not None:
        pending_ids = request.session.get(session_key, [])
        if pending_ids:
            existing_pos = post.media_attachments.aggregate(models.Max("position"))["position__max"] or 0
            for idx, pid in enumerate(pending_ids):
                try:
                    pending_asset = MediaAsset.objects.get(id=pid, workspace=workspace)
                    PostMedia.objects.get_or_create(
                        post=post,
                        media_asset=pending_asset,
                        defaults={"position": existing_pos + idx + 1},
                    )
                except MediaAsset.DoesNotExist:
                    continue
            del request.session[session_key]

        max_pos = post.media_attachments.aggregate(models.Max("position"))["position__max"]
        position = (max_pos or 0) + 1
        return PostMedia.objects.create(post=post, media_asset=asset, position=position)

    # No post yet - store pending media IDs in session so they can be
    # attached when the post is eventually saved.
    pending = request.session.get(session_key, [])
    pending.append(str(asset.id))
    request.session[session_key] = pending
    return None


@login_required
@require_POST
def upload_media(request, workspace_id, post_id=None):
    """Upload a file directly from the composer and optionally attach to a post."""
    workspace = _get_workspace(request, workspace_id)
    uploaded_file = request.FILES.get("file")

    if not uploaded_file:
        return JsonResponse({"error": "No file provided"}, status=400)

    from apps.media_library.models import MediaAsset

    # Determine media type
    content_type = uploaded_file.content_type or ""
    if content_type.startswith("image/"):
        media_type = MediaAsset.MediaType.IMAGE
    elif content_type.startswith("video/"):
        media_type = MediaAsset.MediaType.VIDEO
    elif content_type == "image/gif":
        media_type = MediaAsset.MediaType.GIF
    else:
        media_type = MediaAsset.MediaType.DOCUMENT

    asset = MediaAsset.objects.create(
        organization=workspace.organization,
        workspace=workspace,
        uploaded_by=request.user,
        file=uploaded_file,
        filename=uploaded_file.name,
        media_type=media_type,
        mime_type=content_type,
        file_size=uploaded_file.size,
        source="upload",
    )

    if post_id:
        post = get_object_or_404(Post, id=post_id, workspace=workspace)
        attachment = _attach_asset_for_composer(request, workspace, asset, post)
        # Option A: changing media on an approved post sends it back for re-approval.
        _revert_approved_to_review(post)
        response = render(
            request,
            "composer/partials/media_list.html",
            {
                "media_attachments": [attachment],
                "post": post,
                "workspace": workspace,
            },
        )
    else:
        _attach_asset_for_composer(request, workspace, asset)
        response = render(
            request,
            "composer/partials/media_list_pending.html",
            {
                "pending_assets": [asset],
                "workspace": workspace,
            },
        )

    response["X-Uploaded-Asset-Id"] = str(asset.id)
    response["X-Uploaded-Asset-Url"] = asset.file.url
    return response


@login_required
@require_POST
def remove_media(request, workspace_id, post_id, media_id):
    """Remove a media attachment from a post."""
    workspace = _get_workspace(request, workspace_id)
    post = get_object_or_404(Post, id=post_id, workspace=workspace)
    attachment = PostMedia.objects.filter(id=media_id, post=post).first()
    if attachment is not None:
        # Taking the last video off a post already scheduled to TikTok, or the
        # last image off a pin, would leave it queued to fail. Plain text,
        # because the global htmx error toast shows the body as-is and a 4xx
        # doesn't swap.
        reason = media_change_blocker(
            post,
            media_types_after=post_media_types(post, excluding=attachment),
            assets_after=post_media_assets(post, excluding=attachment),
        )
        if reason:
            return HttpResponse(reason, status=400, content_type="text/plain")
    PostMedia.objects.filter(id=media_id, post=post).delete()

    # Option A: changing media on an approved post sends it back for re-approval.
    _revert_approved_to_review(post)

    response = render(
        request,
        "composer/partials/media_list.html",
        {
            "media_attachments": post.media_attachments.select_related("media_asset").all(),
            "post": post,
            "workspace": workspace,
        },
    )
    response["HX-Trigger"] = "previewUpdate"
    return response


@login_required
@require_POST
def remove_pending_media(request, workspace_id, asset_id):
    """Remove a pending media asset (before post is saved)."""
    workspace = _get_workspace(request, workspace_id)

    from apps.media_library.models import MediaAsset
    from apps.media_library.services import delete_asset

    session_key = f"pending_media_{workspace.id}"
    pending = request.session.get(session_key, [])
    asset_id_str = str(asset_id)
    if asset_id_str in pending:
        pending.remove(asset_id_str)
        request.session[session_key] = pending

    # Delete the asset and its files from storage (R2)
    asset = MediaAsset.objects.filter(id=asset_id, workspace=workspace).first()
    if asset:
        with contextlib.suppress(Exception):
            delete_asset(asset)

    # Return updated pending list
    pending_assets = MediaAsset.objects.filter(id__in=pending, workspace=workspace)
    response = render(
        request,
        "composer/partials/media_list_pending.html",
        {
            "pending_assets": pending_assets,
            "workspace": workspace,
        },
    )
    response["HX-Trigger"] = "previewUpdate"
    return response


@login_required
@require_GET
def drafts_list(request, workspace_id):
    """List all drafts for this workspace."""
    workspace = _get_workspace(request, workspace_id)
    # A post is a "draft" when at least one of its PlatformPost children is in
    # the draft state and none have moved into a more advanced workflow stage.
    # Easiest correct query: any post whose only child statuses are "draft".
    drafts = (
        Post.objects.for_workspace(workspace.id)
        .filter(platform_posts__status="draft")
        .exclude(
            platform_posts__status__in=[
                "pending_review",
                "pending_client",
                "approved",
                "scheduled",
                "publishing",
                "published",
            ]
        )
        .distinct()
        .select_related("author")
        .prefetch_related("platform_posts__social_account")
        .order_by("-updated_at")
    )

    return render(
        request,
        "composer/drafts_list.html",
        {
            "workspace": workspace,
            "drafts": drafts,
        },
    )


@login_required
@require_POST
def post_delete(request, workspace_id, post_id):
    """Delete a post or a single platform post via HTMX.

    When an ``account`` query parameter is provided, only the PlatformPost for
    that social account is removed.  If it was the last PlatformPost the parent
    Post is deleted as well.  Without the parameter the entire Post (and all
    its PlatformPosts) is deleted.
    """
    workspace = _get_workspace(request, workspace_id)
    post = get_object_or_404(Post, id=post_id, workspace=workspace)

    account_id = request.GET.get("account") or request.POST.get("account")
    if account_id:
        pp = get_object_or_404(PlatformPost, post=post, social_account_id=account_id)
        pp.delete()
        # If no platform posts remain, clean up the parent post too.
        if not post.platform_posts.exists():
            post.delete()
    else:
        post.delete()

    return HttpResponse(
        status=204,
        headers={"HX-Trigger": "postChanged"},
    )


@login_required
@require_POST
def clone_post_view(request, workspace_id, post_id):
    """Clone a post into a fresh draft (Clone / Repost) and open the copy.

    The composer makes published/publishing posts read-only; this is the escape
    hatch to repost — the duplicate starts as a draft with the same content but
    no schedule, so it can be edited and re-queued without touching the original.
    """
    from django.urls import reverse

    from apps.composer.services import clone_post

    workspace = _get_workspace(request, workspace_id)
    post = get_object_or_404(Post, id=post_id, workspace=workspace)

    membership = request.workspace_membership
    perms = membership.effective_permissions if membership else {}
    if not perms.get("create_posts", False):
        raise PermissionDenied("You do not have permission to create posts.")

    new_post = clone_post(post, author=request.user)
    target = reverse("composer:compose_edit", kwargs={"workspace_id": workspace.id, "post_id": new_post.id})
    if request.htmx:
        return HttpResponse(status=204, headers={"HX-Redirect": target})
    return redirect(target)


# ---------------------------------------------------------------------------
# Create landing page & Idea CRUD
# ---------------------------------------------------------------------------


def _idea_columns(workspace, tag=None):
    """Build Kanban columns from IdeaGroup for a workspace, optionally filtered by tag."""
    groups = IdeaGroup.objects.for_workspace(workspace.id).order_by("position", "created_at")

    # Ensure default groups exist for this workspace
    if not groups.exists():
        created_groups = {}
        for name, pos in [("Unassigned", 0), ("To Do", 1), ("In Progress", 2), ("Done", 3)]:
            created_groups[name] = IdeaGroup.objects.create(workspace=workspace, name=name, position=pos)
        # Seed an introductory idea in the Unassigned column
        Idea.objects.create(
            workspace=workspace,
            group=created_groups["Unassigned"],
            title="This is a place to plan \u270d\ufe0f your content",
            description="Save your Ideas before converting them into posts. Brainstorm, plan ahead, and keep everything organized in one place.",
            status=Idea.Status.UNASSIGNED,
            position=0,
        )
        groups = IdeaGroup.objects.for_workspace(workspace.id).order_by("position", "created_at")

    ideas_qs = (
        Idea.objects.for_workspace(workspace.id)
        .select_related("author", "media_asset")
        .prefetch_related("media_attachments__media_asset")
        .order_by("position", "-created_at")
    )
    if tag:
        ideas_qs = ideas_qs.filter(tags__contains=[tag])

    grouped_ideas = {str(grp.id): [] for grp in groups}
    for idea in ideas_qs:
        _prepare_idea_for_kanban(idea)

        group_key = str(idea.group_id) if idea.group_id else ""
        if group_key in grouped_ideas:
            grouped_ideas[group_key].append(idea)

    columns = []
    for grp in groups:
        columns.append(
            {
                "id": str(grp.id),
                "key": str(grp.id),
                "label": grp.name,
                "ideas": grouped_ideas.get(str(grp.id), []),
            }
        )

    # All workspace tags from the Tag model
    all_tags = list(Tag.objects.for_workspace(workspace.id).values_list("name", flat=True))

    return columns, all_tags


def _parse_media_asset_ids(raw_ids):
    """Parse ordered media ids from CSV, preserving order and removing duplicates."""
    ordered = []
    seen = set()
    for token in (raw_ids or "").split(","):
        token = token.strip()
        if not token:
            continue
        try:
            normalized = str(uuid.UUID(token))
        except (ValueError, TypeError, AttributeError):
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


def _build_idea_media_payload(idea):
    """Build ordered media payload and attachment list for Kanban rendering."""
    attachments = [att for att in idea.media_attachments.all() if att.media_asset_id and att.media_asset]
    media_payload = []
    for att in attachments:
        asset = att.media_asset
        media_payload.append(
            {
                "asset_id": str(asset.id),
                "url": asset.file.url if asset.file else "",
                "filename": asset.filename,
                "media_type": asset.media_type,
                "position": att.position,
            }
        )

    # Legacy fallback for ideas that only have the old single media pointer.
    if not media_payload and idea.media_asset_id and idea.media_asset:
        media_payload.append(
            {
                "asset_id": str(idea.media_asset_id),
                "url": idea.media_asset.file.url if idea.media_asset.file else "",
                "filename": idea.media_asset.filename,
                "media_type": idea.media_asset.media_type,
                "position": 0,
            }
        )
    return media_payload, attachments


def _prepare_idea_for_kanban(idea):
    """Attach computed media/tag fields used by Kanban templates."""
    media_payload, attachments = _build_idea_media_payload(idea)
    idea.media_payload_json = json.dumps(media_payload)
    idea.tags_payload_json = json.dumps(idea.tags or [])
    idea.media_count = len(media_payload)
    idea.cover_media = attachments[0].media_asset if attachments else idea.media_asset
    return idea


def _render_idea_card_fragment(request, idea):
    """Render a single Kanban idea card fragment."""
    _prepare_idea_for_kanban(idea)
    return render_to_string(
        "composer/partials/idea_card.html",
        {
            "idea": idea,
            "group_id": str(idea.group_id) if idea.group_id else "",
        },
        request=request,
    )


def _render_kanban_column_fragment(request, group):
    """Render a single Kanban column fragment."""
    return render_to_string(
        "composer/partials/kanban_column.html",
        {
            "col": {
                "id": str(group.id),
                "key": str(group.id),
                "label": group.name,
                "ideas": [],
            },
        },
        request=request,
    )


def _wants_json_response(request):
    """Detect whether mutation endpoint should return JSON payload."""
    accept = request.headers.get("Accept", "")
    return "application/json" in accept.lower()


def _normalize_media_asset_ids(raw_ids="", extra_ids=None):
    """Normalize media IDs from mixed inputs while preserving first-seen order."""
    chunks = []
    if isinstance(raw_ids, str):
        chunks.append(raw_ids)
    elif raw_ids:
        chunks.extend(str(item) for item in raw_ids)
    if extra_ids:
        chunks.extend(str(item) for item in extra_ids)
    return _parse_media_asset_ids(",".join(chunks))


def _sync_idea_media_attachments(idea, workspace, ordered_asset_ids):
    """Synchronize IdeaMedia rows to match ordered ids and keep cover pointer aligned."""
    from apps.media_library.models import MediaAsset

    normalized_ids = _normalize_media_asset_ids(ordered_asset_ids)
    if normalized_ids:
        valid_assets = set(
            str(aid)
            for aid in MediaAsset.objects.filter(workspace=workspace, id__in=normalized_ids).values_list(
                "id", flat=True
            )
        )
        ordered_ids = [aid for aid in normalized_ids if aid in valid_assets]
    else:
        ordered_ids = []

    existing_attachments = list(idea.media_attachments.all())
    existing = {str(att.media_asset_id): att for att in existing_attachments}
    ids_to_keep = set(ordered_ids)
    ids_to_delete = [att.id for att in existing_attachments if str(att.media_asset_id) not in ids_to_keep]
    if ids_to_delete:
        IdeaMedia.objects.filter(id__in=ids_to_delete).delete()

    to_create = []
    to_update = []
    now = timezone.now()
    for position, asset_id in enumerate(ordered_ids):
        attachment = existing.get(asset_id)
        if attachment:
            if attachment.position != position:
                attachment.position = position
                attachment.updated_at = now
                to_update.append(attachment)
            continue
        to_create.append(
            IdeaMedia(
                idea=idea,
                media_asset_id=asset_id,
                position=position,
            )
        )

    if to_create:
        IdeaMedia.objects.bulk_create(to_create)
    if to_update:
        IdeaMedia.objects.bulk_update(to_update, ["position", "updated_at"])

    cover_id = ordered_ids[0] if ordered_ids else None
    current_cover_id = str(idea.media_asset_id) if idea.media_asset_id else None
    if current_cover_id != cover_id:
        idea.media_asset_id = cover_id
        idea.save(update_fields=["media_asset", "updated_at"])


def _create_idea_media_asset(workspace, user, uploaded_file):
    """Create a MediaAsset from an uploaded file for Idea create/edit flows."""
    from apps.media_library.models import MediaAsset

    content_type = uploaded_file.content_type or ""
    if content_type == "image/gif":
        media_type = MediaAsset.MediaType.GIF
    elif content_type.startswith("image/"):
        media_type = MediaAsset.MediaType.IMAGE
    elif content_type.startswith("video/"):
        media_type = MediaAsset.MediaType.VIDEO
    else:
        media_type = MediaAsset.MediaType.DOCUMENT

    return MediaAsset.objects.create(
        organization=workspace.organization,
        workspace=workspace,
        uploaded_by=user,
        file=uploaded_file,
        filename=uploaded_file.name,
        media_type=media_type,
        mime_type=content_type,
        file_size=uploaded_file.size,
        source="upload",
    )


@login_required
@require_permission("create_posts")
def create_landing(request, workspace_id):
    """Render the Create landing page with Ideas Kanban board."""
    from apps.composer.builtin_templates import (
        CATEGORIES,
        get_all_templates,
        get_featured_templates,
    )

    workspace = _get_workspace(request, workspace_id)
    tab = request.GET.get("tab", "ideas")
    tag = request.GET.get("tag")

    columns, all_tags = _idea_columns(workspace, tag)

    feeds = Feed.objects.for_workspace(workspace.id)

    context = {
        "workspace": workspace,
        "tab": tab,
        "columns": columns,
        "all_tags": all_tags,
        "active_tag": tag,
        "featured_templates": get_featured_templates(),
        "builtin_templates": get_all_templates(),
        "template_categories": CATEGORIES,
        "feeds": feeds,
    }
    return render(request, "composer/create_landing.html", context)


@login_required
@require_permission("create_posts")
@require_POST
def idea_upload_media(request, workspace_id):
    """Upload media for Idea modals and return an asset id for reliable save binding."""
    workspace = _get_workspace(request, workspace_id)
    uploaded_file = request.FILES.get("file") or request.FILES.get("media")
    if not uploaded_file:
        return JsonResponse({"error": "No file provided"}, status=400)

    asset = _create_idea_media_asset(workspace, request.user, uploaded_file)
    return JsonResponse(
        {
            "asset_id": str(asset.id),
            "filename": asset.filename,
            "url": asset.file.url if asset.file else "",
            "size": asset.file_size,
            "media_type": asset.media_type,
        }
    )


@login_required
@require_permission("create_posts")
@require_POST
def idea_create(request, workspace_id):
    """Create a new idea via HTMX."""
    workspace = _get_workspace(request, workspace_id)
    title = request.POST.get("title", "").strip()
    description = request.POST.get("description", "").strip()
    tags = parse_and_truncate_tag_string(request.POST.get("tags", ""))

    if not title:
        return HttpResponse("Title is required.", status=400)

    # Assign to the specified group or default to the first group
    group_id = request.POST.get("group")
    if group_id:
        group = IdeaGroup.objects.filter(id=group_id, workspace=workspace).first()
    else:
        group = IdeaGroup.objects.for_workspace(workspace.id).order_by("position").first()

    has_multi_media_payload = "media_asset_ids" in request.POST
    media_asset_ids = _normalize_media_asset_ids(request.POST.get("media_asset_ids", ""))

    # Handle media: pre-uploaded IDs (preferred), direct multipart upload fallback,
    # or legacy single pre-uploaded asset id.
    uploaded_file = request.FILES.get("media") or request.FILES.get("file")
    media_asset_id = request.POST.get("media_asset_id", "").strip()
    if uploaded_file:
        uploaded_asset = _create_idea_media_asset(workspace, request.user, uploaded_file)
        media_asset_ids.append(str(uploaded_asset.id))
    elif not media_asset_ids and media_asset_id:
        from apps.media_library.models import MediaAsset

        legacy_asset = MediaAsset.objects.filter(id=media_asset_id, workspace=workspace).first()
        if legacy_asset:
            media_asset_ids.append(str(legacy_asset.id))

    media_asset_ids = _normalize_media_asset_ids(media_asset_ids)

    with transaction.atomic():
        idea = Idea.objects.create(
            workspace=workspace,
            author=request.user,
            title=title,
            description=description,
            tags=tags,
            group=group,
            status=Idea.Status.UNASSIGNED,
            media_asset_id=media_asset_ids[0] if media_asset_ids else None,
        )

        if has_multi_media_payload or media_asset_ids:
            _sync_idea_media_attachments(idea, workspace, media_asset_ids)

    # Sync any new tags to the Tag model
    _sync_tags_to_model(workspace, tags)

    if _wants_json_response(request):
        idea = (
            Idea.objects.for_workspace(workspace.id)
            .select_related("media_asset")
            .prefetch_related("media_attachments__media_asset")
            .get(id=idea.id)
        )
        return JsonResponse(
            {
                "ok": True,
                "idea_id": str(idea.id),
                "group_id": str(idea.group_id) if idea.group_id else "",
                "tags": idea.tags or [],
                "card_html": _render_idea_card_fragment(request, idea),
            }
        )

    return HttpResponse(
        status=204,
        headers={"HX-Trigger": "ideaChanged"},
    )


@login_required
@require_permission("create_posts")
@require_POST
def idea_edit(request, workspace_id, idea_id):
    """Edit an existing idea via HTMX."""
    workspace = _get_workspace(request, workspace_id)
    idea = get_object_or_404(Idea, id=idea_id, workspace=workspace)
    previous_group_id = str(idea.group_id) if idea.group_id else ""

    idea.title = request.POST.get("title", idea.title).strip()
    idea.description = request.POST.get("description", idea.description).strip()
    idea.tags = parse_and_truncate_tag_string(request.POST.get("tags", ""))

    group_id = request.POST.get("group", "").strip()
    if group_id:
        group = IdeaGroup.objects.filter(id=group_id, workspace=workspace).first()
        if group:
            idea.group = group

    # Handle media attachments:
    # - preferred: ordered media_asset_ids list
    # - fallback: legacy single media_asset_id + remove_media
    has_multi_media_payload = "media_asset_ids" in request.POST
    media_asset_ids = _normalize_media_asset_ids(request.POST.get("media_asset_ids", ""))
    uploaded_file = request.FILES.get("media") or request.FILES.get("file")
    uploaded_asset = None
    if uploaded_file:
        uploaded_asset = _create_idea_media_asset(workspace, request.user, uploaded_file)
        media_asset_ids.append(str(uploaded_asset.id))
    media_asset_ids = _normalize_media_asset_ids(media_asset_ids)

    with transaction.atomic():
        idea.save(update_fields=["title", "description", "tags", "group", "updated_at"])

        if has_multi_media_payload:
            _sync_idea_media_attachments(idea, workspace, media_asset_ids)
        else:
            media_asset_id = request.POST.get("media_asset_id", "").strip()
            remove_media = request.POST.get("remove_media") == "true"

            if uploaded_asset:
                _sync_idea_media_attachments(idea, workspace, [str(uploaded_asset.id)])
            elif media_asset_id:
                from apps.media_library.models import MediaAsset

                asset = MediaAsset.objects.filter(id=media_asset_id, workspace=workspace).first()
                if asset:
                    _sync_idea_media_attachments(idea, workspace, [str(asset.id)])
            elif remove_media:
                _sync_idea_media_attachments(idea, workspace, [])

    # Sync any new tags to the Tag model
    _sync_tags_to_model(workspace, idea.tags)

    if _wants_json_response(request):
        idea = (
            Idea.objects.for_workspace(workspace.id)
            .select_related("media_asset")
            .prefetch_related("media_attachments__media_asset")
            .get(id=idea.id)
        )
        return JsonResponse(
            {
                "ok": True,
                "idea_id": str(idea.id),
                "previous_group_id": previous_group_id,
                "group_id": str(idea.group_id) if idea.group_id else "",
                "tags": idea.tags or [],
                "card_html": _render_idea_card_fragment(request, idea),
            }
        )

    return HttpResponse(
        status=204,
        headers={"HX-Trigger": "ideaChanged"},
    )


@login_required
@require_permission("create_posts")
@require_POST
def idea_create_post(request, workspace_id, idea_id):
    """Create a new draft post from an idea and return composer redirect metadata."""
    workspace = _get_workspace(request, workspace_id)
    idea = get_object_or_404(
        Idea.objects.for_workspace(workspace.id)
        .select_related("media_asset")
        .prefetch_related("media_attachments__media_asset"),
        id=idea_id,
    )

    tags = []
    if isinstance(idea.tags, list):
        tags = [tag.strip() for tag in idea.tags if isinstance(tag, str) and tag.strip()]

    ordered_media_asset_ids = []
    seen_media_ids = set()
    for attachment in idea.media_attachments.all():
        if not attachment.media_asset_id:
            continue
        media_id = str(attachment.media_asset_id)
        if media_id in seen_media_ids:
            continue
        seen_media_ids.add(media_id)
        ordered_media_asset_ids.append(media_id)

    # Legacy fallback: old ideas may only have the single media pointer set.
    if not ordered_media_asset_ids and idea.media_asset_id:
        ordered_media_asset_ids.append(str(idea.media_asset_id))

    connected_accounts = list(
        SocialAccount.objects.for_workspace(workspace.id)
        .filter(connection_status=SocialAccount.ConnectionStatus.CONNECTED)
        .order_by("platform", "account_name", "id")
    )

    with transaction.atomic():
        post = Post.objects.create(
            workspace=workspace,
            author=request.user,
            title=idea.title or "",
            caption=idea.description or "",
            tags=tags,
        )

        if ordered_media_asset_ids:
            PostMedia.objects.bulk_create(
                [
                    PostMedia(
                        post=post,
                        media_asset_id=asset_id,
                        position=index,
                    )
                    for index, asset_id in enumerate(ordered_media_asset_ids)
                ]
            )

        if connected_accounts:
            PlatformPost.objects.bulk_create(
                [
                    PlatformPost(
                        post=post,
                        social_account=account,
                    )
                    for account in connected_accounts
                ]
            )

        idea.post = post
        idea.save(update_fields=["post", "updated_at"])

    from django.urls import reverse

    compose_url = reverse("composer:compose_edit", kwargs={"workspace_id": workspace.id, "post_id": post.id})
    return JsonResponse(
        {
            "ok": True,
            "post_id": str(post.id),
            "compose_url": compose_url,
        }
    )


@login_required
@require_permission("create_posts")
@require_POST
def idea_delete(request, workspace_id, idea_id):
    """Delete an idea via HTMX."""
    workspace = _get_workspace(request, workspace_id)
    idea = get_object_or_404(Idea, id=idea_id, workspace=workspace)
    group_id = str(idea.group_id) if idea.group_id else ""
    idea.delete()

    if _wants_json_response(request):
        return JsonResponse(
            {
                "ok": True,
                "idea_id": str(idea_id),
                "group_id": group_id,
            }
        )

    return HttpResponse(
        status=204,
        headers={"HX-Trigger": "ideaChanged"},
    )


@login_required
@require_permission("create_posts")
@require_POST
def idea_move(request, workspace_id, idea_id):
    """Move an idea to a new column/position via HTMX (drag-and-drop)."""
    workspace = _get_workspace(request, workspace_id)
    idea = get_object_or_404(Idea, id=idea_id, workspace=workspace)
    new_group_id = request.POST.get("group")
    new_position = request.POST.get("position")

    # Support both group-based and legacy status-based moves
    if new_group_id:
        group = IdeaGroup.objects.filter(id=new_group_id, workspace=workspace).first()
        if group:
            idea.group = group
    else:
        new_status = request.POST.get("status")
        if new_status and new_status in dict(Idea.Status.choices):
            idea.status = new_status
    if new_position is not None:
        with contextlib.suppress(ValueError, TypeError):
            idea.position = int(new_position)
    idea.save()

    # No HX-Trigger here - the frontend handles the move optimistically
    return HttpResponse(status=204)


@login_required
@require_permission("create_posts")
@require_GET
def idea_board(request, workspace_id):
    """Return the Kanban board partial for HTMX refresh."""
    workspace = _get_workspace(request, workspace_id)
    tag = request.GET.get("tag")
    columns, all_tags = _idea_columns(workspace, tag)

    return render(
        request,
        "composer/partials/kanban_board.html",
        {
            "workspace": workspace,
            "columns": columns,
            "all_tags": all_tags,
            "active_tag": tag,
        },
    )


# ---------------------------------------------------------------------------
# Idea Group CRUD (Kanban columns)
# ---------------------------------------------------------------------------


@login_required
@require_permission("create_posts")
@require_POST
def idea_group_create(request, workspace_id):
    """Create a new Kanban column via HTMX."""
    workspace = _get_workspace(request, workspace_id)
    name = request.POST.get("name", "").strip()
    if not name:
        if _wants_json_response(request):
            return JsonResponse({"error": "Name is required."}, status=400)
        return HttpResponse("Name is required.", status=400)

    max_pos = IdeaGroup.objects.for_workspace(workspace.id).aggregate(models.Max("position"))["position__max"] or 0
    group = IdeaGroup.objects.create(workspace=workspace, name=name, position=max_pos + 1)

    if _wants_json_response(request):
        return JsonResponse(
            {
                "ok": True,
                "group_id": str(group.id),
                "group_name": group.name,
                "column_html": _render_kanban_column_fragment(request, group),
            }
        )

    return HttpResponse(status=204, headers={"HX-Trigger": "ideaChanged"})


@login_required
@require_permission("create_posts")
@require_POST
def idea_group_delete(request, workspace_id, group_id):
    """Delete an empty Kanban column via HTMX."""
    workspace = _get_workspace(request, workspace_id)
    group = get_object_or_404(IdeaGroup, id=group_id, workspace=workspace)

    if group.ideas.exists():
        if _wants_json_response(request):
            return JsonResponse({"error": "Column must be empty before deleting."}, status=400)
        return HttpResponse("Column must be empty before deleting.", status=400)

    deleted_group_id = str(group.id)
    group.delete()
    if _wants_json_response(request):
        return JsonResponse({"ok": True, "group_id": deleted_group_id})
    return HttpResponse(status=204, headers={"HX-Trigger": "ideaChanged"})


@login_required
@require_permission("create_posts")
@require_POST
def idea_group_reorder(request, workspace_id):
    """Reorder Kanban columns. Expects JSON body: {"order": ["uuid1", "uuid2", ...]}."""
    workspace = _get_workspace(request, workspace_id)
    try:
        data = json.loads(request.body)
        order = data.get("order", [])
    except (json.JSONDecodeError, AttributeError):
        return HttpResponse("Invalid JSON.", status=400)

    if not order:
        return HttpResponse(status=204)

    groups = {str(g.id): g for g in IdeaGroup.objects.for_workspace(workspace.id)}
    for position, group_id in enumerate(order):
        group = groups.get(group_id)
        if group and group.position != position:
            group.position = position
            group.save(update_fields=["position"])

    return HttpResponse(status=204)


# ---------------------------------------------------------------------------
# Content Categories CRUD
# ---------------------------------------------------------------------------


@login_required
def category_list(request, workspace_id):
    """Settings page for managing content categories."""
    workspace = _get_workspace(request, workspace_id)
    categories = ContentCategory.objects.for_workspace(workspace.id)
    form = ContentCategoryForm()

    return render(
        request,
        "composer/categories.html",
        {
            "workspace": workspace,
            "categories": categories,
            "form": form,
        },
    )


@login_required
@require_POST
def category_create(request, workspace_id):
    """Create a new content category via HTMX."""
    workspace = _get_workspace(request, workspace_id)
    form = ContentCategoryForm(request.POST)

    if not form.is_valid():
        return HttpResponse("Invalid data.", status=400)

    category = form.save(commit=False)
    category.workspace = workspace
    max_pos = ContentCategory.objects.for_workspace(workspace.id).aggregate(models.Max("position"))["position__max"]
    category.position = (max_pos or 0) + 1
    category.save()

    return HttpResponse(
        status=204,
        headers={"HX-Trigger": "categoryChanged"},
    )


@login_required
@require_POST
def category_edit(request, workspace_id, category_id):
    """Edit a content category via HTMX."""
    workspace = _get_workspace(request, workspace_id)
    category = get_object_or_404(ContentCategory, id=category_id, workspace=workspace)
    form = ContentCategoryForm(request.POST, instance=category)

    if not form.is_valid():
        return HttpResponse("Invalid data.", status=400)

    form.save()
    return HttpResponse(
        status=204,
        headers={"HX-Trigger": "categoryChanged"},
    )


@login_required
@require_POST
def category_delete(request, workspace_id, category_id):
    """Delete a content category via HTMX."""
    workspace = _get_workspace(request, workspace_id)
    category = get_object_or_404(ContentCategory, id=category_id, workspace=workspace)
    category.delete()

    return HttpResponse(
        status=204,
        headers={"HX-Trigger": "categoryChanged"},
    )


# ---------------------------------------------------------------------------
# Post Templates
# ---------------------------------------------------------------------------


@login_required
def template_list(request, workspace_id):
    """Settings page for managing post templates."""
    workspace = _get_workspace(request, workspace_id)
    templates = PostTemplate.objects.for_workspace(workspace.id).select_related("created_by")

    return render(
        request,
        "composer/templates_list.html",
        {
            "workspace": workspace,
            "templates": templates,
        },
    )


@login_required
@require_POST
def save_as_template(request, workspace_id, post_id):
    """Save the current post as a reusable template."""
    workspace = _get_workspace(request, workspace_id)
    post = get_object_or_404(Post, id=post_id, workspace=workspace)

    name = request.POST.get("template_name", "").strip()
    if not name:
        name = f"Template from {post.caption_snippet or 'post'}"

    description = request.POST.get("template_description", "").strip()

    template_data = {
        "caption": post.caption,
        "first_comment": post.first_comment,
        "category_id": str(post.category_id) if post.category_id else None,
        "tags": post.tags,
        "platform_account_ids": [str(pp.social_account_id) for pp in post.platform_posts.all()],
        "media_asset_ids": [str(pm.media_asset_id) for pm in post.media_attachments.all()],
    }

    PostTemplate.objects.create(
        workspace=workspace,
        name=name,
        description=description,
        template_data=template_data,
        created_by=request.user,
    )

    if request.htmx:
        return HttpResponse(
            status=204,
            headers={"HX-Trigger": "templateSaved"},
        )
    return redirect("composer:compose_edit", workspace_id=workspace.id, post_id=post.id)


@login_required
@require_POST
def template_delete(request, workspace_id, template_id):
    """Delete a post template."""
    workspace = _get_workspace(request, workspace_id)
    tpl = get_object_or_404(PostTemplate, id=template_id, workspace=workspace)
    tpl.delete()

    return HttpResponse(
        status=204,
        headers={"HX-Trigger": "templateChanged"},
    )


@login_required
@require_GET
def template_picker(request, workspace_id):
    """HTMX partial returning list of templates for the picker modal."""
    workspace = _get_workspace(request, workspace_id)
    templates = PostTemplate.objects.for_workspace(workspace.id).select_related("created_by")

    return render(
        request,
        "composer/partials/template_picker.html",
        {
            "workspace": workspace,
            "templates": templates,
        },
    )


@login_required
@require_GET
def use_template(request, workspace_id, template_id):
    """Redirect to composer with template data pre-filled."""
    workspace = _get_workspace(request, workspace_id)
    get_object_or_404(PostTemplate, id=template_id, workspace=workspace)

    from django.urls import reverse

    compose_url = reverse("composer:compose", kwargs={"workspace_id": workspace.id})
    return redirect(f"{compose_url}?template={template_id}")


# ---------------------------------------------------------------------------
# CSV Import
# ---------------------------------------------------------------------------


_CSV_MEDIA_MESSAGE = (
    "CSV media URLs are not supported yet. Upload files to Media Library, remove the media columns, "
    "and import without a date so you can attach the files before scheduling. No posts were imported."
)


def _reject_csv_media(request, workspace, csv_data, mapping=None):
    """Fail visibly rather than silently stripping attachments from a CSV import.

    URL ingestion needs a bounded downloader pinned to a validated public IP;
    the existing feed fetcher does not provide that protection. Never fetch here.
    This also rejects legacy session mappings and manually submitted mappings.
    """
    media_columns = [
        index
        for index, header in enumerate(csv_data.get("headers", []))
        if re.fullmatch(
            r"(?:media|image|video|photo|attachment)(?:_?urls?)?(?:_?\d+)?s?",
            re.sub(r"[\s-]+", "_", str(header).strip().lower()),
        )
    ]
    errors = [
        {"row": number, "errors": [_CSV_MEDIA_MESSAGE]}
        for number, row in enumerate(csv_data.get("rows", []), start=2)
        if any(index < len(row) and str(row[index]).strip() for index in media_columns)
    ]
    if not errors and any(
        field in (mapping or {}) and mapping[field] not in (None, "")
        for field in ("media_url", "media_urls", "image_url", "video_url")
    ):
        errors = [{"row": 1, "errors": [_CSV_MEDIA_MESSAGE]}]
    if not errors:
        return None
    request.session.pop(f"csv_import_{workspace.id}", None)
    request.session.pop(f"csv_mapping_{workspace.id}", None)
    return render(
        request,
        "composer/partials/csv_validation.html",
        {
            "workspace": workspace,
            "total_rows": len(csv_data.get("rows", [])),
            "valid_count": 0,
            "errors": errors[:50],
            "has_more_errors": len(errors) > 50,
        },
    )


@login_required
@require_permission("create_posts")
def csv_upload(request, workspace_id):
    """Render CSV upload page or handle file upload and show column mapping."""
    workspace = _get_workspace(request, workspace_id)

    if request.method == "POST" and request.FILES.get("csv_file"):
        import csv
        import io

        request.session.pop(f"csv_import_{workspace.id}", None)
        request.session.pop(f"csv_mapping_{workspace.id}", None)
        csv_file = request.FILES["csv_file"]
        if csv_file.size and csv_file.size > MAX_CSV_UPLOAD_BYTES:
            return render(
                request,
                "composer/csv_import.html",
                {"workspace": workspace, "error": "CSV file too large (max 5 MB)."},
            )
        decoded = csv_file.read().decode("utf-8-sig")
        reader = csv.reader(io.StringIO(decoded))
        rows = list(reader)

        if not rows:
            return render(
                request,
                "composer/csv_import.html",
                {"workspace": workspace, "error": "CSV file is empty."},
            )

        headers = rows[0]
        media_error = _reject_csv_media(request, workspace, {"headers": headers, "rows": rows[1:]})
        if media_error is not None:
            return media_error
        preview_rows = rows[1:6]  # First 5 data rows

        # Auto-detect column mapping
        field_map = {
            "date": ["date", "publish_date", "scheduled_date"],
            "time": ["time", "publish_time", "scheduled_time"],
            "platforms": ["platform", "platforms", "channel", "channels"],
            "caption": ["caption", "text", "content", "message", "body"],
            "category": ["category", "content_category", "type"],
            "tags": ["tags", "labels", "tag"],
            "first_comment": ["first_comment", "comment"],
        }

        auto_mapping = {}
        for col_idx, header in enumerate(headers):
            header_lower = header.strip().lower().replace(" ", "_")
            for field, aliases in field_map.items():
                if header_lower in aliases:
                    auto_mapping[field] = col_idx
                    break

        # Store CSV in session for the next step
        request.session[f"csv_import_{workspace.id}"] = {
            "headers": headers,
            "rows": rows[1:],  # Exclude header
            "filename": csv_file.name,
        }

        return render(
            request,
            "composer/partials/csv_mapping.html",
            {
                "workspace": workspace,
                "headers": headers,
                "preview_rows": preview_rows,
                "auto_mapping": auto_mapping,
                "field_choices": list(field_map.keys()),
            },
        )

    return render(
        request,
        "composer/csv_import.html",
        {"workspace": workspace},
    )


@login_required
@require_permission("create_posts")
@require_POST
def csv_preview(request, workspace_id):
    """Validate CSV rows with the selected column mapping and show preview."""
    workspace = _get_workspace(request, workspace_id)
    csv_data = request.session.get(f"csv_import_{workspace.id}")

    if not csv_data:
        return HttpResponse("No CSV data found. Please upload again.", status=400)

    media_error = _reject_csv_media(
        request,
        workspace,
        csv_data,
        {
            "media_url": request.POST.get("map_media_url"),
            "media_urls": request.POST.get("map_media_urls"),
            "image_url": request.POST.get("map_image_url"),
            "video_url": request.POST.get("map_video_url"),
        },
    )
    if media_error is not None:
        return media_error

    # Parse supported column mapping from POST.
    mapping = {}
    for field in ["date", "time", "platforms", "caption", "category", "tags", "first_comment"]:
        col_idx = request.POST.get(f"map_{field}", "")
        if col_idx != "":
            import contextlib

            with contextlib.suppress(ValueError, TypeError):
                mapping[field] = int(col_idx)

    rows = csv_data["rows"]
    errors = []
    valid_count = 0

    from apps.social_accounts.models import SocialAccount

    valid_platforms = {p[0].lower() for p in SocialAccount._meta.get_field("platform").choices}
    connected_accounts = set(
        SocialAccount.objects.for_workspace(workspace.id)
        .filter(connection_status=SocialAccount.ConnectionStatus.CONNECTED)
        .values_list("platform", flat=True)
    )

    for row_idx, row in enumerate(rows, start=2):  # Row 2 = first data row
        row_errors = []

        # Validate date
        if "date" in mapping:
            date_val = row[mapping["date"]].strip() if mapping["date"] < len(row) else ""
            if date_val:
                try:
                    from datetime import date as date_cls

                    date_cls.fromisoformat(date_val)
                except ValueError:
                    row_errors.append(f"Invalid date format '{date_val}' (expected YYYY-MM-DD)")
            else:
                row_errors.append("Date is empty")

        # Validate platforms
        if "platforms" in mapping:
            platforms_val = row[mapping["platforms"]].strip() if mapping["platforms"] < len(row) else ""
            if platforms_val:
                for p in platforms_val.split(","):
                    p = p.strip().lower()
                    if p and p not in valid_platforms:
                        row_errors.append(f"Unknown platform '{p}'")
                    elif p and p not in connected_accounts:
                        row_errors.append(f"Platform '{p}' is not connected")

        # Validate caption
        if "caption" in mapping:
            caption_val = row[mapping["caption"]].strip() if mapping["caption"] < len(row) else ""
            if not caption_val:
                row_errors.append("Caption is empty")

        if row_errors:
            errors.append({"row": row_idx, "errors": row_errors})
        else:
            valid_count += 1

    # Store mapping in session
    request.session[f"csv_mapping_{workspace.id}"] = mapping

    return render(
        request,
        "composer/partials/csv_validation.html",
        {
            "workspace": workspace,
            "total_rows": len(rows),
            "valid_count": valid_count,
            "errors": errors[:50],  # Show max 50 errors
            "has_more_errors": len(errors) > 50,
        },
    )


def _csv_scheduled_by_month(workspace, rows, mapping):
    """Count the posts a CSV import would schedule, per month: ``{month's first scheduled_at: count}``.

    Mirrors the import loop: a dated row schedules one post per connected
    account on its platforms that can publish without media; undated rows and
    accounts that can't publish become drafts, which the allowance ignores.
    """
    import zoneinfo
    from datetime import time as time_cls

    from apps.social_accounts.models import SocialAccount

    if "date" not in mapping or "platforms" not in mapping:
        return {}
    tz = zoneinfo.ZoneInfo(workspace.effective_timezone or "UTC")
    accounts_by_platform: dict[str, int] = {}
    counts: dict[tuple[int, int], list] = {}
    for row in rows:
        if mapping["date"] >= len(row) or mapping["platforms"] >= len(row) or not row[mapping["date"]].strip():
            continue
        try:
            day = datetime.strptime(row[mapping["date"]].strip(), "%Y-%m-%d").date()
            time_str = row[mapping["time"]].strip() if "time" in mapping and mapping["time"] < len(row) else ""
            at = datetime.combine(day, datetime.strptime(time_str, "%H:%M").time() if time_str else time_cls(9, 0))
        except ValueError:
            continue  # The import loop reports a row with a bad date.
        at = at.replace(tzinfo=tz)
        platforms = {p.strip().lower() for p in row[mapping["platforms"]].split(",") if p.strip()}
        for platform in platforms:
            if platform not in accounts_by_platform:
                accounts_by_platform[platform] = sum(
                    1
                    for acc in SocialAccount.objects.filter(
                        workspace=workspace,
                        platform=platform,
                        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
                    )
                    if not publish_blocker(acc, media_types=set())
                )
            bucket = counts.setdefault((at.astimezone(UTC).year, at.astimezone(UTC).month), [at, 0])
            bucket[1] += accounts_by_platform[platform]
    return {at: count for at, count in counts.values() if count}


def _reject_csv_over_plan_limit(request, workspace, csv_data, mapping):
    """Refuse the whole import when its scheduled posts would pass the monthly allowance.

    Checked before anything is written, so the import never stops partway
    with the first rows in and the rest failing at the plan limit. The
    alternative (importing the overflow as drafts) would quietly change what
    the person asked to schedule; they can instead trim the file or upgrade.
    """
    if not billing_enabled():
        return None
    try:
        for at, count in _csv_scheduled_by_month(workspace, csv_data["rows"], mapping).items():
            check_can_schedule_posts(workspace.organization, count=count, when=at)
    except PlanLimitError as exc:
        request.session.pop(f"csv_import_{workspace.id}", None)
        request.session.pop(f"csv_mapping_{workspace.id}", None)
        return render(
            request,
            "composer/partials/csv_validation.html",
            {
                "workspace": workspace,
                "total_rows": len(csv_data["rows"]),
                "valid_count": 0,
                "errors": [{"row": 1, "errors": [f"{exc.message} No posts were imported."]}],
                "has_more_errors": False,
            },
        )
    return None


@login_required
@require_permission("create_posts")
@require_POST
def csv_confirm_import(request, workspace_id):
    """Import supported CSV rows, committing each complete row atomically."""
    workspace = _get_workspace(request, workspace_id)
    csv_data = request.session.get(f"csv_import_{workspace.id}")
    mapping = request.session.get(f"csv_mapping_{workspace.id}")

    if not csv_data or not mapping:
        return HttpResponse("No CSV data found. Please upload again.", status=400)

    media_error = _reject_csv_media(request, workspace, csv_data, mapping)
    if media_error is not None:
        return media_error

    limit_error = _reject_csv_over_plan_limit(request, workspace, csv_data, mapping)
    if limit_error is not None:
        return limit_error

    from apps.social_accounts.models import SocialAccount

    rows = csv_data["rows"]
    created_count = 0
    error_count = 0
    # Dated rows for platforms a CSV row can never satisfy: it carries no
    # media and no Pinterest board. Those are kept as drafts at their date
    # rather than scheduled to fail, and the result screen says so.
    drafted_count = 0
    drafted_platforms: set[str] = set()

    for row in rows:
        try:
            with transaction.atomic():
                caption = (
                    row[mapping["caption"]].strip() if "caption" in mapping and mapping["caption"] < len(row) else ""
                )
                if not caption:
                    error_count += 1
                    continue

                post = Post(
                    workspace=workspace,
                    author=request.user,
                    caption=caption,
                )
                initial_pp_status = "draft"

                # Date + time
                if "date" in mapping and mapping["date"] < len(row):
                    date_str = row[mapping["date"]].strip()
                    time_str = ""
                    if "time" in mapping and mapping["time"] < len(row):
                        time_str = row[mapping["time"]].strip()

                    if date_str:
                        import zoneinfo

                        ws_tz = workspace.effective_timezone or "UTC"
                        tz = zoneinfo.ZoneInfo(ws_tz)
                        from datetime import time as time_cls

                        d = datetime.strptime(date_str, "%Y-%m-%d").date()
                        t = datetime.strptime(time_str, "%H:%M").time() if time_str else time_cls(9, 0)
                        naive_dt = datetime.combine(d, t)
                        post.scheduled_at = naive_dt.replace(tzinfo=tz)
                        initial_pp_status = "scheduled"

                # First comment
                if "first_comment" in mapping and mapping["first_comment"] < len(row):
                    post.first_comment = row[mapping["first_comment"]].strip()

                # Tags
                if "tags" in mapping and mapping["tags"] < len(row):
                    tags_raw = row[mapping["tags"]].strip()
                    if tags_raw:
                        post.tags = [t.strip() for t in tags_raw.split(",") if t.strip()]

                # Category
                if "category" in mapping and mapping["category"] < len(row):
                    cat_name = row[mapping["category"]].strip()
                    if cat_name:
                        cat, _ = ContentCategory.objects.get_or_create(
                            workspace=workspace,
                            name=cat_name,
                            defaults={"color": "#3B82F6"},
                        )
                        post.category = cat

                post.save()

                # Platforms
                if "platforms" in mapping and mapping["platforms"] < len(row):
                    platforms_str = row[mapping["platforms"]].strip()
                    if platforms_str:
                        for p in platforms_str.split(","):
                            p = p.strip().lower()
                            accounts = SocialAccount.objects.filter(
                                workspace=workspace,
                                platform=p,
                                connection_status=SocialAccount.ConnectionStatus.CONNECTED,
                            )
                            for acc in accounts:
                                status = initial_pp_status
                                if status == "scheduled" and publish_blocker(acc, media_types=set()):
                                    status = "draft"
                                _pp, created = PlatformPost.objects.get_or_create(
                                    post=post,
                                    social_account=acc,
                                    defaults={
                                        "status": status,
                                        "scheduled_at": post.scheduled_at,
                                    },
                                )
                                if created and status != initial_pp_status:
                                    drafted_count += 1
                                    drafted_platforms.add(acc.get_platform_display())

            created_count += 1
        except Exception:
            error_count += 1

    # Clean up session data
    request.session.pop(f"csv_import_{workspace.id}", None)
    request.session.pop(f"csv_mapping_{workspace.id}", None)

    return render(
        request,
        "composer/partials/csv_progress.html",
        {
            "workspace": workspace,
            "created_count": created_count,
            "error_count": error_count,
            "total_rows": len(rows),
            "drafted_count": drafted_count,
            "drafted_platforms": get_text_list(sorted(drafted_platforms), "and"),
        },
    )


# ---------------------------------------------------------------------------
# Tag CRUD (JSON API endpoints)
# ---------------------------------------------------------------------------


def _sync_tags_to_model(workspace, tag_names):
    """Ensure Tag records exist for all given tag names in a workspace."""
    if not tag_names:
        return
    existing = set(Tag.objects.for_workspace(workspace.id).filter(name__in=tag_names).values_list("name", flat=True))
    new_tags = [Tag(workspace=workspace, name=name) for name in tag_names if name not in existing]
    if new_tags:
        Tag.objects.bulk_create(new_tags, ignore_conflicts=True)


@login_required
@require_GET
def tag_list(request, workspace_id):
    """Return workspace tags as JSON, optionally filtered by search query."""
    workspace = _get_workspace(request, workspace_id)
    q = request.GET.get("q", "").strip().lower()
    tags = Tag.objects.for_workspace(workspace.id)
    if q:
        tags = tags.filter(name__icontains=q)
    tag_data = [{"id": str(t.id), "name": t.name} for t in tags[:50]]
    return JsonResponse(tag_data, safe=False)


@login_required
@require_POST
def tag_create(request, workspace_id):
    """Create a new tag and return it as JSON."""
    workspace = _get_workspace(request, workspace_id)
    name = request.POST.get("name", "").strip()
    if not name:
        return JsonResponse({"error": "Tag name is required."}, status=400)
    tag, created = Tag.objects.get_or_create(workspace=workspace, name=name)
    return JsonResponse({"id": str(tag.id), "name": tag.name, "created": created})


# ---------------------------------------------------------------------------
# Composer inserts: hashtag sets, signatures, @mention suggestions
# ---------------------------------------------------------------------------

# Per user, across every account searched. Typing pauses of 250 ms and the
# client's own cache keep a normal writer well under it.
MENTION_SEARCH_RATE = "60/m"


@login_required
@require_GET
@require_permission("create_posts")
def hashtag_picker(request, workspace_id):
    """JSON for the composer's hashtag menu: saved sets and recently used tags."""
    workspace = _get_workspace(request, workspace_id)
    sets = HashtagSet.objects.for_workspace(workspace.id)
    sets_json = [{"id": str(s.id), "name": s.name, "text": s.text, "count": len(s.hashtags)} for s in sets]
    brand_tags = brand_kit.resolve(workspace).default_hashtags
    if brand_tags:
        sets_json.insert(
            0, {"id": "brand-kit", "name": "Brand kit hashtags", "text": " ".join(brand_tags), "count": len(brand_tags)}
        )
    return JsonResponse(
        {
            "sets": sets_json,
            "recent": recent_hashtags(workspace.id),
        }
    )


@login_required
@require_GET
@require_permission("create_posts")
def signature_picker(request, workspace_id):
    """JSON for the composer's signature menu: the workspace's signatures and the user's own."""
    workspace = _get_workspace(request, workspace_id)
    signatures = Signature.objects.available(workspace.id, request.user)
    return JsonResponse(
        {
            "signatures": [
                {
                    "id": str(s.id),
                    "name": s.name,
                    "body": s.body,
                    "scope": "personal" if s.user_id else "workspace",
                    "is_default": s.is_default,
                }
                for s in signatures
            ]
            # The brand kit's legal disclaimers and boilerplate, inserted like any signature.
            + [
                {
                    "id": f"brand-kit-{index}",
                    "name": item["name"],
                    "body": item["text"],
                    "scope": "brand",
                    "is_default": False,
                }
                for index, item in enumerate(brand_kit.resolve(workspace).disclaimers)
            ]
        }
    )


@login_required
@require_GET
@require_permission("create_posts")
@ratelimit(key="user", rate=MENTION_SEARCH_RATE, block=False)
def mention_search(request, workspace_id):
    """Accounts to @mention matching ``?q=``, searched on the ``?account=`` ids.

    A nicety, so it never errors: a bad id, a platform without lookup, a
    platform that is down or a user typing faster than ``MENTION_SEARCH_RATE``
    just yields no suggestions. The rate keeps one composer from spending the
    connected accounts' own platform rate limits.
    """
    workspace = _get_workspace(request, workspace_id)
    query = request.GET.get("q", "").strip().lstrip("@")
    account_ids = [a for a in request.GET.getlist("account") if _is_valid_uuid(a)]
    if getattr(request, "limited", False) or not query or len(query) > MENTION_QUERY_MAX or not account_ids:
        return JsonResponse({"results": []})
    accounts = SocialAccount.objects.for_workspace(workspace.id).filter(
        id__in=account_ids,
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )
    return JsonResponse({"results": mention_suggestions(accounts, query)})


# ── Feeds ──────────────────────────────────────────────────────────────────


FEED_EVENTS_PAGE_SIZE = 15
FEED_EVENTS_CACHE_TTL_SECONDS = 10 * 60
_IMG_SRC_RE = re.compile(r"""<img[^>]+src=["']([^"']+)["']""", re.IGNORECASE)


def _feed_events_cache_key(workspace_id):
    return f"composer:feed-events:{workspace_id}"


def _normalize_selected_feed_id(selected_feed_id, feeds):
    valid_feed_ids = {str(feed.id) for feed in feeds}
    if selected_feed_id in valid_feed_ids:
        return selected_feed_id
    return "all"


def _coerce_positive_int(value, default=0):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _xml_local_name(tag):
    """Return local XML tag name without namespace."""
    if not tag:
        return ""
    if "}" in tag:
        return tag.rsplit("}", 1)[-1].lower()
    if ":" in tag:
        return tag.split(":", 1)[-1].lower()
    return tag.lower()


def _first_child(element, *names):
    wanted = {name.lower() for name in names}
    for child in element:
        if _xml_local_name(child.tag) in wanted:
            return child
    return None


def _first_child_text(element, *names):
    child = _first_child(element, *names)
    if child is None:
        return ""
    return (child.text or "").strip()


def _extract_atom_link(entry):
    for child in entry:
        if _xml_local_name(child.tag) != "link":
            continue
        href = (child.attrib.get("href") or "").strip()
        rel = (child.attrib.get("rel") or "").strip().lower()
        if href and rel in ("", "alternate"):
            return href
    for child in entry:
        if _xml_local_name(child.tag) != "link":
            continue
        href = (child.attrib.get("href") or "").strip()
        if href:
            return href
    return ""


def _extract_image_url(entry, summary_raw):
    for node in entry.iter():
        name = _xml_local_name(node.tag)
        if name not in {"thumbnail", "content", "enclosure"}:
            continue
        url = (node.attrib.get("url") or node.attrib.get("href") or node.attrib.get("src") or "").strip()
        media_type = (node.attrib.get("type") or "").lower()
        medium = (node.attrib.get("medium") or "").lower()
        if url and (name == "thumbnail" or medium == "image" or media_type.startswith("image/") or not media_type):
            return url

    if summary_raw:
        match = _IMG_SRC_RE.search(summary_raw)
        if match:
            return match.group(1)
    return ""


def _clean_summary(raw_summary):
    if not raw_summary:
        return ""
    return re.sub(r"\s+", " ", strip_tags(raw_summary)).strip()


def _parse_published_at(raw_value):
    if not raw_value:
        return None
    with contextlib.suppress(ValueError, TypeError, OverflowError):
        parsed = date_parser.parse(raw_value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    return None


def _parse_feed_document(xml_content):
    """Parse RSS/Atom document into metadata and raw entries.

    Uses safe_xml_fromstring to bound size and reject DTD/entity-bearing
    payloads (billion-laughs defence).
    """
    if isinstance(xml_content, str):
        xml_content = xml_content.encode("utf-8", errors="replace")
    root = safe_xml_fromstring(xml_content)
    if root is None:
        return None

    root_name = _xml_local_name(root.tag)
    if root_name == "rss":
        channel = _first_child(root, "channel")
        if channel is None:
            return None
        entries = [child for child in channel if _xml_local_name(child.tag) == "item"]
        return {
            "title": _first_child_text(channel, "title"),
            "website_url": _first_child_text(channel, "link"),
            "entries": entries,
            "entry_kind": "rss",
        }

    if root_name == "feed":
        entries = [child for child in root if _xml_local_name(child.tag) == "entry"]
        return {
            "title": _first_child_text(root, "title"),
            "website_url": _extract_atom_link(root),
            "entries": entries,
            "entry_kind": "atom",
        }

    if root_name == "rdf":
        channel = _first_child(root, "channel")
        entries = [child for child in root if _xml_local_name(child.tag) == "item"]
        return {
            "title": _first_child_text(channel, "title") if channel is not None else "",
            "website_url": _first_child_text(channel, "link") if channel is not None else "",
            "entries": entries,
            "entry_kind": "rss",
        }

    return None


def _build_event_from_entry(feed, parsed_feed, entry):
    kind = parsed_feed["entry_kind"]
    if kind == "atom":
        raw_title = _first_child_text(entry, "title")
        raw_link = _extract_atom_link(entry)
        raw_summary = _first_child_text(entry, "summary") or _first_child_text(entry, "content")
        raw_published = (
            _first_child_text(entry, "published")
            or _first_child_text(entry, "updated")
            or _first_child_text(entry, "issued")
        )
    else:
        raw_title = _first_child_text(entry, "title")
        raw_link = _first_child_text(entry, "link")
        raw_summary = (
            _first_child_text(entry, "description")
            or _first_child_text(entry, "summary")
            or _first_child_text(entry, "content")
        )
        raw_published = (
            _first_child_text(entry, "pubDate")
            or _first_child_text(entry, "published")
            or _first_child_text(entry, "updated")
            or _first_child_text(entry, "date")
        )

    title = raw_title or parsed_feed["title"] or feed.name or "Untitled"
    link = raw_link or parsed_feed["website_url"] or feed.website_url
    summary = _clean_summary(raw_summary)
    image_url = _extract_image_url(entry, raw_summary)
    published_at = _parse_published_at(raw_published)
    event_id = (link or f"{feed.id}:{title}:{raw_published}").strip()
    return {
        "event_id": event_id,
        "feed_id": str(feed.id),
        "feed_name": feed.name,
        "feed_favicon_url": feed.favicon_url,
        "feed_website_url": feed.website_url or parsed_feed["website_url"],
        "title": title,
        "link": link,
        "summary": summary,
        "image_url": image_url,
        "published_at": published_at,
    }


# Response-body caps for the SSRF-safe fetch below. Feeds can legitimately be
# a few megabytes; a page read for the AI assistant never needs more than a
# couple (``apps.ai_writer.services.fetch_link`` passes its own cap).
FEED_MAX_BYTES = 10 * 1024 * 1024
_REDIRECT_STATUSES = (301, 302, 303, 307, 308)
# Body headers that no longer describe the bytes once they've been read and
# decoded, so they're dropped from the response handed back to the caller.
_DECODED_BODY_HEADERS = {"content-encoding", "content-length", "transfer-encoding"}


def _fetch_hop(url, headers, *, timeout, max_bytes, content_types):
    """One GET, streamed and capped. Returns an ``httpx.Response`` or None.

    None when the declared or actual body exceeds ``max_bytes`` (counted after
    decompression, so a gzip bomb is cut off too), when the final response's
    Content-Type isn't one of ``content_types``, or when reading the body takes
    longer than twice ``timeout`` (``timeout`` alone is per socket operation,
    so a slow drip could otherwise hold the request open indefinitely).
    Redirects come back without their body for the caller to follow.
    """
    with httpx.stream("GET", url, headers=headers, timeout=timeout, follow_redirects=False) as response:
        kept_headers = {k: v for k, v in response.headers.items() if k.lower() not in _DECODED_BODY_HEADERS}
        if response.status_code in _REDIRECT_STATUSES:
            return httpx.Response(response.status_code, headers=kept_headers)
        if content_types is not None and response.status_code < 400:
            content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
            if content_type not in content_types:
                return None
        declared = response.headers.get("content-length") or ""
        if declared.isdigit() and int(declared) > max_bytes:
            return None
        deadline = time.monotonic() + 2 * timeout
        buf = bytearray()
        for chunk in response.iter_bytes():
            buf += chunk
            if len(buf) > max_bytes or time.monotonic() > deadline:
                return None
    return httpx.Response(response.status_code, headers=kept_headers, content=bytes(buf))


def _safe_fetch_feed(url, headers, *, timeout=8.0, max_redirects=5, max_bytes=FEED_MAX_BYTES, content_types=None):
    """Fetch *url* with manual redirect handling, re-validating each hop with
    is_safe_url. Returns (response, final_url) on success or (None, None) on
    any reject path (initial-URL failed SSRF check, intermediate hop failed
    SSRF check, broken redirect, transport error, body over ``max_bytes``,
    or — when ``content_types`` is given — a Content-Type outside it).
    """
    if not is_safe_url(url):
        return None, None
    current_url = url
    for _ in range(max_redirects + 1):
        try:
            response = _fetch_hop(
                current_url, headers, timeout=timeout, max_bytes=max_bytes, content_types=content_types
            )
        except httpx.HTTPError:
            return None, None
        if response is None:
            return None, None
        if response.status_code not in _REDIRECT_STATUSES:
            return response, current_url
        location = response.headers.get("Location")
        if not location:
            return None, None
        next_url = urljoin(current_url, location)
        if not is_safe_url(next_url):
            return None, None
        current_url = next_url
    # Too many redirects.
    return None, None


def _fetch_feed_events_for_workspace(feeds):
    """Fetch and aggregate recent events across all workspace feeds.

    Each feed is re-validated against is_safe_url at fetch time (not just at
    add time), and redirects are followed manually with per-hop SSRF checks.
    This closes the DNS-rebind / redirect-bait window that would otherwise
    let a previously-valid feed URL reach internal hosts on subsequent polls.
    """
    if not feeds:
        return []

    headers = {
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.1",
        "User-Agent": f"{settings.BRAND_SHORT_NAME} RSS Reader/1.0",
    }
    all_events = []
    for feed in feeds:
        response, _final_url = _safe_fetch_feed(feed.url, headers)
        if response is None or response.status_code >= 400:
            continue
        parsed_feed = _parse_feed_document(response.content)
        if not parsed_feed:
            continue
        for entry in parsed_feed["entries"][:50]:
            all_events.append(_build_event_from_entry(feed, parsed_feed, entry))

    deduped_events = []
    seen = set()
    for event in all_events:
        dedupe_key = (event["feed_id"], event["event_id"])
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        deduped_events.append(event)

    deduped_events.sort(
        key=lambda event: event["published_at"] or datetime(1970, 1, 1, tzinfo=UTC),
        reverse=True,
    )
    return deduped_events


def _get_cached_workspace_feed_events(workspace, feeds, force_refresh=False):
    """Return cached feed events and last refresh time for workspace feeds."""
    cache_key = _feed_events_cache_key(workspace.id)
    signature = tuple((str(feed.id), feed.url, feed.name, feed.website_url) for feed in feeds)
    cached = cache.get(cache_key)
    if cached and not force_refresh and cached.get("signature") == signature:
        return cached.get("events", []), cached.get("fetched_at")

    events = _fetch_feed_events_for_workspace(feeds)
    fetched_at = timezone.now()
    cache.set(
        cache_key,
        {
            "signature": signature,
            "events": events,
            "fetched_at": fetched_at,
        },
        FEED_EVENTS_CACHE_TTL_SECONDS,
    )
    return events, fetched_at


def _filter_events_for_feed(events, selected_feed_id):
    if selected_feed_id == "all":
        return events
    return [event for event in events if event["feed_id"] == selected_feed_id]


def _build_feed_events_context(workspace, selected_feed_id="all", offset=0):
    feeds = list(Feed.objects.for_workspace(workspace.id))
    selected_feed_id = _normalize_selected_feed_id(selected_feed_id, feeds)
    selected_feed = next((feed for feed in feeds if str(feed.id) == selected_feed_id), None)
    events, last_refreshed_at = _get_cached_workspace_feed_events(workspace, feeds)
    filtered_events = _filter_events_for_feed(events, selected_feed_id)
    page_events = filtered_events[offset : offset + FEED_EVENTS_PAGE_SIZE]
    next_offset = offset + FEED_EVENTS_PAGE_SIZE
    has_more = len(filtered_events) > next_offset
    return {
        "feeds": feeds,
        "selected_feed_id": selected_feed_id,
        "selected_feed": selected_feed,
        "events": page_events,
        "next_offset": next_offset,
        "has_more": has_more,
        "last_refreshed_at": last_refreshed_at,
        "total_event_count": len(filtered_events),
    }


def _render_feeds_tab(
    request,
    workspace,
    *,
    show_add_modal=False,
    add_rss_url="",
    add_error="",
    selected_feed_id="all",
):
    """Render the feeds tab partial with modal state and first event page."""
    context = _build_feed_events_context(workspace, selected_feed_id=selected_feed_id, offset=0)
    context.update(
        {
            "workspace": workspace,
            "show_add_modal": show_add_modal,
            "add_rss_url": add_rss_url,
            "add_error": add_error,
        }
    )
    return render(request, "composer/partials/feeds_tab.html", context)


def _validate_rss_url(rss_url):
    """Validate that a URL points to a reachable RSS/Atom XML feed."""
    headers = {
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.1",
        "User-Agent": f"{settings.BRAND_SHORT_NAME} RSS Validator/1.0",
    }
    response, _final_url = _safe_fetch_feed(rss_url, headers)
    if response is None:
        return False, "Could not reach this URL. Please check the link and try again.", {}

    if response.status_code >= 400:
        return False, "This URL could not be loaded as a feed.", {}

    parsed_feed = _parse_feed_document(response.content)
    if not parsed_feed:
        return False, "This URL is reachable, but it does not appear to be a valid RSS/Atom feed.", {}

    return (
        True,
        "",
        {
            "title": parsed_feed.get("title", "").strip(),
            "website_url": parsed_feed.get("website_url", "").strip(),
        },
    )


@login_required
@require_permission("create_posts")
@require_GET
def feed_list(request, workspace_id):
    """Return the feeds tab partial (empty state or feed list)."""
    workspace = _get_workspace(request, workspace_id)
    selected_feed_id = request.GET.get("feed_id", "all")
    is_append = request.GET.get("append") == "1"
    offset = _coerce_positive_int(request.GET.get("offset"), default=0)

    if is_append:
        context = _build_feed_events_context(workspace, selected_feed_id=selected_feed_id, offset=offset)
        context.update(
            {
                "workspace": workspace,
                "show_empty": False,
            }
        )
        return render(request, "composer/partials/feed_events_batch.html", context)

    return _render_feeds_tab(request, workspace, selected_feed_id=selected_feed_id)


@login_required
@require_permission("create_posts")
@require_POST
def feed_add(request, workspace_id):
    """Add a feed subscription to the workspace."""
    from django.core.exceptions import ValidationError
    from django.core.validators import URLValidator

    workspace = _get_workspace(request, workspace_id)
    rss_url = request.POST.get("rss_url", "").strip()
    name = request.POST.get("name", "").strip()
    website_url = request.POST.get("website_url", "").strip()
    source = request.POST.get("source", "")
    category = request.POST.get("category", "brightbean-favorites")
    selected_feed_id = request.POST.get("feed_id", "all")
    derived_metadata = {}

    if not rss_url:
        if source == "explore":
            return HttpResponse("Feed URL is required.", status=400)
        return _render_feeds_tab(
            request,
            workspace,
            show_add_modal=True,
            add_rss_url=rss_url,
            add_error="Feed URL is required.",
            selected_feed_id=selected_feed_id,
        )

    validator = URLValidator()
    try:
        validator(rss_url)
    except ValidationError:
        if source == "explore":
            return HttpResponse("Invalid URL.", status=400)
        return _render_feeds_tab(
            request,
            workspace,
            show_add_modal=True,
            add_rss_url=rss_url,
            add_error="Invalid URL.",
            selected_feed_id=selected_feed_id,
        )

    if source != "explore":
        is_valid_rss, validation_error, derived_metadata = _validate_rss_url(rss_url)
        if not is_valid_rss:
            return _render_feeds_tab(
                request,
                workspace,
                show_add_modal=True,
                add_rss_url=rss_url,
                add_error=validation_error,
                selected_feed_id=selected_feed_id,
            )

    if Feed.objects.for_workspace(workspace.id).filter(url=rss_url).exists():
        # Already subscribed - if from explore, just re-render explore view
        if source == "explore":
            return _render_explore(request, workspace, category)
        return _render_feeds_tab(
            request,
            workspace,
            show_add_modal=True,
            add_rss_url=rss_url,
            add_error="Already subscribed to this feed.",
            selected_feed_id=selected_feed_id,
        )

    resolved_name = name or derived_metadata.get("title") or rss_url
    resolved_website_url = website_url or derived_metadata.get("website_url", "")
    Feed.objects.create(
        workspace=workspace,
        name=resolved_name,
        url=rss_url,
        website_url=resolved_website_url,
        added_by=request.user,
    )
    cache.delete(_feed_events_cache_key(workspace.id))

    if source == "explore":
        response = _render_explore(request, workspace, category)
        response["HX-Trigger"] = "feedsUpdated"
        return response

    return _render_feeds_tab(request, workspace, selected_feed_id=selected_feed_id)


@login_required
@require_permission("create_posts")
@require_POST
def feed_delete(request, workspace_id, feed_id):
    """Remove a feed subscription."""
    workspace = _get_workspace(request, workspace_id)
    selected_feed_id = request.POST.get("feed_id", "all")
    feed = get_object_or_404(Feed, id=feed_id, workspace=workspace)
    feed.delete()
    cache.delete(_feed_events_cache_key(workspace.id))

    return _render_feeds_tab(request, workspace, selected_feed_id=selected_feed_id)


@login_required
@require_permission("create_posts")
@require_GET
def feed_explore(request, workspace_id):
    """Return the explore feeds modal content for a given category."""
    workspace = _get_workspace(request, workspace_id)
    category = request.GET.get("category", "brightbean-favorites")
    return _render_explore(request, workspace, category)


def _render_explore(request, workspace, category):
    """Shared helper to render the explore feeds partial."""
    from .curated_feeds import get_feed_categories, get_feeds_for_category

    subscribed_urls = set(Feed.objects.for_workspace(workspace.id).values_list("url", flat=True))
    curated = get_feeds_for_category(category)
    for feed in curated:
        feed["subscribed"] = feed["rss"] in subscribed_urls

    return render(
        request,
        "composer/partials/feeds_explore.html",
        {
            "workspace": workspace,
            "categories": get_feed_categories(),
            "active_category": category,
            "curated_feeds": curated,
        },
    )
