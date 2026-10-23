"""Workspace operations shared by the REST routers and the MCP tools.

Each function takes an :class:`Actor` (the key or OAuth member making the call
plus its workspace and permissions) and returns an API response schema, so the
two surfaces enforce the same permission, allowlist and validation rules and
cannot drift. A refusal is an :class:`OpError`, which REST turns into
``HttpError`` and MCP into a JSON-RPC ``INVALID_PARAMS`` error.

Two rules hold throughout:

* Everything is scoped to the actor's workspace; an id from another workspace
  is a 404, never a 403, so foreign ids can't be probed.
* Anything tied to a social account honours the key's account allowlist: reads
  filter to it, writes outside it are refused. A post counts as visible only
  when every platform target is on the allowlist.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from typing import Any

from django.db import IntegrityError, transaction
from django.db.models import Count, Exists, OuterRef, Q, QuerySet
from django.db.models.functions import Coalesce
from django.utils import timezone
from ninja.errors import HttpError

from apps.api.limits import check_platform_quota
from apps.api.pagination import decode_offset_cursor, encode_offset_cursor
from apps.api.schemas import (
    AddEvergreenRequest,
    ApplyBestTimesResponse,
    BestTimeResponse,
    BestTimesResponse,
    BrandVoiceResponse,
    CalendarEventResponse,
    CalendarEventsListResponse,
    CategoriesListResponse,
    CategoryResponse,
    CommentResponse,
    CommentsListResponse,
    DeletedResponse,
    EvergreenDetailResponse,
    EvergreenItemResponse,
    EvergreenListResponse,
    EvergreenRepeatResponse,
    HashtagSetsListResponse,
    HashtagSetSummary,
    IdeaGroupResponse,
    IdeaGroupsListResponse,
    IdeaResponse,
    IdeasListResponse,
    MemberResponse,
    MembersListResponse,
    NextFreeSlotsResponse,
    PostingSlotResponse,
    PostingSlotsListResponse,
    PostResponse,
    PostsListResponse,
    QueueDetailResponse,
    QueueEntryResponse,
    QueuesListResponse,
    QueueSummary,
    RecurrenceSpec,
    ReportResponse,
    ReportRunResponse,
    ReportsListResponse,
    SignaturesListResponse,
    SignatureSummary,
    TagResponse,
    TagsListResponse,
    TemplateResponse,
    TemplatesListResponse,
    UpdatePostRequest,
    UsageMeterResponse,
    UsageResponse,
)
from apps.approvals import comments as comment_service
from apps.approvals import services as approval_services
from apps.approvals.models import PostComment
from apps.calendar import evergreen as evergreen_service
from apps.calendar import services as calendar_services
from apps.calendar.models import CustomCalendarEvent, EvergreenItem, PostingSlot, Queue
from apps.common.validators import MAX_TAG_LENGTH, MAX_TAGS, is_valid_hex_color
from apps.composer import services as composer_services
from apps.composer.models import (
    ContentCategory,
    HashtagSet,
    Idea,
    IdeaGroup,
    PlatformPost,
    Post,
    PostMedia,
    PostTemplate,
    Signature,
    Tag,
)
from apps.composer.services import clone_post, transition_platform_post
from apps.media_library.models import MediaAsset
from apps.members.models import WorkspaceMembership
from apps.social_accounts.models import SocialAccount

LIMIT_DEFAULT = 50
LIMIT_MAX = 100

# Statuses an approval list can be asked for. ``pending`` (the default) is the
# two review stages a reviewer or client acts on.
APPROVAL_STATUSES = (
    "pending_review",
    "pending_client",
    "approved",
    "rejected",
    "changes_requested",
    "on_hold",
)
_PENDING_APPROVAL_STATUSES = ("pending_review", "pending_client")

# Brand voice bounds, shared with the settings page (apps.workspaces.views).
BRAND_VOICE_FIELD_MAX = 1000
BRAND_VOICE_EXAMPLE_MAX = 2000
BRAND_VOICE_MAX_EXAMPLES = 3


class OpError(Exception):
    """A refused operation. ``status`` is the HTTP status REST answers with."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Actor:
    """Who is calling: the key (or OAuth shim), its workspace, and its permissions."""

    api_key: Any
    workspace: Any
    membership: Any
    user: Any = None
    _allowed: set[uuid.UUID] | None = field(default=None, repr=False)

    @classmethod
    def from_request(cls, request) -> Actor:
        user = getattr(request, "user", None)
        return cls(
            api_key=request.api_key,
            workspace=request.workspace,
            membership=request.workspace_membership,
            user=user if user is not None and getattr(user, "is_authenticated", False) else None,
        )

    @classmethod
    def from_context(cls, context: dict[str, Any]) -> Actor:
        api_key = context["api_key"]
        return cls(
            api_key=api_key,
            workspace=context.get("workspace") or api_key.workspace,
            membership=context.get("membership"),
            user=getattr(api_key, "issued_by", None),
        )

    @property
    def workspace_id(self):
        return self.workspace.id

    @property
    def allowed_account_ids(self) -> set[uuid.UUID]:
        if self._allowed is None:
            self._allowed = {sa.id for sa in self.api_key.social_accounts.all()}
        return self._allowed

    def has(self, permission: str) -> bool:
        return bool(self.membership and self.membership.effective_permissions.get(permission, False))

    def require(self, permission: str) -> None:
        if not self.has(permission):
            raise OpError(403, f"Permission denied: {permission}")

    def require_any(self, *permissions: str) -> None:
        if not any(self.has(p) for p in permissions):
            raise OpError(403, f"Permission denied: needs one of {', '.join(permissions)}")

    def real_membership(self) -> WorkspaceMembership | None:
        """The calling person's actual workspace membership (their role), if they still have one."""
        if self.user is None:
            return None
        return (
            WorkspaceMembership.objects.select_related("custom_role")
            .filter(user=self.user, workspace_id=self.workspace_id)
            .first()
        )

    def require_user(self) -> Any:
        if self.user is None:
            raise OpError(403, "This key's issuer no longer exists, so it can't act as a person.")
        return self.user


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _paginate(qs: QuerySet, *, limit: int, cursor: str | None) -> tuple[list, str | None]:
    """One page of ``qs`` (ordered by the caller, ``id`` tiebreak included) and the next cursor."""
    if limit < 1 or limit > LIMIT_MAX:
        raise OpError(422, f"limit must be between 1 and {LIMIT_MAX}")
    try:
        offset = decode_offset_cursor(cursor)
    except ValueError as exc:
        raise OpError(422, "cursor is not a valid pagination cursor") from exc
    rows = list(qs[offset : offset + limit + 1])
    has_more = len(rows) > limit
    return rows[:limit], (encode_offset_cursor(offset + limit) if has_more else None)


def _aware(value: dt.datetime | None) -> dt.datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value


def _clean_tags(tags: list[str] | None) -> list[str]:
    cleaned: list[str] = []
    for raw in tags or []:
        name = raw.strip()
        if not name:
            continue
        if len(name) > MAX_TAG_LENGTH:
            raise OpError(422, f"A tag can be at most {MAX_TAG_LENGTH} characters.")
        if name not in cleaned:
            cleaned.append(name)
    if len(cleaned) > MAX_TAGS:
        raise OpError(422, f"At most {MAX_TAGS} tags are allowed.")
    return cleaned


def _sync_tags(workspace, names: list[str]) -> None:
    """Make sure every tag name used on content exists in the workspace's tag list."""
    if not names:
        return
    existing = set(Tag.objects.for_workspace(workspace.id).filter(name__in=names).values_list("name", flat=True))
    Tag.objects.bulk_create(
        [Tag(workspace=workspace, name=n) for n in names if n not in existing], ignore_conflicts=True
    )


def _resolve_category(actor: Actor, category_id: uuid.UUID | None) -> ContentCategory | None:
    if category_id is None:
        return None
    category = ContentCategory.objects.for_workspace(actor.workspace_id).filter(id=category_id).first()
    if category is None:
        raise OpError(422, "category_id not found in this workspace")
    return category


def _require_account(actor: Actor, social_account_id: uuid.UUID) -> SocialAccount:
    if social_account_id not in actor.allowed_account_ids:
        raise OpError(403, "social_account_id is not in this key's allowlist.")
    return SocialAccount.objects.get(id=social_account_id)


def _media_ids_in_workspace(actor: Actor, ids: list[uuid.UUID]) -> list[uuid.UUID]:
    wanted = list(dict.fromkeys(ids))
    found = set(MediaAsset.objects.filter(id__in=wanted, workspace_id=actor.workspace_id).values_list("id", flat=True))
    missing = [str(i) for i in wanted if i not in found]
    if missing:
        raise OpError(422, f"Media asset(s) not in workspace: {missing}")
    return wanted


# ---------------------------------------------------------------------------
# Posts
# ---------------------------------------------------------------------------


