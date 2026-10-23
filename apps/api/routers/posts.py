"""``/api/v1/posts/*`` — create, list, read, update, schedule, cancel, delete, duplicate, resume a thread.

Every write path:

1. Enforces HTTP-level rate limits (per-key, per-workspace, global).
2. Replays the response if an idempotency key matches an earlier request.
3. Checks workspace permission via the shared ``@require_permission`` helper.
4. Validates the target ``SocialAccount`` is in the key's allowlist.
5. Checks per-platform 24h quota.
6. Calls the composer service layer (single source of truth for state).
7. Writes an audit log row.
8. Persists the response under the idempotency key if one was passed.

This ordering means an expensive operation (the create) can't run until
both the cheap rate-limit check and the issuer-permission check pass —
defence against spending DB cycles for unauthorized callers.
"""

from __future__ import annotations

import datetime as dt
import uuid

from django.db import transaction
from django.http import HttpRequest
from django.shortcuts import get_object_or_404
from ninja import Query, Router
from ninja.errors import HttpError

from apps.api import ops
from apps.api.limits import check_platform_quota, enforce_http_rate_limits
from apps.api.middleware import (
    claim_idempotency_slot,
    finalize_idempotent_response,
    fingerprint_request,
    log_audit_entry,
    release_idempotent_claim,
)
from apps.api.routers.common import run_op
from apps.api.schemas import (
    CreatePostRequest,
    DeletedResponse,
    PostResponse,
    PostsListResponse,
    RecurrenceSpec,
    ScheduleRequest,
    UpdatePostRequest,
)
from apps.calendar.services import clean_recurrence, save_recurrence, stop_recurrence
from apps.composer.models import Post
from apps.composer.services import (
    clean_post_settings,
    create_post,
    media_change_blocker,
    replace_post_settings,
    resolve_thread_part_assets,
    set_post_parts,
    sync_post_scheduled_at,
    thread_change_blocker,
    transition_platform_post,
    usable_media_types,
)
from apps.social_accounts.models import SocialAccount