def visible_posts_qs(api_key) -> QuerySet[Post]:
    """Posts this key may see: in its workspace, with at least one platform target, every target allowlisted.

    Expressed in SQL (not by filtering child rows in Python) so callers can
    order, filter and paginate without scanning rows they may not see. A key
    with an empty allowlist sees nothing.
    """
    allowed = [sa.id for sa in api_key.social_accounts.all()]
    if not allowed:
        return Post.objects.none()
    children = PlatformPost.objects.filter(post_id=OuterRef("pk"))
    return (
        Post.objects.filter(workspace_id=api_key.workspace_id)
        .filter(Exists(children))
        .exclude(Exists(children.exclude(social_account_id__in=allowed)))
        .select_related("recurrence_rule")
    )


_POST_PREFETCH = (
    "platform_posts__social_account",
    "platform_posts__part_states__part",
    "thread_parts__media_attachments",
)


def _get_visible_post(actor: Actor, post_id: uuid.UUID, *, prefetch: bool = True) -> Post:
    qs = visible_posts_qs(actor.api_key)
    if prefetch:
        qs = qs.prefetch_related(*_POST_PREFETCH)
    try:
        return qs.get(id=post_id)
    except Post.DoesNotExist as exc:
        raise OpError(404, "Post not found") from exc


def post_response(actor: Actor, post: Post) -> PostResponse:
    return PostResponse.from_post(post, include_internal_notes=actor.has("create_posts"))


def apply_post_taxonomy(actor: Actor, post: Post, *, tags: list[str] | None, category_id: uuid.UUID | None) -> None:
    """Set a post's tags and category when given (``None`` leaves them as they are)."""
    fields: list[str] = []
    if tags is not None:
        post.tags = _clean_tags(tags)
        _sync_tags(actor.workspace, post.tags)
        fields.append("tags")
    if category_id is not None:
        post.category = _resolve_category(actor, category_id)
        fields.append("category")
    if fields:
        post.save(update_fields=[*fields, "updated_at"])


def validate_post_taxonomy(actor: Actor, *, tags: list[str] | None, category_id: uuid.UUID | None) -> None:
    """Refuse bad tags / a foreign category before anything is written."""
    _clean_tags(tags)
    _resolve_category(actor, category_id)


def list_posts(
    actor: Actor,
    *,
    status: str | None = None,
    social_account_id: uuid.UUID | None = None,
    date_from: dt.datetime | None = None,
    date_to: dt.datetime | None = None,
    tag: str | None = None,
    search: str | None = None,
    limit: int = LIMIT_DEFAULT,
    cursor: str | None = None,
) -> PostsListResponse:
    if status is not None and status not in PlatformPost.Status.values:
        raise OpError(422, f"status must be one of {', '.join(PlatformPost.Status.values)}")
    if social_account_id is not None and social_account_id not in actor.allowed_account_ids:
        raise OpError(403, "social_account_id is not in this key's allowlist.")
    date_from, date_to = _aware(date_from), _aware(date_to)
    if date_from and date_to and date_to < date_from:
        raise OpError(422, "date_to must not be before date_from.")

    qs = visible_posts_qs(actor.api_key).prefetch_related(*_POST_PREFETCH)
    if status or social_account_id:
        # Status and account must hold on the SAME platform target.
        match = PlatformPost.objects.filter(post_id=OuterRef("pk"))
        if status:
            match = match.filter(status=status)
        if social_account_id:
            match = match.filter(social_account_id=social_account_id)
        qs = qs.filter(Exists(match))
    if date_from or date_to:
        # A post's date is when it goes out, or when it was created while it has no schedule yet.
        qs = qs.annotate(_when=Coalesce("scheduled_at", "created_at"))
        if date_from:
            qs = qs.filter(_when__gte=date_from)
        if date_to:
            qs = qs.filter(_when__lte=date_to)
    if tag:
        qs = qs.filter(tags__contains=[tag])
    if search and search.strip():
        term = search.strip()
        qs = qs.filter(Q(caption__icontains=term) | Q(title__icontains=term))
    rows, next_cursor = _paginate(qs.order_by("-created_at", "id"), limit=limit, cursor=cursor)
    return PostsListResponse(posts=[post_response(actor, p) for p in rows], limit=limit, next_cursor=next_cursor)


def check_recurrence(spec: RecurrenceSpec, *, workspace, scheduled_at: dt.datetime) -> None:
    """Refuse a repeat that can't start at ``scheduled_at``; run before anything is written."""
    import zoneinfo

    ws_tz = zoneinfo.ZoneInfo(workspace.effective_timezone or "UTC")
    try:
        calendar_services.clean_recurrence(**spec.rule_fields(), start=scheduled_at.astimezone(ws_tz))
    except ValueError as exc:
        raise OpError(422, str(exc)) from exc


def _post_settings_by_child(post: Post, settings: dict) -> dict:
    """The ``platform_extra`` each child gets from an update's ``post_settings``.

    Each child takes the keys its own platform declares, replaced as the
    composer replaces them; a key no child's platform declares is refused, and
    a scheduled child must stay publishable.
    """
    from providers import post_settings_error_for, post_settings_for

    result = {}
    used: set[str] = set()
    for pp in post.platform_posts.select_related("social_account"):
        platform = pp.social_account.platform
        declared = {f.key for f in post_settings_for(platform)}
        if not declared:
            continue
        mine = {key: value for key, value in settings.items() if key in declared}
        used.update(mine)
        try:
            cleaned = composer_services.clean_post_settings(platform, mine)
        except ValueError as exc:
            raise OpError(422, str(exc)) from exc
        platform_extra = composer_services.replace_post_settings(platform, pp.platform_extra, cleaned)
        if pp.status == "scheduled":
            reason = post_settings_error_for(platform, platform_extra)
            if reason:
                raise OpError(422, f"{pp.social_account.get_platform_display()}: {reason}")
        result[pp] = platform_extra
    unknown = sorted(set(settings) - used)
    if unknown:
        raise OpError(422, f"Unknown post setting(s) {unknown}: none of this post's accounts declare them.")
    return result


def update_post(actor: Actor, post_id: uuid.UUID, payload: UpdatePostRequest) -> PostResponse:
    """Edit a post that hasn't gone out. Omitted (null) fields are left as they are.

    Every reference is resolved and every input checked before anything is
    written; the writes then happen in one transaction, so a refusal never
    leaves a half-edited post.
    """
    actor.require("create_posts")  # the composer's create-or-edit permission
    post = _get_visible_post(actor, post_id)
    # Same rule as delete: someone else's post takes edit_others_posts.
    if post.author_id != getattr(actor.user, "id", None) and not actor.has("edit_others_posts"):
        raise OpError(403, "Permission denied: edit_others_posts")
    validate_post_taxonomy(actor, tags=payload.tags, category_id=payload.category_id)
    if not post.is_editable:
        raise OpError(409, f"Post is not editable in status {post.status}.")

    # Re-timing a scheduled post is publish-budget behaviour (a past time fires
    # it on the next poll), so it takes publish_directly like scheduling does.
    has_scheduled = post.platform_posts.filter(status="scheduled").exists()
    if payload.scheduled_at is not None and has_scheduled:
        actor.require("publish_directly")

    # Repeating hands each generated copy to the publisher too, so starting or
    # changing a series takes the same permission; stopping one doesn't.
    repeat = payload.recurrence if payload.recurrence and payload.recurrence.frequency != "never" else None
    if payload.recurrence is not None and post.recurrence_source_id is not None:
        raise OpError(
            422,
            f"This post is a copy in a repeating series; change the series on post {post.recurrence_source_id}.",
        )
    if repeat is not None:
        actor.require("publish_directly")
        first_at = payload.scheduled_at or post.scheduled_at
        if first_at is None or not has_scheduled:
            raise OpError(422, "Only a scheduled post can repeat; schedule it first.")
        check_recurrence(repeat, workspace=post.workspace, scheduled_at=first_at)

    wanted_media: list = []
    resolved_assets: dict = {}
    if payload.media_asset_ids is not None:
        wanted_media = list(payload.media_asset_ids)
        resolved_assets = {a.id: a for a in MediaAsset.objects.filter(id__in=wanted_media, workspace=post.workspace)}
        missing = [i for i in wanted_media if i not in resolved_assets]
        if missing:
            raise OpError(422, f"Media asset(s) not in workspace: {missing}")
        # A scheduled post may be edited, but not into one it can't publish.
        reason = composer_services.media_change_blocker(
            post,
            media_types_after=composer_services.usable_media_types(resolved_assets[mid] for mid in wanted_media),
            assets_after=[resolved_assets[mid] for mid in wanted_media],
        )
        if reason:
            raise OpError(422, reason)

    part_specs = None
    part_assets: dict = {}
    if payload.parts is not None:
        part_specs = [part.to_spec() for part in payload.parts]
        child_ids = {str(pp.social_account_id) for pp in post.platform_posts.all()}
        foreign = {acc for spec in part_specs for acc in spec.caption_overrides if acc not in child_ids}
        if foreign:
            raise OpError(422, f"parts[*].platform_overrides name accounts this post doesn't target: {sorted(foreign)}")
        try:
            part_assets = composer_services.resolve_thread_part_assets(post.workspace, part_specs)
        except ValueError as exc:
            raise OpError(422, str(exc)) from exc
    if payload.parts is not None or payload.first_comment is not None:
        # Nor into a thread its platform would publish only the first part of,
        # or a comments-mode thread that also carries a first comment.
        reason = composer_services.thread_change_blocker(
            post, part_specs, part_assets, first_comment=payload.first_comment
        )
        if reason:
            raise OpError(422, reason)

    settings_by_child: dict = {}
    if payload.post_settings is not None:
        settings_by_child = _post_settings_by_child(post, payload.post_settings)

    with transaction.atomic():
        update_fields: list[str] = []
        for pp, platform_extra in settings_by_child.items():
            pp.platform_extra = platform_extra
            pp.save(update_fields=["platform_extra", "updated_at"])
        for name in ("caption", "title", "first_comment", "internal_notes"):
            value = getattr(payload, name)
            if value is not None:
                setattr(post, name, value)
                update_fields.append(name)
        if payload.scheduled_at is not None:
            # Re-time any currently scheduled child; drafts are unaffected.
            # ``update()`` bypasses ``auto_now``, hence the explicit updated_at.
            post.platform_posts.filter(status="scheduled").update(
                scheduled_at=payload.scheduled_at, updated_at=timezone.now()
            )
            post.scheduled_at = payload.scheduled_at
            update_fields.append("scheduled_at")
        if payload.proposed_publish_at is not None:
            # Draft-stage suggestion; never reaches the publisher, so not gated on publish_directly.
            post.proposed_publish_at = payload.proposed_publish_at
            update_fields.append("proposed_publish_at")
        if payload.media_asset_ids is not None:
            post.media_attachments.all().delete()
            PostMedia.objects.bulk_create(
                [
                    PostMedia(post=post, media_asset=resolved_assets[mid], position=position)
                    for position, mid in enumerate(wanted_media)
                ]
            )
        if part_specs is not None:
            try:
                composer_services.set_post_parts(post, part_specs, asset_map=part_assets)
            except ValueError as exc:
                raise OpError(409, str(exc)) from exc
        if update_fields:
            post.save(update_fields=[*update_fields, "updated_at"])
        elif settings_by_child or part_specs is not None:
            # Settings and parts are the post's content too; touch it so
            # "modified since" readers see the change.
            post.save(update_fields=["updated_at"])

        composer_services.sync_post_scheduled_at(post)
        apply_post_taxonomy(actor, post, tags=payload.tags, category_id=payload.category_id)

        if repeat is not None:
            calendar_services.save_recurrence(post, **repeat.rule_fields(), refresh=payload.scheduled_at is not None)
        elif payload.recurrence is not None:
            calendar_services.stop_recurrence(post)

    return post_response(actor, _get_visible_post(actor, post_id))


def delete_post(actor: Actor, post_id: uuid.UUID) -> DeletedResponse:
    """Delete a post that hasn't gone out. Published (or publishing) posts are audit records and stay."""
    actor.require("create_posts")
    post = _get_visible_post(actor, post_id)
    if post.author_id != getattr(actor.user, "id", None) and not actor.has("edit_others_posts"):
        raise OpError(403, "Permission denied: edit_others_posts")
    if any(pp.status in PlatformPost.PROTECTED_STATUSES for pp in post.platform_posts.all()):
        raise OpError(409, "A post that is publishing or already published can't be deleted.")
    with transaction.atomic():
        # A series' first post takes its upcoming copies with it.
        calendar_services.stop_recurrence(post)
        post.delete()
    return DeletedResponse(id=post_id)


def duplicate_post(actor: Actor, post_id: uuid.UUID) -> PostResponse:
    """A fresh draft copy of a post (content, media, per-platform settings; no schedule)."""
    actor.require("create_posts")
    post = _get_visible_post(actor, post_id)
    copy = clone_post(post, author=actor.user)
    copy = Post.objects.prefetch_related(*_POST_PREFETCH).select_related("recurrence_rule").get(id=copy.id)
    return post_response(actor, copy)


# ---------------------------------------------------------------------------
# Approvals and comments
# ---------------------------------------------------------------------------


def list_approvals(
    actor: Actor,
    *,
    status: str | None = None,
    social_account_id: uuid.UUID | None = None,
    limit: int = LIMIT_DEFAULT,
    cursor: str | None = None,
) -> PostsListResponse:
    """Posts in the review workflow; by default those waiting on a reviewer or the client."""
    actor.require_any("approve_posts", "create_posts")
    if status is not None and status not in ("pending", "all", *APPROVAL_STATUSES):
        raise OpError(422, f"status must be 'pending', 'all' or one of {', '.join(APPROVAL_STATUSES)}")
    if social_account_id is not None and social_account_id not in actor.allowed_account_ids:
        raise OpError(403, "social_account_id is not in this key's allowlist.")
    statuses = APPROVAL_STATUSES if status == "all" else (status,) if status and status != "pending" else None
    match = PlatformPost.objects.filter(post_id=OuterRef("pk"), status__in=statuses or _PENDING_APPROVAL_STATUSES)
    if social_account_id:
        match = match.filter(social_account_id=social_account_id)
    qs = visible_posts_qs(actor.api_key).filter(Exists(match)).prefetch_related(*_POST_PREFETCH)
    rows, next_cursor = _paginate(qs.order_by("scheduled_at", "-created_at", "id"), limit=limit, cursor=cursor)
    return PostsListResponse(posts=[post_response(actor, p) for p in rows], limit=limit, next_cursor=next_cursor)


# decision -> (service function, whether a comment is required)
_DECISIONS = {
    "approve": (approval_services.approve_post, False),
    "request_changes": (approval_services.request_changes, True),
    "reject": (approval_services.reject_post, True),
}


def decide_approval(actor: Actor, post_id: uuid.UUID, decision: str, comment: str = "") -> PostResponse:
    """Approve a post, send it back for changes, or reject it — as the person behind this key."""
    actor.require("approve_posts")
    user = actor.require_user()
    service, comment_required = _DECISIONS[decision]
    if comment_required and not comment.strip():
        raise OpError(422, "A comment is required: the author is told why.")
    post = _get_visible_post(actor, post_id)
    member = actor.real_membership()
    if member is None or not member.effective_permissions.get("approve_posts", False):
        raise OpError(403, "Permission denied: approve_posts")
    try:
        result = service(post, user, actor.workspace, comment)
    except ValueError as exc:
        raise OpError(422, str(exc)) from exc
    if not result:
        raise OpError(409, "Nothing to update: no platform post of this post is waiting for your review.")
    return post_response(actor, _get_visible_post(actor, post_id))


def _visible_comments(actor: Actor, post: Post) -> list[PostComment]:
    return list(comment_service.get_comments_for_post(post, actor.require_user()))


def list_comments(actor: Actor, post_id: uuid.UUID) -> CommentsListResponse:
    """The post's comment threads. Client members see only ``external`` comments."""
    actor.require_any("create_posts", "approve_posts")
    post = _get_visible_post(actor, post_id, prefetch=False)
    return CommentsListResponse(
        comments=[CommentResponse.from_comment(c, replies=c.replies.all()) for c in _visible_comments(actor, post)]
    )


def add_comment(
    actor: Actor,
    post_id: uuid.UUID,
    *,
    body: str,
    visibility: str = "external",
    parent_id: uuid.UUID | None = None,
) -> CommentResponse:
    actor.require_any("create_posts", "approve_posts")
    user = actor.require_user()
    post = _get_visible_post(actor, post_id, prefetch=False)
    if not body.strip():
        raise OpError(422, "Comment body is required.")
    member = actor.real_membership()
    is_client = bool(member and member.workspace_role == WorkspaceMembership.WorkspaceRole.CLIENT)
    if is_client and visibility != PostComment.Visibility.EXTERNAL:
        raise OpError(403, "Clients can only write external comments.")
    if parent_id is not None:
        visible = comment_service.get_comments_for_post(post, user)
        parent = next((c for c in visible if c.id == parent_id), None)
        if parent is None:
            raise OpError(404, "Parent comment not found")
    comment = comment_service.create_comment(
        post=post, author=user, body=body.strip(), visibility=visibility, parent_id=parent_id
    )
    return CommentResponse.from_comment(comment)


# ---------------------------------------------------------------------------
# Queues and posting slots
# ---------------------------------------------------------------------------


def _visible_queues(actor: Actor) -> QuerySet[Queue]:
    return Queue.objects.filter(
        workspace_id=actor.workspace_id, social_account_id__in=actor.allowed_account_ids
    ).select_related("social_account")


def _get_queue(actor: Actor, queue_id: uuid.UUID) -> Queue:
    try:
        return _visible_queues(actor).get(id=queue_id)
    except Queue.DoesNotExist as exc:
        raise OpError(404, "Queue not found") from exc


def _next_free(queue: Queue) -> dt.datetime | None:
    slots = calendar_services.next_available_slots(queue.social_account, 1)
    return slots[0] if slots else None


def _queue_summary(queue: Queue, entry_count: int) -> QueueSummary:
    return QueueSummary(
        id=queue.id,
        name=queue.name,
        social_account_id=queue.social_account_id,
        category_id=queue.category_id,
        is_active=queue.is_active,
        entry_count=entry_count,
        next_free_slot=_next_free(queue),
    )