router = Router(tags=["posts"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_perm(request: HttpRequest, key: str) -> None:
    """Re-check a workspace permission inside a Ninja route body.

    We can't decorate Ninja routes with ``@require_permission`` because it
    expects a Django view signature; instead we inline the same check
    against the virtual membership shim.
    """
    membership = getattr(request, "workspace_membership", None)
    if membership is None or not membership.effective_permissions.get(key, False):
        raise HttpError(403, f"Permission denied: {key}")


def _resolve_account(request: HttpRequest, social_account_id: uuid.UUID) -> SocialAccount:
    """Resolve the target account and verify it is in the key's allowlist.

    Defence against confused-deputy attacks: even though the bearer is
    valid, the caller may only act on accounts the issuer explicitly
    listed at issuance time.
    """
    api_key = request.api_key  # type: ignore[attr-defined]  # set by ApiKeyAuth
    allowlist_ids = {sa.id for sa in api_key.social_accounts.all()}
    if social_account_id not in allowlist_ids:
        raise HttpError(403, "SocialAccount is not in this key's allowlist.")
    return SocialAccount.objects.get(id=social_account_id)


def _can_view_internal_notes(request: HttpRequest) -> bool:
    """Whether this caller may see a post's team-only ``internal_notes``.

    The composer hides ``internal_notes`` from the ``client`` / ``viewer``
    workspace roles ([views.py](apps/composer/views.py): ``can_view_internal_notes``).
    Those are exactly the builtin roles without ``create_posts`` (see
    ``BUILTIN_ROLE_PERMISSIONS``), so gating visibility on ``create_posts``
    reproduces that rule uniformly for both API keys and OAuth members. Reads
    (`GET /posts/{id}`) aren't otherwise permission-gated, so without this the
    shared serializer would leak notes to any read-capable client/viewer.
    """
    membership = getattr(request, "workspace_membership", None)
    return bool(membership and membership.effective_permissions.get("create_posts", False))


def _check_recurrence(spec: RecurrenceSpec, *, workspace, scheduled_at) -> None:
    """422 unless ``spec`` can repeat a post first scheduled at ``scheduled_at``.

    Run before anything is written, so a bad repeat never leaves a post
    scheduled without it.
    """
    import zoneinfo

    ws_tz = zoneinfo.ZoneInfo(workspace.effective_timezone or "UTC")
    try:
        clean_recurrence(**spec.rule_fields(), start=scheduled_at.astimezone(ws_tz))
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc


def _post_to_response(request: HttpRequest, post: Post) -> PostResponse:
    return PostResponse.from_post(post, include_internal_notes=_can_view_internal_notes(request))


def _get_workspace_post(request: HttpRequest, post_id: uuid.UUID) -> Post:
    """Fetch a Post that belongs to the key's workspace **and** whose every
    ``PlatformPost`` child targets a ``SocialAccount`` in the key's
    allowlist.

    The workspace filter alone is not enough — a key scoped to LinkedIn-A
    could otherwise read or mutate a Post whose only child is for
    Twitter-B in the same workspace if it happened to know the Post UUID.
    The "all children in allowlist" rule (rather than "any child") means
    schedule/cancel/update can freely iterate ``post.platform_posts``
    without us having to scope each operation sub-Post — there is no
    foreign child for them to touch.

    We intentionally don't distinguish "doesn't exist" from "exists in
    another workspace" from "exists but partially out of scope" — all
    three return 404 so the API doesn't leak the existence of foreign
    IDs to a partial-scope bearer.
    """
    from django.http import Http404

    post = get_object_or_404(
        Post.objects.prefetch_related(
            "platform_posts__social_account",
            "platform_posts__part_states__part",
            "thread_parts__media_attachments",
        ).select_related("recurrence_rule"),
        id=post_id,
        workspace_id=request.api_key.workspace_id,  # type: ignore[attr-defined]
    )
    allowed_ids = {sa.id for sa in request.api_key.social_accounts.all()}  # type: ignore[attr-defined]
    pp_account_ids = {pp.social_account_id for pp in post.platform_posts.all()}
    # No platform_posts → nothing this key could legitimately act on.
    # Foreign child → leaking even via a read would be a confused-deputy.
    if not pp_account_ids or not pp_account_ids.issubset(allowed_ids):
        raise Http404()
    return post


def _check_taxonomy(request: HttpRequest, *, tags, category_id) -> None:
    try:
        ops.validate_post_taxonomy(ops.Actor.from_request(request), tags=tags, category_id=category_id)
    except ops.OpError as exc:
        raise HttpError(exc.status, exc.message) from exc


def _apply_taxonomy(request: HttpRequest, post: Post, *, tags, category_id) -> None:
    try:
        ops.apply_post_taxonomy(ops.Actor.from_request(request), post, tags=tags, category_id=category_id)
    except ops.OpError as exc:
        raise HttpError(exc.status, exc.message) from exc


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/", response={201: PostResponse, 200: PostResponse}, summary="Create a draft or scheduled post")
def create(request, payload: CreatePostRequest):
    enforce_http_rate_limits(request, is_write=True)
    _require_perm(request, "create_posts")
    # Scheduling implies "ready to publish without further human review",
    # which is exactly what ``publish_directly`` gates in the composer
    # ([views.py:797](apps/composer/views.py)). The REST API must mirror
    # that contract — a key issued with only ``create_posts`` can park
    # drafts but cannot send them into the publisher's poll loop.
    if payload.action == "schedule":
        _require_perm(request, "publish_directly")

    # ---- Cheap validation runs BEFORE we touch the idempotency table.
    # The motivation: an early failure here (403, 422) would otherwise
    # leave a placeholder claim row that release-on-error has to clean
    # up — and forgetting one release path locks the agent out for 24h.
    # Doing all "can this request possibly succeed?" checks pre-claim
    # keeps the claim/release pair tight and confined to the post-claim
    # path (platform quota + create_post).
    social_account = _resolve_account(request, payload.social_account_id)
    _check_taxonomy(request, tags=payload.tags, category_id=payload.category_id)
    if payload.action == "schedule" and payload.scheduled_at is None:
        raise HttpError(422, "scheduled_at is required when action='schedule'.")
    repeat = payload.recurrence if payload.recurrence and payload.recurrence.frequency != "never" else None
    if repeat is not None:
        if payload.action != "schedule":
            raise HttpError(422, "recurrence needs action='schedule': only a scheduled post can repeat.")
        _check_recurrence(repeat, workspace=request.api_key.workspace, scheduled_at=payload.scheduled_at)

    # Build the platform_overrides dict and validate that each override's
    # social_account_id matches one of the post's target accounts. In the
    # current single-account API that's only ``payload.social_account_id``;
    # anything else would silently no-op at publish time, so we reject up
    # front. The plan's [Gap 2] section calls this out explicitly.
    platform_overrides: dict = {}
    for ov in payload.platform_overrides:
        if ov.social_account_id != payload.social_account_id:
            raise HttpError(
                422,
                (
                    f"platform_overrides[*].social_account_id must match the post's "
                    f"social_account_id ({payload.social_account_id}); got {ov.social_account_id}."
                ),
            )
        platform_overrides[ov.social_account_id] = {
            "title": ov.title,
            "caption": ov.caption,
            "first_comment": ov.first_comment,
        }

    # Same rule for each part's overrides: the single-account API has one
    # child, so an override for any other account could never apply.
    for part in payload.parts:
        for ov in part.platform_overrides:
            if ov.social_account_id != payload.social_account_id:
                raise HttpError(
                    422,
                    (
                        f"parts[*].platform_overrides[*].social_account_id must match the post's "
                        f"social_account_id ({payload.social_account_id}); got {ov.social_account_id}."
                    ),
                )

    # ---- Atomic claim-first idempotency. Three early-out branches
    # before we do *any* mutating work, so concurrent identical retries
    # can never both reach create_post:
    #   replay     — prior request already finished, return its response
    #   in_flight  — a concurrent peer holds the slot, return 409
    #   passthrough/claimed — caller proceeds; only the "claimed" path
    #                         must finalize or release before returning.
    fingerprint = fingerprint_request(request.method or "POST", request.path, payload.dict(by_alias=True))
    # Accept the canonical ``Idempotency-Key`` HTTP header (Stripe-style)
    # in addition to the body field. The header is the industry
    # convention — clients that retry on network timeout typically reuse
    # the header but can't easily re-send the same JSON body — so
    # ignoring it would silently break the "retry-safe" contract Codex
    # review (PR #53) flagged. The body field still wins when both are
    # present, so a deliberate caller can override.
    idempotency_key = payload.idempotency_key or request.headers.get("Idempotency-Key") or None
    try:
        disposition, replay_status, replay_body = claim_idempotency_slot(
            api_key=request.api_key,
            idempotency_key=idempotency_key,
            fingerprint=fingerprint,
        )
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc
    if disposition == "replay":
        return replay_status, replay_body
    if disposition == "in_flight":
        raise HttpError(
            409,
            "An identical request with this idempotency_key is still in flight; retry shortly.",
        )

    # Platform-quota check has to be inside the claim window because it
    # depends on database state that other concurrent claims could
    # change. Drafts are excluded from the count inside
    # ``check_platform_quota`` so creating drafts cannot exhaust the
    # platform's posting cap.
    if payload.action == "schedule":
        try:
            check_platform_quota(social_account)
        except HttpError:
            release_idempotent_claim(api_key=request.api_key, idempotency_key=idempotency_key)
            raise

    # Commit the post and replayable response together. If response construction
    # or finalization fails, rollback the post before releasing the claim; a
    # retry must not create a second post after a partially committed request.
    try:
        with transaction.atomic():
            post = create_post(
                workspace=request.api_key.workspace,
                social_account=social_account,
                caption=payload.caption,
                media_asset_ids=payload.media_asset_ids,
                title=payload.title,
                first_comment=payload.first_comment,
                internal_notes=payload.internal_notes,
                scheduled_at=payload.scheduled_at,
                # A scheduled post carries a real time, not a proposal — ignore any
                # proposed_publish_at when scheduling so the two never coexist.
                proposed_publish_at=None if payload.action == "schedule" else payload.proposed_publish_at,
                author=request.user if not request.user.is_anonymous else None,
                status="scheduled" if payload.action == "schedule" else "draft",
                platform_overrides=platform_overrides,
                parts=[part.to_spec() for part in payload.parts],
                post_settings=payload.post_settings,
            )
            _apply_taxonomy(request, post, tags=payload.tags, category_id=payload.category_id)
            if repeat is not None:
                save_recurrence(post, **repeat.rule_fields())
            body = _post_to_response(request, post)
            status_code = 201
            log_audit_entry(
                request,
                action=f"post.create.{payload.action}",
                target_id=post.id,
                status_code=status_code,
            )
            # ``model_dump(mode='json')`` yields JSON-safe primitives
            # (UUID→str, datetime→ISO-8601 str) so the response_body
            # JSONField round-trips cleanly through psycopg's jsonb adapter.
            finalize_idempotent_response(
                api_key=request.api_key,
                idempotency_key=idempotency_key,
                status_code=status_code,
                body=body.model_dump(mode="json"),
            )
    except ValueError as exc:
        # Use the *effective* idempotency key (header fallback applied)
        # so a header-only client's claim is released too — Codex PR #53
        # round-3 flagged that the previous ``payload.idempotency_key``
        # was None on header-only requests and left the claim wedged in
        # PENDING until the 24h sweep.
        release_idempotent_claim(api_key=request.api_key, idempotency_key=idempotency_key)
        raise HttpError(422, str(exc)) from exc
    except Exception:
        release_idempotent_claim(api_key=request.api_key, idempotency_key=idempotency_key)
        raise
    return status_code, body


@router.get(
    "/",
    response=PostsListResponse,
    summary="List posts",
    description=(
        "Newest first (by creation time). Only posts whose every platform target is on the key's account allowlist "
        "are returned. `status` and `social_account_id` must hold on the same platform target. `date_from`/`date_to` "
        "bound the post's date: when it goes out, or when it was created while it has no schedule. `tag` is an exact "
        "tag name; `search` matches the caption or title. When `next_cursor` is non-null, pass it back as `cursor`."
    ),
)
def list_posts(
    request,
    status: str | None = Query(None, description="Per-platform status; a post matches if any target has it."),
    social_account_id: uuid.UUID | None = Query(None),
    date_from: dt.datetime | None = Query(None, description="ISO 8601; a time without an offset is UTC."),
    date_to: dt.datetime | None = Query(None, description="ISO 8601; a time without an offset is UTC."),
    tag: str | None = Query(None),
    search: str | None = Query(None),
    limit: int = Query(ops.LIMIT_DEFAULT, ge=1, le=ops.LIMIT_MAX),
    cursor: str | None = Query(None),
):
    return run_op(
        request,
        ops.list_posts,
        status=status,
        social_account_id=social_account_id,
        date_from=date_from,
        date_to=date_to,
        tag=tag,
        search=search,
        limit=limit,
        cursor=cursor,
        action="post.list",
        is_write=False,
    )


@router.get("/{post_id}", response=PostResponse, summary="Read a single post")
def retrieve(request, post_id: uuid.UUID):
    enforce_http_rate_limits(request, is_write=False)
    post = _get_workspace_post(request, post_id)
    log_audit_entry(request, action="post.read", target_id=post.id, status_code=200)
    return _post_to_response(request, post)


@router.patch("/{post_id}", response=PostResponse, summary="Update draft fields")
def update(request, post_id: uuid.UUID, payload: UpdatePostRequest):
    enforce_http_rate_limits(request, is_write=True)
    _require_perm(request, "create_posts")  # mirrors composer's create-or-edit perm
    post = _get_workspace_post(request, post_id)
    _check_taxonomy(request, tags=payload.tags, category_id=payload.category_id)

    # Only allow updates while editable.
    if not post.is_editable:
        raise HttpError(409, f"Post is not editable in status {post.status}.")

    # Re-timing a scheduled post is publish-budget behaviour: pushing
    # ``scheduled_at`` into the past makes the publisher fire the post on
    # its very next poll (~15 s), and pushing it far into the future buries
    # admin-scheduled content. Either is a privilege escalation for a key
    # that doesn't hold ``publish_directly`` — the create / schedule routes
    # and every MCP transition tool gate this exact mutation on that
    # permission, and the PATCH route must do the same to stay consistent.
    # Codex PR #53 security review (round 4) caught this gap.
    if payload.scheduled_at is not None and post.platform_posts.filter(status="scheduled").exists():
        _require_perm(request, "publish_directly")

    # Repeating hands each generated copy to the publisher too, so starting or
    # changing a series takes the same permission; stopping one doesn't.
    repeat = payload.recurrence if payload.recurrence and payload.recurrence.frequency != "never" else None
    if payload.recurrence is not None and post.recurrence_source_id is not None:
        raise HttpError(
            422,
            f"This post is a copy in a repeating series; change the series on post {post.recurrence_source_id}.",
        )
    if repeat is not None:
        _require_perm(request, "publish_directly")
        first_at = payload.scheduled_at or post.scheduled_at
        if first_at is None or not post.platform_posts.filter(status="scheduled").exists():
            raise HttpError(422, "Only a scheduled post can repeat; schedule it first.")
        _check_recurrence(repeat, workspace=post.workspace, scheduled_at=first_at)

    # ---- Validate-everything-first.
    #
    # Codex review found two bugs in the previous implementation:
    #   (a) ``scheduled_children.update(scheduled_at=...)`` ran BEFORE
    #       media validation, so a 422 from a foreign media asset still
    #       committed the new schedule timestamp to the DB.
    #   (b) ``post.media_attachments.all().delete()`` + the create loop
    #       were not wrapped in a transaction, so a mid-loop failure
    #       deleted the originals and persisted only a partial new set.
    # Fix: resolve every required reference and reject every invalid
    # input before any database mutation, then perform all mutations
    # inside a single ``transaction.atomic()`` block. Either everything
    # commits or nothing does.
    from django.db import transaction

    from apps.composer.models import PostMedia
    from apps.media_library.models import MediaAsset

    wanted_media: list = []
    resolved_assets: dict = {}
    if payload.media_asset_ids is not None:
        wanted_media = list(payload.media_asset_ids)
        resolved_assets = {a.id: a for a in MediaAsset.objects.filter(id__in=wanted_media, workspace=post.workspace)}
        missing = [i for i in wanted_media if i not in resolved_assets]
        if missing:
            raise HttpError(422, f"Media asset(s) not in workspace: {missing}")
        # A scheduled post may be edited, but not into one it can't publish:
        # swapping a TikTok post's video for an image, or emptying a pin's
        # media, would leave it queued to fail after the schedule route had
        # checked it.
        reason = media_change_blocker(
            post,
            media_types_after=usable_media_types(resolved_assets[mid] for mid in wanted_media),
            assets_after=[resolved_assets[mid] for mid in wanted_media],
        )
        if reason:
            raise HttpError(422, reason)

    part_specs = None
    part_assets: dict = {}
    if payload.parts is not None:
        part_specs = [part.to_spec() for part in payload.parts]
        child_ids = {str(pp.social_account_id) for pp in post.platform_posts.all()}
        foreign = {acc for spec in part_specs for acc in spec.caption_overrides if acc not in child_ids}
        if foreign:
            raise HttpError(
                422, f"parts[*].platform_overrides name accounts this post doesn't target: {sorted(foreign)}"
            )
        try:
            part_assets = resolve_thread_part_assets(post.workspace, part_specs)
        except ValueError as exc:
            raise HttpError(422, str(exc)) from exc
    if payload.parts is not None or payload.first_comment is not None:
        # As with media: a scheduled post may be edited, but not into a thread
        # its platform would publish only the first part of — nor into a
        # comments-mode thread that also carries a first comment.
        reason = thread_change_blocker(post, part_specs, part_assets, first_comment=payload.first_comment)
        if reason:
            raise HttpError(422, reason)

    settings_by_child: dict = {}
    if payload.post_settings is not None:
        settings_by_child = _post_settings_by_child(post, payload.post_settings)

    with transaction.atomic():
        update_fields: list[str] = []
        for pp, platform_extra in settings_by_child.items():
            pp.platform_extra = platform_extra
            pp.save(update_fields=["platform_extra", "updated_at"])
        if payload.caption is not None:
            post.caption = payload.caption
            update_fields.append("caption")
        if payload.title is not None:
            post.title = payload.title
            update_fields.append("title")
        if payload.first_comment is not None:
            post.first_comment = payload.first_comment
            update_fields.append("first_comment")
        if payload.internal_notes is not None:
            post.internal_notes = payload.internal_notes
            update_fields.append("internal_notes")
        if payload.scheduled_at is not None:
            # Re-time any currently-scheduled child. Drafts are unaffected.
            # ``QuerySet.update()`` bypasses ``auto_now``, so we include
            # ``updated_at=timezone.now()`` explicitly. Otherwise each
            # child's ``updated_at`` would freeze at its creation time
            # despite an effective state change.
            from django.utils import timezone as _tz

            scheduled_children = post.platform_posts.filter(status="scheduled")
            scheduled_children.update(scheduled_at=payload.scheduled_at, updated_at=_tz.now())
            post.scheduled_at = payload.scheduled_at
            update_fields.append("scheduled_at")
        if payload.proposed_publish_at is not None:
            # Draft-stage suggestion; null is a no-op, consistent with the
            # other PATCH fields. Not gated on publish_directly — it never
            # reaches the publisher.
            post.proposed_publish_at = payload.proposed_publish_at
            update_fields.append("proposed_publish_at")
        if payload.media_asset_ids is not None:
            # Replace the attachment set in order. Validated above, so
            # the only remaining failure modes are DB-level — the atomic
            # block rolls back the whole route if any single
            # ``PostMedia.objects.create`` raises.
            post.media_attachments.all().delete()
            for position, mid in enumerate(wanted_media):
                PostMedia.objects.create(
                    post=post,
                    media_asset=resolved_assets[mid],
                    position=position,
                )

        if part_specs is not None:
            try:
                set_post_parts(post, part_specs, asset_map=part_assets)
            except ValueError as exc:
                raise HttpError(409, str(exc)) from exc
            # The parts are the post's content too; touch it so "modified
            # since" readers see the change.
            if not update_fields:
                post.save(update_fields=["updated_at"])

        if update_fields:
            post.save(update_fields=[*update_fields, "updated_at"])
        elif settings_by_child and part_specs is None:
            post.save(update_fields=["updated_at"])

        sync_post_scheduled_at(post)
        _apply_taxonomy(request, post, tags=payload.tags, category_id=payload.category_id)

        if repeat is not None:
            save_recurrence(post, **repeat.rule_fields(), refresh=payload.scheduled_at is not None)
        elif payload.recurrence is not None:
            stop_recurrence(post)

    post.refresh_from_db()
    log_audit_entry(request, action="post.update", target_id=post.id, status_code=200)
    return _post_to_response(request, post)


def _post_settings_by_child(post, settings: dict) -> dict:
    """The ``platform_extra`` each child gets from a PATCH's ``post_settings``, or ``HttpError`` 422.

    Each child takes the keys its own platform declares, replaced as the
    composer replaces them; a key no child's platform declares is refused, and
    a scheduled child must stay publishable.
    """
    from providers import post_settings_error_for, post_settings_for

    result = {}
    used: set[str] = set()
    for pp in post.platform_posts.select_related("social_account"):
        platform = pp.social_account.platform
        declared = {field.key for field in post_settings_for(platform)}
        if not declared:
            continue
        mine = {key: value for key, value in settings.items() if key in declared}
        used.update(mine)
        try:
            cleaned = clean_post_settings(platform, mine)
        except ValueError as exc:
            raise HttpError(422, str(exc)) from exc
        platform_extra = replace_post_settings(platform, pp.platform_extra, cleaned)
        if pp.status == "scheduled":
            reason = post_settings_error_for(platform, platform_extra)
            if reason:
                raise HttpError(422, f"{pp.social_account.get_platform_display()}: {reason}")
        result[pp] = platform_extra
    unknown = sorted(set(settings) - used)
    if unknown:
        raise HttpError(422, f"Unknown post setting(s) {unknown}: none of this post's accounts declare them.")
    return result


@router.post("/{post_id}/schedule", response=PostResponse, summary="Schedule a draft or approved post")
def schedule(request, post_id: uuid.UUID, payload: ScheduleRequest):
    enforce_http_rate_limits(request, is_write=True)
    # Same ``publish_directly`` contract as the create-with-schedule
    # branch: only keys that can publish-directly may push a post into
    # the SCHEDULED state. See ``create``.
    _require_perm(request, "create_posts")
    _require_perm(request, "publish_directly")
    post = _get_workspace_post(request, post_id)

    # Schedule every draft or approved child; a single-account key produces one child,
    # but defensively we apply the transition to all draft children so we
    # don't half-schedule.
    schedulable = list(post.platform_posts.filter(status__in=["draft", "approved"]))
    if not schedulable:
        raise HttpError(409, "No draft or approved platform posts to schedule.")
    repeat = payload.recurrence if payload.recurrence and payload.recurrence.frequency != "never" else None
    if repeat is not None:
        if post.recurrence_source_id is not None:
            raise HttpError(
                422,
                f"This post is a copy in a repeating series; change the series on post {post.recurrence_source_id}.",
            )
        _check_recurrence(repeat, workspace=post.workspace, scheduled_at=payload.scheduled_at)

    # Quota check is per-account, so we evaluate it once per child before
    # we touch any state. Doing the checks first means an over-quota
    # account fails the whole route with 429 — no partial commit.
    for pp in schedulable:
        check_platform_quota(pp.social_account)

    # Wrap the per-child transitions in a single outer atomic so a
    # mid-loop ValueError rolls back any earlier ``scheduled`` commits.
    # Without this, child 1 could be persisted as ``scheduled`` while
    # child 2's ``transition_to`` rejects the move (concurrent admin
    # edit, state-machine conflict) — the route 422s but the post is
    # left in a half-scheduled state.
    from django.db import transaction

    with transaction.atomic():
        for pp in schedulable:
            try:
                transition_platform_post(pp, "scheduled", scheduled_at=payload.scheduled_at)
            except ValueError as exc:
                raise HttpError(422, str(exc)) from exc
        if repeat is not None:
            post.refresh_from_db()
            save_recurrence(post, **repeat.rule_fields(), refresh=True)

    post.refresh_from_db()
    log_audit_entry(request, action="post.schedule", target_id=post.id, status_code=200)
    return _post_to_response(request, post)


@router.post("/{post_id}/cancel", response=PostResponse, summary="Cancel a scheduled post (back to draft)")
def cancel(request, post_id: uuid.UUID):
    enforce_http_rate_limits(request, is_write=True)
    _require_perm(request, "create_posts")
    post = _get_workspace_post(request, post_id)

    scheduled_children = list(post.platform_posts.filter(status="scheduled"))
    if not scheduled_children:
        raise HttpError(409, "No scheduled platform posts to cancel.")

    # Same atomic-loop reasoning as ``schedule``: any per-child transition
    # failure rolls back the whole cancellation. Half-cancelled posts
    # would otherwise leave the publisher with a mix of ``draft`` and
    # ``scheduled`` children, which is exactly the inconsistent state
    # the route was supposed to prevent.
    from django.db import transaction

    with transaction.atomic():
        for pp in scheduled_children:
            try:
                transition_platform_post(pp, "draft")
            except ValueError as exc:
                raise HttpError(422, str(exc)) from exc

    post.refresh_from_db()
    log_audit_entry(request, action="post.cancel", target_id=post.id, status_code=200)
    return _post_to_response(request, post)


@router.delete(
    "/{post_id}",
    response=DeletedResponse,
    summary="Delete a post",
    description=(
        "Needs `create_posts` (and `edit_others_posts` for someone else's post). A post that is publishing or "
        "already published is an audit record and is refused with 409; cancel a scheduled post first or just delete it."
    ),
)
def delete(request, post_id: uuid.UUID):
    return run_op(request, ops.delete_post, post_id, action="post.delete", is_write=True, target_id=post_id)


@router.post(
    "/{post_id}/duplicate",
    response={201: PostResponse},
    summary="Duplicate a post as a new draft",
    description="Copies content, media, thread parts and per-platform settings; the copy has no schedule. Needs `create_posts`.",
)
def duplicate(request, post_id: uuid.UUID):
    post = run_op(
        request,
        ops.duplicate_post,
        post_id,
        action="post.duplicate",
        is_write=True,
        target_id=lambda p: p.id,
        status_code=201,
    )
    return 201, post


@router.post(
    "/{post_id}/resume-thread",
    response=PostResponse,
    summary="Resume a partially published thread from the part where it stopped",
)
def resume_thread_route(request, post_id: uuid.UUID, confirm_unconfirmed: bool = False):
    """Restart each stopped thread on the post at the part where it stopped.

    ``?confirm_unconfirmed=true`` is required when that part is
    ``unconfirmed``: the platform may already have published it, and resuming
    sends it again, so the caller confirms they checked the platform first.
    """
    enforce_http_rate_limits(request, is_write=True)
    # Resuming publishes the rest of the thread, so it takes the same
    # permission as scheduling it did.
    _require_perm(request, "create_posts")
    _require_perm(request, "publish_directly")
    post = _get_workspace_post(request, post_id)

    from apps.publisher.engine import resume_thread

    stopped = [pp for pp in post.platform_posts.all() if pp.is_thread_partial]
    if not stopped:
        raise HttpError(409, "No thread on this post is stopped part-way.")
    for pp in stopped:
        try:
            resume_thread(pp, confirm_unconfirmed=confirm_unconfirmed)
        except ValueError as exc:
            raise HttpError(409, str(exc)) from exc

    post = _get_workspace_post(request, post_id)
    log_audit_entry(request, action="post.resume_thread", target_id=post.id, status_code=200)
    return _post_to_response(request, post)