def list_queues(actor: Actor) -> QueuesListResponse:
    queues = _visible_queues(actor).annotate(_entries=Count("entries")).order_by("name", "id")
    return QueuesListResponse(queues=[_queue_summary(q, q._entries) for q in queues])


def get_queue(actor: Actor, queue_id: uuid.UUID) -> QueueDetailResponse:
    queue = _get_queue(actor, queue_id)
    entries = list(queue.entries.order_by("assigned_slot_datetime", "position", "id"))
    summary = _queue_summary(queue, len(entries))
    return QueueDetailResponse(**summary.model_dump(), entries=[QueueEntryResponse.from_entry(e) for e in entries])


def add_post_to_queue(
    actor: Actor, queue_id: uuid.UUID, post_id: uuid.UUID, priority: bool = False
) -> QueueEntryResponse:
    """Schedule a post into the queue's next free posting slot (or its first, with ``priority``)."""
    actor.require("create_posts")
    actor.require("publish_directly")
    queue = _get_queue(actor, queue_id)
    if not queue.is_active:
        raise OpError(422, "This queue is not active.")
    post = _get_visible_post(actor, post_id)
    pp = post.platform_posts.filter(social_account_id=queue.social_account_id).first()
    if pp is None:
        raise OpError(422, "The post has no platform target on this queue's account.")
    if pp.status not in ("draft", "approved", "scheduled"):
        raise OpError(409, f"A post in status {pp.status} can't be queued.")
    try:
        check_platform_quota(queue.social_account)
    except HttpError as exc:
        raise OpError(exc.status_code, exc.message) from exc
    try:
        with transaction.atomic():
            entry = calendar_services.add_to_queue(post, queue, priority=priority)
            pp.refresh_from_db()
            if pp.status != "scheduled":
                transition_platform_post(pp, "scheduled", scheduled_at=pp.scheduled_at)
    except calendar_services.QueueFullError as exc:
        raise OpError(409, "No open slot within the scheduling horizon.") from exc
    except ValueError as exc:
        raise OpError(422, str(exc)) from exc
    entry.refresh_from_db()
    return QueueEntryResponse.from_entry(entry)


def remove_queue_entry(actor: Actor, queue_id: uuid.UUID, entry_id: uuid.UUID) -> DeletedResponse:
    """Take a post out of the queue; it goes back to draft and its slot is left open."""
    actor.require("create_posts")
    queue = _get_queue(actor, queue_id)
    entry = queue.entries.select_related("post", "queue__social_account").filter(id=entry_id).first()
    if entry is None:
        raise OpError(404, "Queue entry not found")
    calendar_services.remove_from_queue(entry)
    return DeletedResponse(id=entry_id)


def _slot_response(slot: PostingSlot) -> PostingSlotResponse:
    return PostingSlotResponse(
        id=slot.id,
        social_account_id=slot.social_account_id,
        day_of_week=slot.day_of_week,
        day_name=slot.day_name,
        time=slot.time.strftime("%H:%M"),
        is_active=slot.is_active,
    )


def _get_slot(actor: Actor, slot_id: uuid.UUID) -> PostingSlot:
    try:
        return PostingSlot.objects.get(
            id=slot_id, social_account__workspace_id=actor.workspace_id, social_account_id__in=actor.allowed_account_ids
        )
    except PostingSlot.DoesNotExist as exc:
        raise OpError(404, "Posting slot not found") from exc


def list_slots(actor: Actor, social_account_id: uuid.UUID | None = None) -> PostingSlotsListResponse:
    if social_account_id is not None:
        _require_account(actor, social_account_id)
    slots = PostingSlot.objects.filter(
        social_account__workspace_id=actor.workspace_id, social_account_id__in=actor.allowed_account_ids
    )
    if social_account_id is not None:
        slots = slots.filter(social_account_id=social_account_id)
    return PostingSlotsListResponse(
        slots=[_slot_response(s) for s in slots.order_by("social_account_id", "day_of_week", "time")]
    )


def create_slot(actor: Actor, *, social_account_id: uuid.UUID, day_of_week: int, time: dt.time) -> PostingSlotResponse:
    actor.require("manage_social_accounts")
    account = _require_account(actor, social_account_id)
    time = time.replace(second=0, microsecond=0, tzinfo=None)
    slot, created = PostingSlot.objects.get_or_create(
        social_account=account, day_of_week=day_of_week, time=time, defaults={"is_active": True}
    )
    if not created:
        raise OpError(409, "A slot at that day and time already exists.")
    return _slot_response(slot)


def update_slot(
    actor: Actor, slot_id: uuid.UUID, *, time: dt.time | None = None, is_active: bool | None = None
) -> PostingSlotResponse:
    actor.require("manage_social_accounts")
    slot = _get_slot(actor, slot_id)
    fields: list[str] = []
    if time is not None:
        time = time.replace(second=0, microsecond=0, tzinfo=None)
        clash = PostingSlot.objects.filter(
            social_account=slot.social_account, day_of_week=slot.day_of_week, time=time
        ).exclude(id=slot.id)
        if clash.exists():
            raise OpError(409, "A slot at that time already exists.")
        slot.time = time
        fields.append("time")
    if is_active is not None:
        slot.is_active = is_active
        fields.append("is_active")
    if fields:
        slot.save(update_fields=[*fields, "updated_at"])
    return _slot_response(slot)


def delete_slot(actor: Actor, slot_id: uuid.UUID) -> DeletedResponse:
    actor.require("manage_social_accounts")
    slot = _get_slot(actor, slot_id)
    slot.delete()
    return DeletedResponse(id=slot_id)


def next_free_slots(actor: Actor, social_account_id: uuid.UUID, count: int = 1) -> NextFreeSlotsResponse:
    """The account's next open posting slots — times no scheduled post already holds."""
    if count < 1 or count > 20:
        raise OpError(422, "count must be between 1 and 20")
    account = _require_account(actor, social_account_id)
    return NextFreeSlotsResponse(
        social_account_id=account.id,
        timezone=account.workspace.effective_timezone or "UTC",
        slots=calendar_services.next_available_slots(account, count),
    )


# ---------------------------------------------------------------------------
# Ideas (kanban)
# ---------------------------------------------------------------------------


def _get_idea(actor: Actor, idea_id: uuid.UUID) -> Idea:
    try:
        return Idea.objects.for_workspace(actor.workspace_id).get(id=idea_id)
    except Idea.DoesNotExist as exc:
        raise OpError(404, "Idea not found") from exc


def _get_group(actor: Actor, group_id: uuid.UUID) -> IdeaGroup:
    try:
        return IdeaGroup.objects.for_workspace(actor.workspace_id).get(id=group_id)
    except IdeaGroup.DoesNotExist as exc:
        raise OpError(404, "Idea group not found") from exc


def list_ideas(
    actor: Actor,
    *,
    group_id: uuid.UUID | None = None,
    tag: str | None = None,
    search: str | None = None,
    limit: int = LIMIT_DEFAULT,
    cursor: str | None = None,
) -> IdeasListResponse:
    actor.require("create_posts")
    qs = Idea.objects.for_workspace(actor.workspace_id)
    if group_id is not None:
        qs = qs.filter(group_id=group_id)
    if tag:
        qs = qs.filter(tags__contains=[tag])
    if search and search.strip():
        qs = qs.filter(Q(title__icontains=search.strip()) | Q(description__icontains=search.strip()))
    rows, next_cursor = _paginate(qs.order_by("position", "-created_at", "id"), limit=limit, cursor=cursor)
    return IdeasListResponse(ideas=[IdeaResponse.from_idea(i) for i in rows], limit=limit, next_cursor=next_cursor)


def get_idea(actor: Actor, idea_id: uuid.UUID) -> IdeaResponse:
    actor.require("create_posts")
    return IdeaResponse.from_idea(_get_idea(actor, idea_id))


def create_idea(
    actor: Actor,
    *,
    title: str,
    description: str = "",
    tags: list[str] | None = None,
    group_id: uuid.UUID | None = None,
) -> IdeaResponse:
    actor.require("create_posts")
    title = title.strip()
    if not title:
        raise OpError(422, "title is required.")
    cleaned = _clean_tags(tags)
    if group_id is not None:
        group = _get_group(actor, group_id)
    else:
        group = IdeaGroup.objects.for_workspace(actor.workspace_id).order_by("position").first()
    with transaction.atomic():
        idea = Idea.objects.create(
            workspace=actor.workspace,
            author=actor.user,
            title=title,
            description=description.strip(),
            tags=cleaned,
            group=group,
            status=Idea.Status.UNASSIGNED,
        )
        _sync_tags(actor.workspace, cleaned)
    return IdeaResponse.from_idea(idea)


def update_idea(
    actor: Actor,
    idea_id: uuid.UUID,
    *,
    title: str | None = None,
    description: str | None = None,
    tags: list[str] | None = None,
    group_id: uuid.UUID | None = None,
    position: int | None = None,
) -> IdeaResponse:
    actor.require("create_posts")
    idea = _get_idea(actor, idea_id)
    if title is not None:
        if not title.strip():
            raise OpError(422, "title can't be empty.")
        idea.title = title.strip()
    if description is not None:
        idea.description = description.strip()
    cleaned = None
    if tags is not None:
        cleaned = _clean_tags(tags)
        idea.tags = cleaned
    if group_id is not None:
        idea.group = _get_group(actor, group_id)
    if position is not None:
        idea.position = position
    with transaction.atomic():
        idea.save()
        if cleaned:
            _sync_tags(actor.workspace, cleaned)
    return IdeaResponse.from_idea(idea)


def delete_idea(actor: Actor, idea_id: uuid.UUID) -> DeletedResponse:
    actor.require("create_posts")
    _get_idea(actor, idea_id).delete()
    return DeletedResponse(id=idea_id)


def list_idea_groups(actor: Actor) -> IdeaGroupsListResponse:
    actor.require("create_posts")
    groups = IdeaGroup.objects.for_workspace(actor.workspace_id).annotate(_n=Count("ideas")).order_by("position", "id")
    return IdeaGroupsListResponse(
        groups=[IdeaGroupResponse(id=g.id, name=g.name, position=g.position, idea_count=g._n) for g in groups]
    )


def create_idea_group(actor: Actor, *, name: str) -> IdeaGroupResponse:
    actor.require("create_posts")
    name = name.strip()
    if not name:
        raise OpError(422, "name is required.")
    top = (
        IdeaGroup.objects.for_workspace(actor.workspace_id)
        .order_by("-position")
        .values_list("position", flat=True)
        .first()
    )
    group = IdeaGroup.objects.create(workspace=actor.workspace, name=name, position=(top or 0) + 1)
    return IdeaGroupResponse(id=group.id, name=group.name, position=group.position, idea_count=0)


def delete_idea_group(actor: Actor, group_id: uuid.UUID) -> DeletedResponse:
    """Delete an empty kanban column."""
    actor.require("create_posts")
    group = _get_group(actor, group_id)
    if group.ideas.exists():
        raise OpError(409, "Column must be empty before deleting.")
    group.delete()
    return DeletedResponse(id=group_id)


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


def _uuid_list(values) -> list[uuid.UUID]:
    out = []
    for value in values or []:
        try:
            out.append(uuid.UUID(str(value)))
        except ValueError:
            continue
    return out


def _template_response(actor: Actor, template: PostTemplate) -> TemplateResponse:
    data = template.template_data or {}
    category_id = None
    if data.get("category_id"):
        parsed = _uuid_list([data["category_id"]])
        category_id = parsed[0] if parsed else None
    return TemplateResponse(
        id=template.id,
        name=template.name,
        description=template.description,
        caption=data.get("caption", ""),
        first_comment=data.get("first_comment", ""),
        category_id=category_id,
        tags=list(data.get("tags") or []),
        platform_account_ids=[
            a for a in _uuid_list(data.get("platform_account_ids")) if a in actor.allowed_account_ids
        ],
        media_asset_ids=_uuid_list(data.get("media_asset_ids")),
        created_at=template.created_at,
        updated_at=template.updated_at,
    )


def _visible_templates(actor: Actor) -> QuerySet[PostTemplate]:
    """Templates this key may see: the post rule, applied to a template's target accounts.

    A template aimed at any workspace account outside the allowlist is hidden,
    so a narrow key never reads (or rewrites) content meant for another
    account. Targets that no longer exist don't count against it.
    """
    foreign = [
        str(i)
        for i in SocialAccount.objects.for_workspace(actor.workspace_id)
        .exclude(id__in=actor.allowed_account_ids)
        .values_list("id", flat=True)
    ]
    qs = PostTemplate.objects.for_workspace(actor.workspace_id)
    if foreign:
        qs = qs.exclude(template_data__platform_account_ids__has_any_keys=foreign)
    return qs


def _get_template(actor: Actor, template_id: uuid.UUID) -> PostTemplate:
    try:
        return _visible_templates(actor).get(id=template_id)
    except PostTemplate.DoesNotExist as exc:
        raise OpError(404, "Template not found") from exc


def list_templates(
    actor: Actor, *, search: str | None = None, limit: int = LIMIT_DEFAULT, cursor: str | None = None
) -> TemplatesListResponse:
    qs = _visible_templates(actor)
    if search and search.strip():
        qs = qs.filter(Q(name__icontains=search.strip()) | Q(description__icontains=search.strip()))
    rows, next_cursor = _paginate(qs.order_by("name", "id"), limit=limit, cursor=cursor)
    return TemplatesListResponse(
        templates=[_template_response(actor, t) for t in rows], limit=limit, next_cursor=next_cursor
    )


def get_template(actor: Actor, template_id: uuid.UUID) -> TemplateResponse:
    return _template_response(actor, _get_template(actor, template_id))


def _check_template_refs(actor: Actor, *, category_id, account_ids, media_ids) -> None:
    _resolve_category(actor, category_id)
    foreign = [str(a) for a in account_ids if a not in actor.allowed_account_ids]
    if foreign:
        raise OpError(403, f"platform_account_ids not in this key's allowlist: {foreign}")
    _media_ids_in_workspace(actor, list(media_ids))


def create_template(
    actor: Actor,
    *,
    name: str,
    description: str = "",
    caption: str = "",
    first_comment: str = "",
    category_id: uuid.UUID | None = None,
    tags: list[str] | None = None,
    platform_account_ids: list[uuid.UUID] | None = None,
    media_asset_ids: list[uuid.UUID] | None = None,
) -> TemplateResponse:
    actor.require("create_posts")
    if not name.strip():
        raise OpError(422, "name is required.")
    cleaned = _clean_tags(tags)
    accounts = list(dict.fromkeys(platform_account_ids or []))
    media = list(dict.fromkeys(media_asset_ids or []))
    _check_template_refs(actor, category_id=category_id, account_ids=accounts, media_ids=media)
    template = PostTemplate.objects.create(
        workspace=actor.workspace,
        name=name.strip(),
        description=description.strip(),
        created_by=actor.user,
        template_data={
            "caption": caption,
            "first_comment": first_comment,
            "category_id": str(category_id) if category_id else None,
            "tags": cleaned,
            "platform_account_ids": [str(a) for a in accounts],
            "media_asset_ids": [str(m) for m in media],
        },
    )
    return _template_response(actor, template)


def update_template(
    actor: Actor,
    template_id: uuid.UUID,
    *,
    name: str | None = None,
    description: str | None = None,
    caption: str | None = None,
    first_comment: str | None = None,
    category_id: uuid.UUID | None = None,
    tags: list[str] | None = None,
    platform_account_ids: list[uuid.UUID] | None = None,
    media_asset_ids: list[uuid.UUID] | None = None,
) -> TemplateResponse:
    actor.require("create_posts")
    template = _get_template(actor, template_id)
    data = dict(template.template_data or {})
    if name is not None:
        if not name.strip():
            raise OpError(422, "name can't be empty.")
        template.name = name.strip()
    if description is not None:
        template.description = description.strip()
    accounts = list(dict.fromkeys(platform_account_ids)) if platform_account_ids is not None else None
    media = list(dict.fromkeys(media_asset_ids)) if media_asset_ids is not None else None
    _check_template_refs(actor, category_id=category_id, account_ids=accounts or [], media_ids=media or [])
    if caption is not None:
        data["caption"] = caption
    if first_comment is not None:
        data["first_comment"] = first_comment
    if category_id is not None:
        data["category_id"] = str(category_id)
    if tags is not None:
        data["tags"] = _clean_tags(tags)
    if accounts is not None:
        data["platform_account_ids"] = [str(a) for a in accounts]
    if media is not None:
        data["media_asset_ids"] = [str(m) for m in media]
    template.template_data = data
    template.save()
    return _template_response(actor, template)


def delete_template(actor: Actor, template_id: uuid.UUID) -> DeletedResponse:
    actor.require("create_posts")
    _get_template(actor, template_id).delete()
    return DeletedResponse(id=template_id)


# ---------------------------------------------------------------------------
# Tags and categories
# ---------------------------------------------------------------------------


def _get_tag(actor: Actor, tag_id: uuid.UUID) -> Tag:
    try:
        return Tag.objects.for_workspace(actor.workspace_id).get(id=tag_id)
    except Tag.DoesNotExist as exc:
        raise OpError(404, "Tag not found") from exc


def _tag_response(tag: Tag) -> TagResponse:
    return TagResponse(id=tag.id, name=tag.name, created_at=tag.created_at)


def list_tags(
    actor: Actor, *, search: str | None = None, limit: int = LIMIT_DEFAULT, cursor: str | None = None
) -> TagsListResponse:
    qs = Tag.objects.for_workspace(actor.workspace_id)
    if search and search.strip():
        qs = qs.filter(name__icontains=search.strip())
    rows, next_cursor = _paginate(qs.order_by("name", "id"), limit=limit, cursor=cursor)
    return TagsListResponse(tags=[_tag_response(t) for t in rows], limit=limit, next_cursor=next_cursor)


def create_tag(actor: Actor, *, name: str) -> TagResponse:
    """Create a tag (or return the existing one with that name)."""
    actor.require("create_posts")
    (cleaned,) = _clean_tags([name]) or [None]
    if cleaned is None:
        raise OpError(422, "name is required.")
    tag, _created = Tag.objects.get_or_create(workspace=actor.workspace, name=cleaned)
    return _tag_response(tag)


def _require_tag_in_scope(actor: Actor, name: str) -> None:
    """Refuse to rewrite a tag that sits on posts this key can't see.

    Renaming or deleting a tag edits every post carrying it; a key scoped to
    some accounts must not change posts for the others.
    """
    hidden = (
        Post.objects.filter(workspace_id=actor.workspace_id, tags__contains=[name])
        .exclude(id__in=visible_posts_qs(actor.api_key).values("id"))
        .exists()
    )
    if hidden:
        raise OpError(
            403, "This tag is used on posts for accounts outside this key's allowlist, so this key can't change it."
        )


def _retag(actor: Actor, old: str, new: str | None) -> None:
    """Rename (or, with ``new=None``, remove) ``old`` in the tag lists of posts and ideas."""
    now = timezone.now()
    for model in (Post, Idea):
        rows = list(model.objects.filter(workspace_id=actor.workspace_id, tags__contains=[old]))
        for row in rows:
            names = [new if n == old else n for n in row.tags if not (n == old and new is None)]
            row.tags = list(dict.fromkeys(names))
            row.updated_at = now
        model.objects.bulk_update(rows, ["tags", "updated_at"])


def rename_tag(actor: Actor, tag_id: uuid.UUID, *, name: str) -> TagResponse:
    """Rename a tag everywhere it is used."""
    actor.require("create_posts")
    tag = _get_tag(actor, tag_id)
    (cleaned,) = _clean_tags([name]) or [None]
    if cleaned is None:
        raise OpError(422, "name is required.")
    if cleaned != tag.name and Tag.objects.for_workspace(actor.workspace_id).filter(name=cleaned).exists():
        raise OpError(409, "A tag with that name already exists.")
    old = tag.name
    _require_tag_in_scope(actor, old)
    with transaction.atomic():
        tag.name = cleaned
        tag.save(update_fields=["name"])
        if old != cleaned:
            _retag(actor, old, cleaned)
    return _tag_response(tag)


def delete_tag(actor: Actor, tag_id: uuid.UUID) -> DeletedResponse:
    """Delete a tag and remove it from the posts and ideas that carry it."""
    actor.require("create_posts")
    tag = _get_tag(actor, tag_id)
    _require_tag_in_scope(actor, tag.name)
    with transaction.atomic():
        _retag(actor, tag.name, None)
        tag.delete()
    return DeletedResponse(id=tag_id)


def _category_response(category: ContentCategory) -> CategoryResponse:
    return CategoryResponse(id=category.id, name=category.name, color=category.color, position=category.position)


def _get_category(actor: Actor, category_id: uuid.UUID) -> ContentCategory:
    try:
        return ContentCategory.objects.for_workspace(actor.workspace_id).get(id=category_id)
    except ContentCategory.DoesNotExist as exc:
        raise OpError(404, "Category not found") from exc


def list_categories(actor: Actor) -> CategoriesListResponse:
    return CategoriesListResponse(
        categories=[_category_response(c) for c in ContentCategory.objects.for_workspace(actor.workspace_id)]
    )


def create_category(actor: Actor, *, name: str, color: str = "#3B82F6") -> CategoryResponse:
    actor.require("create_posts")
    name = name.strip()
    if not name:
        raise OpError(422, "name is required.")
    if not is_valid_hex_color(color):
        raise OpError(422, "color must be a 6-digit hex value like #3B82F6.")
    top = (
        ContentCategory.objects.for_workspace(actor.workspace_id)
        .order_by("-position")
        .values_list("position", flat=True)
        .first()
    )
    try:
        with transaction.atomic():
            category = ContentCategory.objects.create(
                workspace=actor.workspace, name=name, color=color, position=(top or 0) + 1
            )
    except IntegrityError as exc:
        raise OpError(409, "A category with that name already exists.") from exc
    return _category_response(category)


def update_category(
    actor: Actor,
    category_id: uuid.UUID,
    *,
    name: str | None = None,
    color: str | None = None,
    position: int | None = None,
) -> CategoryResponse:
    actor.require("create_posts")
    category = _get_category(actor, category_id)
    if name is not None:
        if not name.strip():
            raise OpError(422, "name can't be empty.")
        category.name = name.strip()
    if color is not None:
        if not is_valid_hex_color(color):
            raise OpError(422, "color must be a 6-digit hex value like #3B82F6.")
        category.color = color
    if position is not None:
        category.position = position
    try:
        with transaction.atomic():
            category.save()
    except IntegrityError as exc:
        raise OpError(409, "A category with that name already exists.") from exc
    return _category_response(category)


def delete_category(actor: Actor, category_id: uuid.UUID) -> DeletedResponse:
    """Delete a category; posts that used it are left uncategorised."""
    actor.require("create_posts")
    _get_category(actor, category_id).delete()
    return DeletedResponse(id=category_id)


# ---------------------------------------------------------------------------
# Calendar events
# ---------------------------------------------------------------------------


def _event_response(event: CustomCalendarEvent) -> CalendarEventResponse:
    return CalendarEventResponse(
        id=event.id,
        title=event.title,
        description=event.description,
        start_date=event.start_date,
        end_date=event.end_date,
        color=event.color,
    )


def list_events(
    actor: Actor,
    *,
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
    limit: int = LIMIT_DEFAULT,
    cursor: str | None = None,
) -> CalendarEventsListResponse:
    """Events overlapping ``date_from``…``date_to`` (inclusive), soonest first."""
    if date_from and date_to and date_to < date_from:
        raise OpError(422, "date_to must not be before date_from.")
    qs = CustomCalendarEvent.objects.for_workspace(actor.workspace_id)
    if date_from:
        qs = qs.filter(end_date__gte=date_from)
    if date_to:
        qs = qs.filter(start_date__lte=date_to)
    rows, next_cursor = _paginate(qs.order_by("start_date", "id"), limit=limit, cursor=cursor)
    return CalendarEventsListResponse(events=[_event_response(e) for e in rows], limit=limit, next_cursor=next_cursor)


def create_event(
    actor: Actor,
    *,
    title: str,
    start_date: dt.date,
    end_date: dt.date | None = None,
    description: str = "",
    color: str = "#3B82F6",
) -> CalendarEventResponse:
    actor.require("create_posts")
    title = title.strip()
    if not title:
        raise OpError(422, "title is required.")
    if not is_valid_hex_color(color):
        raise OpError(422, "color must be a 6-digit hex value like #3B82F6.")
    end_date = end_date or start_date
    if end_date < start_date:
        raise OpError(422, "end_date must not be before start_date.")
    event = CustomCalendarEvent.objects.create(
        workspace=actor.workspace,
        title=title,
        description=description.strip(),
        start_date=start_date,
        end_date=end_date,
        color=color,
        created_by=actor.user,
    )
    return _event_response(event)


def delete_event(actor: Actor, event_id: uuid.UUID) -> DeletedResponse:
    actor.require("create_posts")
    event = CustomCalendarEvent.objects.for_workspace(actor.workspace_id).filter(id=event_id).first()
    if event is None:
        raise OpError(404, "Event not found")
    event.delete()
    return DeletedResponse(id=event_id)


# ---------------------------------------------------------------------------
# Hashtag sets and signatures (read)
# ---------------------------------------------------------------------------


def list_hashtag_sets(actor: Actor) -> HashtagSetsListResponse:
    return HashtagSetsListResponse(
        hashtag_sets=[
            HashtagSetSummary(id=s.id, name=s.name, hashtags=s.hashtags)
            for s in HashtagSet.objects.for_workspace(actor.workspace_id)
        ]
    )


def list_signatures(actor: Actor) -> SignaturesListResponse:
    return SignaturesListResponse(
        signatures=[
            SignatureSummary(id=s.id, name=s.name, body=s.body, is_default=s.is_default)
            for s in Signature.objects.for_workspace(actor.workspace_id)
        ]
    )


# ---------------------------------------------------------------------------
# Members (read)
# ---------------------------------------------------------------------------


def list_members(actor: Actor) -> MembersListResponse:
    """The workspace's team, for assigning work and @mentioning in comments. No email addresses."""
    memberships = (
        WorkspaceMembership.objects.filter(workspace_id=actor.workspace_id, user__is_active=True)
        .select_related("user", "custom_role")
        .order_by("added_at", "id")
    )
    members = []
    for m in memberships:
        # The handle ``comments._notify_mentions`` resolves: the display name without spaces.
        handle = m.user.display_name.lower().replace(" ", "")
        members.append(
            MemberResponse(
                user_id=m.user_id,
                display_name=m.user.display_name,
                workspace_role=m.workspace_role,
                custom_role=m.custom_role.name if m.custom_role else None,
                mention=f"@{handle}" if comment_service.MENTION_REGEX.fullmatch(f"@{handle}") else None,
            )
        )
    return MembersListResponse(members=members)


# ---------------------------------------------------------------------------
# Brand voice and usage
# ---------------------------------------------------------------------------


def _brand_voice_response(workspace) -> BrandVoiceResponse:
    return BrandVoiceResponse(
        tone=workspace.brand_voice_tone,
        words_to_use=workspace.brand_voice_words_to_use,
        words_to_avoid=workspace.brand_voice_words_to_avoid,
        examples=[e for e in (workspace.brand_voice_examples or []) if isinstance(e, str)],
    )


def get_brand_voice(actor: Actor) -> BrandVoiceResponse:
    actor.require_any("create_posts", "manage_workspace_settings")
    return _brand_voice_response(actor.workspace)


def update_brand_voice(
    actor: Actor,
    *,
    tone: str | None = None,
    words_to_use: str | None = None,
    words_to_avoid: str | None = None,
    examples: list[str] | None = None,
) -> BrandVoiceResponse:
    """Change the voice every AI prompt from this workspace carries. Omitted fields are left as they are."""
    actor.require("manage_workspace_settings")
    workspace = actor.workspace
    fields = {
        "brand_voice_tone": tone,
        "brand_voice_words_to_use": words_to_use,
        "brand_voice_words_to_avoid": words_to_avoid,
    }
    changed: list[str] = []
    for name, value in fields.items():
        if value is None:
            continue
        value = value.strip()
        if len(value) > BRAND_VOICE_FIELD_MAX:
            raise OpError(422, f"Keep each brand voice field under {BRAND_VOICE_FIELD_MAX:,} characters.")
        setattr(workspace, name, value)
        changed.append(name)
    if examples is not None:
        cleaned = [e.strip() for e in examples if e.strip()]
        if len(cleaned) > BRAND_VOICE_MAX_EXAMPLES or any(len(e) > BRAND_VOICE_EXAMPLE_MAX for e in cleaned):
            raise OpError(
                422,
                f"Add up to {BRAND_VOICE_MAX_EXAMPLES} example posts of at most {BRAND_VOICE_EXAMPLE_MAX:,} characters each.",
            )
        workspace.brand_voice_examples = cleaned
        changed.append("brand_voice_examples")
    if changed:
        workspace.save(update_fields=[*changed, "updated_at"])
    return _brand_voice_response(workspace)


def get_usage(actor: Actor) -> UsageResponse:
    """The organization's plan, its limits and current usage (read-only)."""
    from apps.billing.entitlements import billing_enabled, get_entitlements, usage_summary
    from apps.members.models import OrgMembership

    org = actor.workspace.organization
    # The web billing page is for org admins and owners; the API matches it.
    is_admin = OrgMembership.objects.filter(
        organization=org,
        user_id=getattr(actor.user, "id", None),
        org_role__in=(OrgMembership.OrgRole.OWNER, OrgMembership.OrgRole.ADMIN),
    ).exists()
    if not is_admin:
        raise OpError(403, "Permission denied: plan usage is visible to organization owners and admins.")
    ent = get_entitlements(org)
    return UsageResponse(
        billing_enabled=billing_enabled(),
        plan_slug=ent.plan.slug if ent.plan else None,
        plan_name=ent.plan.name if ent.plan else None,
        features=sorted(ent.features),
        usage=[
            UsageMeterResponse(
                key=row["key"], label=row["label"], used=row["used"], limit=row["limit"], at_limit=row["at_limit"]
            )
            for row in usage_summary(org)
        ],
    )


# ---------------------------------------------------------------------------
# Best time to post
# ---------------------------------------------------------------------------

_DAY_NAMES = [label for _, label in PostingSlot.DayOfWeek.choices]


def _best_time_row(slot) -> BestTimeResponse:
    return BestTimeResponse(
        day_of_week=slot.weekday,
        day_name=_DAY_NAMES[slot.weekday],
        time=slot.time_label,
        score=slot.score,
        posts=slot.posts,
    )


def get_best_times(actor: Actor, social_account_id: uuid.UUID, count: int = 5) -> BestTimesResponse:
    """When the account's posts do best: its top times and the weekday x hour grid behind them."""
    if count < 1 or count > calendar_services.SUGGESTED_SLOTS_MAX:
        raise OpError(422, f"count must be between 1 and {calendar_services.SUGGESTED_SLOTS_MAX}")
    account = _require_account(actor, social_account_id)
    best, suggestions = calendar_services.suggest_slots(account, count)
    return BestTimesResponse(
        social_account_id=account.id,
        timezone=best.timezone,
        basis=best.basis,
        basis_label=best.basis_label,
        sample_size=best.sample_size,
        computed_at=best.computed_at,
        suggestions=[_best_time_row(s) for s in suggestions],
        grid=[[round(best.scores[d * 24 + h]) for h in range(24)] for d in range(7)],
    )


def apply_best_times(
    actor: Actor, *, social_account_id: uuid.UUID, count: int = 5, mode: str = "replace", preview: bool = False
) -> ApplyBestTimesResponse:
    """Make the account's top times its posting slots (``replace`` or ``add``); ``preview`` writes nothing."""
    actor.require("manage_social_accounts")
    if mode not in calendar_services.SLOT_MODES:
        raise OpError(422, f"mode must be one of {', '.join(calendar_services.SLOT_MODES)}")
    if count < 1 or count > calendar_services.SUGGESTED_SLOTS_MAX:
        raise OpError(422, f"count must be between 1 and {calendar_services.SUGGESTED_SLOTS_MAX}")
    account = _require_account(actor, social_account_id)
    best, suggestions = calendar_services.suggest_slots(account, count)
    if not suggestions:
        raise OpError(409, "There are no suggested times for this account yet.")
    to_add, to_remove, _kept = calendar_services.plan_slot_changes(account, suggestions, mode)
    if not preview:
        calendar_services.apply_suggested_slots(account, suggestions, mode)
    slots = PostingSlot.objects.filter(social_account=account).order_by("day_of_week", "time")
    return ApplyBestTimesResponse(
        social_account_id=account.id,
        mode=mode,
        applied=not preview,
        basis_label=best.basis_label,
        add=[f"{_DAY_NAMES[day][:3]} {at:%H:%M}" for day, at in to_add],
        remove=[f"{_DAY_NAMES[slot.day_of_week][:3]} {slot.time:%H:%M}" for slot in to_remove],
        slots=[_slot_response(slot) for slot in slots],
    )


# ---------------------------------------------------------------------------
# Evergreen recycling
# ---------------------------------------------------------------------------


def _visible_evergreen(actor: Actor) -> QuerySet[EvergreenItem]:
    return EvergreenItem.objects.filter(
        workspace_id=actor.workspace_id, social_account_id__in=actor.allowed_account_ids
    ).select_related("category")


def _get_evergreen(actor: Actor, item_id: uuid.UUID) -> EvergreenItem:
    try:
        return _visible_evergreen(actor).get(id=item_id)
    except EvergreenItem.DoesNotExist as exc:
        raise OpError(404, "Evergreen item not found") from exc


def _evergreen_category(actor: Actor, category_id: uuid.UUID | None, category_name: str | None):
    if category_name and category_name.strip():
        try:
            return evergreen_service.get_or_create_category(actor.workspace, category_name)
        except evergreen_service.EvergreenError as exc:
            raise OpError(422, str(exc)) from exc
    return _resolve_category(actor, category_id)


def _check_pre_approval(actor: Actor, pre_approved: bool | None) -> None:
    if pre_approved:
        actor.require("approve_posts")


def _evergreen_detail(item: EvergreenItem) -> EvergreenDetailResponse:
    repeats = list(item.repeats.select_related("platform_post", "item__social_account")[:100])
    performance = evergreen_service.repeat_performance(repeats)
    rows = []
    for repeat in repeats:
        stats = performance.get(repeat.id) or {}
        rows.append(
            EvergreenRepeatResponse(
                id=repeat.id,
                post_id=repeat.post_id,
                scheduled_for=repeat.scheduled_for,
                outcome=repeat.outcome,
                wording=repeat.wording,
                status=repeat.platform_post.status if repeat.platform_post else None,
                engagements=stats.get("engagements"),
                reach=stats.get("reach"),
                engagement_rate=stats.get("rate"),
            )
        )
    return EvergreenDetailResponse(**EvergreenItemResponse.from_item(item).model_dump(), repeats=rows)


def list_evergreen(
    actor: Actor,
    *,
    social_account_id: uuid.UUID | None = None,
    category_id: uuid.UUID | None = None,
    paused: bool | None = None,
    limit: int = LIMIT_DEFAULT,
    cursor: str | None = None,
) -> EvergreenListResponse:
    actor.require("create_posts")
    qs = _visible_evergreen(actor)
    if social_account_id is not None:
        _require_account(actor, social_account_id)
        qs = qs.filter(social_account_id=social_account_id)
    if category_id is not None:
        qs = qs.filter(category_id=category_id)
    if paused is not None:
        qs = qs.filter(is_paused=paused)
    rows, next_cursor = _paginate(
        qs.order_by("category__name", "last_posted_at", "created_at", "id"), limit=limit, cursor=cursor
    )
    return EvergreenListResponse(
        items=[EvergreenItemResponse.from_item(i) for i in rows], limit=limit, next_cursor=next_cursor
    )


def get_evergreen(actor: Actor, item_id: uuid.UUID) -> EvergreenDetailResponse:
    actor.require("create_posts")
    return _evergreen_detail(_get_evergreen(actor, item_id))


def add_evergreen(actor: Actor, payload: AddEvergreenRequest) -> EvergreenListResponse:
    """Add a published post (one item per channel), a template or an idea to the evergreen library."""
    actor.require("create_posts")
    _check_pre_approval(actor, payload.pre_approved)
    sources = [s for s in (payload.post_id, payload.template_id, payload.idea_id) if s is not None]
    if len(sources) != 1:
        raise OpError(422, "Give exactly one of post_id, template_id or idea_id.")
    category = _evergreen_category(actor, payload.category_id, payload.category_name)
    rules = {
        "interval_days": payload.interval_days,
        "max_repeats": payload.max_repeats,
        "vary_with_ai": payload.vary_with_ai,
        "pre_approved": payload.pre_approved,
    }
    user = actor.user
    items: list[EvergreenItem] = []
    try:
        with transaction.atomic():
            if payload.post_id is not None:
                post = _get_visible_post(actor, payload.post_id)
                targets = [
                    pp
                    for pp in post.platform_posts.select_related("social_account", "post")
                    if pp.status == PlatformPost.Status.PUBLISHED
                    and pp.social_account_id in actor.allowed_account_ids
                    and (payload.social_account_id is None or pp.social_account_id == payload.social_account_id)
                ]
                if not targets:
                    raise OpError(422, "The post has no published channel to recycle (check social_account_id).")
                for pp in targets:
                    items.append(evergreen_service.add_from_platform_post(pp, user=user, category=category, **rules))
            else:
                if payload.social_account_id is None:
                    raise OpError(422, "social_account_id is required for a template or an idea.")
                account = _require_account(actor, payload.social_account_id)
                if payload.template_id is not None:
                    template = _get_template(actor, payload.template_id)
                    items.append(
                        evergreen_service.add_from_template(template, account, user=user, category=category, **rules)
                    )
                else:
                    idea = _get_idea(actor, payload.idea_id)
                    items.append(evergreen_service.add_from_idea(idea, account, user=user, category=category, **rules))
    except evergreen_service.EvergreenError as exc:
        raise OpError(409 if "already" in str(exc) else 422, str(exc)) from exc
    return EvergreenListResponse(
        items=[EvergreenItemResponse.from_item(i) for i in items], limit=len(items), next_cursor=None
    )


def update_evergreen(actor: Actor, item_id: uuid.UUID, changes: dict[str, Any]) -> EvergreenItemResponse:
    """Change an item: pause or resume it, its category, interval, repeat limit, wording or pre-approval."""
    actor.require("create_posts")
    item = _get_evergreen(actor, item_id)
    _check_pre_approval(actor, changes.get("pre_approved"))
    fields = ["updated_at"]
    if "interval_days" in changes or "max_repeats" in changes or changes.get("clear_max_repeats"):
        max_repeats = None if changes.get("clear_max_repeats") else changes.get("max_repeats", item.max_repeats)
        try:
            item.interval_days, item.max_repeats = evergreen_service.clean_rules(
                changes.get("interval_days", item.interval_days), max_repeats
            )
        except evergreen_service.EvergreenError as exc:
            raise OpError(422, str(exc)) from exc
        fields += ["interval_days", "max_repeats"]
    if changes.get("category_id") is not None or changes.get("category_name"):
        item.category = _evergreen_category(actor, changes.get("category_id"), changes.get("category_name"))
        fields.append("category")
    if changes.get("vary_with_ai") is not None:
        item.vary_with_ai = changes["vary_with_ai"]
        fields.append("vary_with_ai")
    if changes.get("pre_approved") is not None and changes["pre_approved"] != item.pre_approved:
        item.pre_approved = changes["pre_approved"]
        item.pre_approved_by = actor.user if item.pre_approved else None
        fields += ["pre_approved", "pre_approved_by"]
    if changes.get("is_paused") is not None:
        item.is_paused = changes["is_paused"]
        fields.append("is_paused")
    item.save(update_fields=fields)
    return EvergreenItemResponse.from_item(item)


def remove_evergreen(actor: Actor, item_id: uuid.UUID) -> DeletedResponse:
    """Take an item out of the library. Posts already made from it stay."""
    actor.require("create_posts")
    _get_evergreen(actor, item_id).delete()
    return DeletedResponse(id=item_id)


# ---------------------------------------------------------------------------
# Client reports
# ---------------------------------------------------------------------------


def _visible_reports(actor: Actor) -> QuerySet:
    """Reports this key may see: the post rule, applied to the accounts a report covers.

    A report covering any workspace account outside the allowlist is hidden,
    since its figures and top posts would show that account. A report with no
    picked accounts covers the whole workspace, so only a key that covers every
    account sees it.
    """
    from apps.reports.models import ReportDefinition

    qs = ReportDefinition.objects.filter(workspace_id=actor.workspace_id).select_related("workspace__organization")
    foreign = SocialAccount.objects.for_workspace(actor.workspace_id).exclude(id__in=actor.allowed_account_ids)
    if foreign.exists():
        picked = ReportDefinition.social_accounts.through.objects.filter(reportdefinition_id=OuterRef("pk"))
        qs = qs.filter(Exists(picked)).exclude(Exists(picked.filter(socialaccount_id__in=foreign.values("id"))))
    return qs


def _require_reports(actor: Actor) -> None:
    from apps.billing.entitlements import require_feature
    from apps.billing.plans import Feature

    actor.require("view_analytics")
    require_feature(actor.workspace.organization, Feature.BRANDED_REPORTS)


def _get_report(actor: Actor, report_id: uuid.UUID):
    from apps.reports.models import ReportDefinition

    try:
        return _visible_reports(actor).get(id=report_id)
    except ReportDefinition.DoesNotExist as exc:
        raise OpError(404, "Report not found") from exc


def _report_run_response(run) -> ReportRunResponse:
    ready = run.is_ready and bool(run.data)
    return ReportRunResponse(
        id=run.id,
        report_id=run.definition_id,
        status=run.status,
        trigger=run.trigger,
        period_start=run.period_start,
        period_end=run.period_end,
        created_at=run.created_at,
        finished_at=run.finished_at,
        error=run.error if run.status == run.Status.FAILED else "",
        emailed_to=len(run.emailed_to),
        summary=(run.data.get("ai_summary") or {}).get("text") if ready else None,
        metrics=[
            {"key": k["key"], "label": k["label"], "value": k["display"], "delta": k["delta"]}
            for k in run.data.get("kpis", [])
        ]
        if ready
        else [],
    )


def _report_response(definition, run=None) -> ReportResponse:
    """``run`` is the definition's latest run, when the caller already has it."""
    if run is None:
        run = definition.runs.order_by("-created_at").first()
    return ReportResponse(
        id=definition.id,
        name=definition.name,
        range_type=definition.range_type,
        sections=list(definition.sections),
        recipients=len(definition.recipients),
        frequency=definition.frequency,
        is_active=definition.is_active,
        visible_to_clients=definition.visible_to_clients,
        white_label=definition.white_label,
        latest_run=_report_run_response(run) if run else None,
    )


def list_reports(actor: Actor, *, limit: int = LIMIT_DEFAULT, cursor: str | None = None) -> ReportsListResponse:
    _require_reports(actor)
    rows, next_cursor = _paginate(_visible_reports(actor).order_by("name", "id"), limit=limit, cursor=cursor)
    from apps.reports.models import ReportRun

    # One query for every row's latest run, as the web list does.
    latest = {
        run.definition_id: run
        for run in ReportRun.objects.filter(definition__in=rows)
        .order_by("definition_id", "-created_at")
        .distinct("definition_id")
    }
    return ReportsListResponse(
        reports=[_report_response(d, latest.get(d.id)) for d in rows], limit=limit, next_cursor=next_cursor
    )


def run_report(actor: Actor, report_id: uuid.UUID, *, deliver: bool = False) -> ReportRunResponse:
    """Queue a build now (the web app's "Run now"); a worker builds it, so the run comes back pending."""
    from apps.reports import services as report_services

    _require_reports(actor)
    actor.require("manage_social_accounts")
    definition = _get_report(actor, report_id)
    try:
        run = report_services.queue_manual_run(definition, actor.user, deliver=deliver)
    except report_services.RunLimitError as exc:
        raise OpError(429, str(exc)) from exc
    return _report_run_response(run)


def get_latest_report_run(actor: Actor, report_id: uuid.UUID) -> ReportRunResponse:
    _require_reports(actor)
    run = _get_report(actor, report_id).runs.order_by("-created_at").first()
    if run is None:
        raise OpError(404, "This report hasn't been run yet")
    return _report_run_response(run)
