"""Concrete MCP tool implementations.

Every tool delegates to the same service-layer functions the REST API
uses — ``apps.composer.services.create_post`` for writes, the same
allowlist + permission checks, the same platform quota — so there's no
MCP-only code path that can drift from REST validation.

Tool result envelope mirrors the spec: a list of ``content`` blocks
plus an ``isError`` flag. We serialize structured results as
``{type: "text", text: "<json>"}`` because Claude clients render JSON
in text blocks more reliably than the experimental ``json`` content
type, and agents can always ``JSON.parse`` it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from django.conf import settings
from ninja.errors import HttpError

from apps.analytics.api_builders import (
    build_account_analytics,
    build_post_analytics,
    build_workspace_overview,
    connected_accounts,
)
from apps.api import ops
from apps.api.limits import check_platform_quota
from apps.api.pagination import decode_offset_cursor, encode_offset_cursor
from apps.api.schemas import (
    PostResponse,
    RecurrenceSpec,
    ThreadPart,
    WebhookDeliveryResponse,
    WebhookEndpointResponse,
    WebhookEndpointWithSecretResponse,
)
from apps.calendar.services import clean_recurrence, save_recurrence
from apps.composer.models import PlatformPost, Post
from apps.composer.services import create_post, transition_platform_post
from apps.inbox.models import InboxMessage, InboxReply
from apps.inbox.services import (
    ReplyStateError,
    create_reply_draft,
    discard_reply_draft,
    send_reply_now,
    update_reply_draft,
)
from apps.mcp.protocol import INVALID_PARAMS, JsonRpcError
from apps.mcp.tools import Tool, register_tool
from apps.social_accounts.models import SocialAccount
from apps.webhooks import services as webhook_services
from apps.webhooks.models import SUBSCRIBABLE_EVENT_TYPES, WebhookDelivery, WebhookEndpoint
from apps.webhooks.models import DeliveryStatus as WebhookDeliveryStatus
from apps.webhooks.services import WebhookError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wrap_text(payload: Any) -> dict:
    """Return MCP's text-content envelope around a JSON-serializable value.

    Most Claude clients render text blocks reliably; the experimental
    ``json`` content type isn't universally supported yet. Agents can
    always ``JSON.parse`` the returned text.
    """
    return {
        "content": [{"type": "text", "text": json.dumps(payload, default=str)}],
        "isError": False,
    }


# Optional argument of the create-like tools; see ``_idempotent``.
IDEMPOTENCY_KEY_INPUT_PROPERTY = {
    "idempotency_key": {
        "type": "string",
        "minLength": 1,
        "maxLength": 128,
        "description": (
            "Optional. Reuse the same value when retrying this call after a timeout: the first result is "
            "returned again instead of creating a duplicate (kept 24 h; API-key connections only)."
        ),
    }
}


def _idempotent(name: str, handler):
    """Wrap a create-like tool handler so a retried ``idempotency_key`` replays the first result.

    The MCP twin of REST's ``Idempotency-Key`` header, stored in the same
    ``IdempotencyRecord`` table under the calling key with a ``tools/call``
    fingerprint, so a key reused with different arguments is refused. The
    work and the stored result commit together; a refusal releases the claim.
    OAuth callers have no ``ApiKey`` row to key the record on, so for them the
    argument is accepted and ignored.
    """
    from django.db import transaction

    from apps.api.middleware import (
        claim_idempotency_slot,
        finalize_idempotent_response,
        fingerprint_request,
        release_idempotent_claim,
    )

    def wrapped(args: dict, context: dict[str, Any]) -> dict:
        key = args.get("idempotency_key")
        args = {k: v for k, v in args.items() if k != "idempotency_key"}
        api_key = context["api_key"]
        if not key or getattr(api_key, "is_oauth", False):
            return handler(args, context)
        try:
            disposition, _status, replay = claim_idempotency_slot(
                api_key=api_key, idempotency_key=key, fingerprint=fingerprint_request("MCP", name, args)
            )
        except ValueError as exc:
            raise JsonRpcError(INVALID_PARAMS, str(exc)) from exc
        if disposition == "replay":
            return replay
        if disposition == "in_flight":
            raise JsonRpcError(INVALID_PARAMS, "A call with this idempotency_key is still in flight; retry shortly.")
        try:
            with transaction.atomic():
                result = handler(args, context)
                finalize_idempotent_response(api_key=api_key, idempotency_key=key, status_code=200, body=result)
        except Exception:
            release_idempotent_claim(api_key=api_key, idempotency_key=key)
            raise
        return result

    return wrapped


def _run_op(op, context: dict[str, Any], *args: Any, **kwargs: Any):
    """Run a shared ``apps.api.ops`` operation as the calling actor, mapping a refusal onto a JSON-RPC error.

    Returns the operation's response schema dumped to JSON-safe primitives, so
    the result is byte-identical to the REST body.
    """
    try:
        result = op(ops.Actor.from_context(context), *args, **kwargs)
    except ops.OpError as exc:
        raise JsonRpcError(INVALID_PARAMS, exc.message) from exc
    return result.model_dump(mode="json")


def _require_perm(context: dict[str, Any], permission_key: str) -> None:
    """Re-check a workspace permission inside a tool handler.

    Mirrors REST's ``_require_perm`` so MCP can't be used to bypass
    permissions that the REST surface enforces.
    """
    membership = context["membership"]
    if not membership.effective_permissions.get(permission_key, False):
        raise JsonRpcError(INVALID_PARAMS, f"Permission denied: {permission_key}")


def _parse_uuid(value: Any, field_name: str) -> UUID:
    if not isinstance(value, str):
        raise JsonRpcError(INVALID_PARAMS, f"{field_name} must be a string UUID")
    try:
        return UUID(value)
    except (TypeError, ValueError) as exc:
        raise JsonRpcError(INVALID_PARAMS, f"{field_name} is not a valid UUID") from exc


def _resolve_allowed_account(api_key, social_account_id_str: str) -> SocialAccount:
    sa_id = _parse_uuid(social_account_id_str, "social_account_id")
    allowed = {sa.id for sa in api_key.social_accounts.all()}
    if sa_id not in allowed:
        raise JsonRpcError(INVALID_PARAMS, "social_account_id is not in this API key's allowlist")
    return SocialAccount.objects.get(id=sa_id)


def _resolve_media_folder(workspace, args: dict):
    """Resolve an optional ``folder_id`` arg to a MediaFolder in the workspace's org.

    Shared by the upload tools so they scope folders identically.
    """
    folder_id_raw = args.get("folder_id")
    if not folder_id_raw:
        return None
    from apps.media_library.models import MediaFolder

    try:
        return MediaFolder.objects.get(
            id=_parse_uuid(folder_id_raw, "folder_id"),
            organization=workspace.organization,
        )
    except MediaFolder.DoesNotExist as exc:
        raise JsonRpcError(INVALID_PARAMS, "folder_id not found in this organization") from exc


def _parse_media_tags(args: dict) -> list:
    """Validate an optional ``tags`` arg as a list of strings."""
    tags = args.get("tags") or []
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        raise JsonRpcError(INVALID_PARAMS, "tags must be a list of strings")
    return tags


def _can_view_internal_notes(context: dict[str, Any]) -> bool:
    """Whether this MCP caller may see a post's team-only ``internal_notes``.

    Mirrors the REST router's check: visibility is gated on ``create_posts``,
    the permission held by exactly the workspace roles the composer lets view
    internal notes (i.e. not client/viewer). ``get_post`` isn't permission-
    gated, so without this an OAuth client/viewer could read team notes.
    """
    membership = context.get("membership")
    return bool(membership and membership.effective_permissions.get("create_posts", False))


def _serialize_post(post: Post, context: dict[str, Any]) -> dict:
    """Serialize a Post for an MCP tool response.

    Delegates to the same Pydantic schema the REST router returns so
    the two surfaces cannot drift in either field set or wire format.
    ``internal_notes`` is redacted unless the caller may view it (see
    ``_can_view_internal_notes``).
    """
    return PostResponse.from_post(post, include_internal_notes=_can_view_internal_notes(context)).model_dump(
        mode="json"
    )


def _visible_posts_qs(api_key):
    """Posts this API key may see (see ``apps.api.ops.visible_posts_qs``)."""
    return ops.visible_posts_qs(api_key)


def _get_post_for_key(api_key, post_id_str: str) -> Post:
    """Allowlist-respecting Post fetch shared by ``get_post`` / ``cancel_post``.

    Out-of-scope and nonexistent both surface as "Post not found" — the API
    never reveals which is which.
    """
    post_id = _parse_uuid(post_id_str, "post_id")
    try:
        return (
            _visible_posts_qs(api_key)
            .prefetch_related(
                "platform_posts__social_account",
                "platform_posts__part_states__part",
                "thread_parts__media_attachments",
            )
            .get(id=post_id)
        )
    except Post.DoesNotExist as exc:
        raise JsonRpcError(INVALID_PARAMS, "Post not found") from exc


def _parse_iso_datetime(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise JsonRpcError(INVALID_PARAMS, f"{field_name} must be a string")
    try:
        # ``fromisoformat`` accepts trailing 'Z' starting in Python 3.11.
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise JsonRpcError(INVALID_PARAMS, f"{field_name} must be ISO 8601") from exc
    # Interpret a tz-less value as UTC (the documented contract) so it lands on
    # the USE_TZ model as an aware instant — otherwise Django stores it naive
    # (RuntimeWarning) and the workspace-tz list views re-localize it wrongly.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


# ---------------------------------------------------------------------------
# Tool: list_accounts
# ---------------------------------------------------------------------------


# The ``post_settings`` argument of the creating tools: the per-post settings an
# account's platform lists in list_accounts (``post_settings``).
_POST_SETTINGS_INPUT_SCHEMA = {
    "type": "object",
    "description": (
        "Per-post settings the account's platform declares in list_accounts' post_settings "
        "(for example a Reddit subreddit and flair_id). Unknown keys are rejected."
    ),
    "additionalProperties": True,
}

# The ``parts`` argument of the post-creating tools — ThreadPart's fields.
_PARTS_INPUT_SCHEMA = {
    "type": "array",
    "description": (
        "Optional later parts of a thread, in order; caption and media_asset_ids above are its first part. "
        "Published per the account's thread_mode (see list_accounts): reply_chain, comments, or none. "
        "Scheduling is refused when the account can't publish the thread."
    ),
    "items": {
        "type": "object",
        "properties": {
            "caption": {"type": "string", "default": "", "maxLength": 10000},
            "media_asset_ids": {
                "type": "array",
                "items": {"type": "string", "format": "uuid"},
                "default": [],
                "description": "Only on accounts with thread_media_per_part.",
            },
            "delay_seconds": {
                "type": "integer",
                "minimum": 0,
                "maximum": 86400,
                "default": 0,
                "description": "Seconds to wait after the previous part is live.",
            },
            "is_finisher": {
                "type": "boolean",
                "default": False,
                "description": "A closing part, always published last. At most one.",
            },
        },
        "additionalProperties": False,
    },
}


def _parse_parts(args: dict) -> list:
    """The validated ``parts`` argument as composer ``ThreadPartSpec``s."""
    from pydantic import ValidationError

    raw = args.get("parts") or []
    if not isinstance(raw, list):
        raise JsonRpcError(INVALID_PARAMS, "parts must be an array")
    try:
        return [ThreadPart.model_validate(item).to_spec() for item in raw]
    except ValidationError as exc:
        raise JsonRpcError(INVALID_PARAMS, f"parts is invalid: {exc.errors()[0]['msg']}") from exc


def _list_accounts(args: dict, context: dict[str, Any]) -> dict:
    api_key = context["api_key"]
    # Reuse the REST schema so MCP and REST stay byte-identical (Gap 4 + 5).
    from apps.api.schemas import AccountSummary

    accounts = [AccountSummary.from_social_account(sa).model_dump(mode="json") for sa in api_key.social_accounts.all()]
    return _wrap_text({"accounts": accounts})


register_tool(
    Tool(
        name="list_accounts",
        description=(
            "List the social media accounts this API key is allowed to act on. "
            "Returns id, platform, account_name, account_handle, connection_status, char_limit, "
            "escaped_chars, needs_title, supports_first_comment, requires_video, requires_media, and the "
            "thread capability (thread_mode, thread_max_parts, thread_part_max_length, thread_media_per_part). "
            "Call this first to discover which "
            "social_account_id values are valid and what each platform requires."
        ),
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=_list_accounts,
        read_only=True,
    )
)


# ---------------------------------------------------------------------------
# Tool: create_draft
# ---------------------------------------------------------------------------


_TAXONOMY_INPUT_PROPERTIES = {
    "tags": {
        "type": "array",
        "items": {"type": "string", "maxLength": 100},
        "maxItems": 25,
        "description": "Tag names for the post (new names are added to the workspace's tags; see list_tags).",
    },
    "category_id": {
        "type": "string",
        "format": "uuid",
        "description": "Content category for the post (see list_categories).",
    },
}


def _post_taxonomy(args: dict, context: dict[str, Any]) -> tuple[list[str] | None, UUID | None]:
    """The validated ``tags`` / ``category_id`` arguments, checked before anything is written."""
    tags = args.get("tags")
    category_id = _parse_uuid(args["category_id"], "category_id") if args.get("category_id") else None
    try:
        ops.validate_post_taxonomy(ops.Actor.from_context(context), tags=tags, category_id=category_id)
    except ops.OpError as exc:
        raise JsonRpcError(INVALID_PARAMS, exc.message) from exc
    return tags, category_id


def _apply_post_taxonomy(post: Post, tags: list[str] | None, category_id: UUID | None, context: dict[str, Any]) -> None:
    try:
        ops.apply_post_taxonomy(ops.Actor.from_context(context), post, tags=tags, category_id=category_id)
    except ops.OpError as exc:
        raise JsonRpcError(INVALID_PARAMS, exc.message) from exc


def _create_draft(args: dict, context: dict[str, Any]) -> dict:
    _require_perm(context, "create_posts")
    api_key = context["api_key"]
    if "social_account_id" not in args:
        raise JsonRpcError(INVALID_PARAMS, "social_account_id is required")
    if "caption" not in args:
        raise JsonRpcError(INVALID_PARAMS, "caption is required")
    sa = _resolve_allowed_account(api_key, args["social_account_id"])
    parts = _parse_parts(args)
    tags, category_id = _post_taxonomy(args, context)
    proposed_publish_at = None
    if args.get("proposed_publish_at") is not None:
        proposed_publish_at = _parse_iso_datetime(args["proposed_publish_at"], "proposed_publish_at")
    try:
        post = create_post(
            workspace=api_key.workspace,
            social_account=sa,
            caption=args["caption"],
            title=args.get("title", ""),
            first_comment=args.get("first_comment", ""),
            internal_notes=args.get("internal_notes", ""),
            media_asset_ids=args.get("media_asset_ids") or [],
            proposed_publish_at=proposed_publish_at,
            author=api_key.issued_by if api_key.issued_by_id else None,
            status="draft",
            parts=parts,
            post_settings=args.get("post_settings"),
        )
    except ValueError as exc:
        raise JsonRpcError(INVALID_PARAMS, str(exc)) from exc
    _apply_post_taxonomy(post, tags, category_id, context)
    return _wrap_text(_serialize_post(post, context))


register_tool(
    Tool(
        name="create_draft",
        description=(
            "Create a draft post against a connected account. The draft is saved but not "
            "queued for publishing; call schedule_draft with the returned post id later. "
            "Optionally record a non-binding proposed_publish_at suggestion."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "social_account_id": {
                    "type": "string",
                    "format": "uuid",
                    "description": "ID of a SocialAccount in this key's allowlist (see list_accounts).",
                },
                "caption": {"type": "string", "maxLength": 10000},
                "title": {"type": "string", "default": "", "maxLength": 255},
                "first_comment": {
                    "type": "string",
                    "default": "",
                    "description": "Optional comment auto-posted after the main post.",
                },
                "internal_notes": {
                    "type": "string",
                    "default": "",
                    "maxLength": 10000,
                    "description": "Private team-only note. Never published to any platform.",
                },
                "media_asset_ids": {
                    "type": "array",
                    "items": {"type": "string", "format": "uuid"},
                    "default": [],
                    "description": "MediaAsset UUIDs already uploaded to the workspace's media library.",
                },
                "proposed_publish_at": {
                    "type": "string",
                    "description": (
                        "Optional ISO-8601 UTC suggested publish time (e.g. 2026-06-01T14:00:00Z). "
                        "A non-binding draft hint shown in the drafts/approval views — stored as-is, "
                        "not validated against the future, and never queued for publishing."
                    ),
                },
                "parts": _PARTS_INPUT_SCHEMA,
                "post_settings": _POST_SETTINGS_INPUT_SCHEMA,
                **_TAXONOMY_INPUT_PROPERTIES,
                **IDEMPOTENCY_KEY_INPUT_PROPERTY,
            },
            "required": ["social_account_id", "caption"],
            "additionalProperties": False,
        },
        handler=_idempotent("create_draft", _create_draft),
    )
)


# ---------------------------------------------------------------------------
# Tool: schedule_post — create + queue for publishing in one step
# ---------------------------------------------------------------------------


# The ``recurrence`` argument of the scheduling tools — RecurrenceSpec's fields.
_RECURRENCE_INPUT_SCHEMA = {
    "type": "object",
    "description": (
        "Repeat the post, like the composer's Repeat control. Copies are generated up to 90 days "
        "ahead at the same local time (workspace timezone)."
    ),
    "properties": {
        "frequency": {"type": "string", "enum": ["never", "daily", "weekly", "monthly"]},
        "interval": {"type": "integer", "minimum": 1, "maximum": 99, "default": 1},
        "weekdays": {
            "type": "array",
            "items": {"type": "integer", "minimum": 0, "maximum": 6},
            "description": "Weekly only: 0=Monday … 6=Sunday. Defaults to the first post's weekday.",
        },
        "end_date": {
            "type": "string",
            "format": "date",
            "description": "Last date a copy may fall on (YYYY-MM-DD). Can't be combined with count.",
        },
        "count": {
            "type": "integer",
            "minimum": 2,
            "description": "Total posts in the series, the first included. Can't be combined with end_date.",
        },
    },
    "required": ["frequency"],
    "additionalProperties": False,
}


def _parse_recurrence(args: dict, *, workspace, scheduled_at: datetime) -> RecurrenceSpec | None:
    """The validated ``recurrence`` argument, or None when it asks for no repeat.

    Checked before anything is written, like the REST routes, so a bad repeat
    never leaves a post scheduled without it.
    """
    import zoneinfo

    from pydantic import ValidationError

    raw = args.get("recurrence")
    if not raw:
        return None
    try:
        spec = RecurrenceSpec.model_validate(raw)
    except ValidationError as exc:
        raise JsonRpcError(INVALID_PARAMS, f"recurrence is invalid: {exc.errors()[0]['msg']}") from exc
    if spec.frequency == "never":
        return None
    ws_tz = zoneinfo.ZoneInfo(workspace.effective_timezone or "UTC")
    try:
        clean_recurrence(**spec.rule_fields(), start=scheduled_at.astimezone(ws_tz))
    except ValueError as exc:
        raise JsonRpcError(INVALID_PARAMS, str(exc)) from exc
    return spec


def _schedule_post(args: dict, context: dict[str, Any]) -> dict:
    # Mirrors the REST contract: scheduling sends the post into the
    # publisher's poll loop, which the composer permission model gates
    # on ``publish_directly`` (see apps/composer/views.py:797). Tools/
    # call to ``schedule_post`` requires the same.
    _require_perm(context, "create_posts")
    _require_perm(context, "publish_directly")
    api_key = context["api_key"]
    if "social_account_id" not in args:
        raise JsonRpcError(INVALID_PARAMS, "social_account_id is required")
    if "caption" not in args:
        raise JsonRpcError(INVALID_PARAMS, "caption is required")
    if "scheduled_at" not in args:
        raise JsonRpcError(INVALID_PARAMS, "scheduled_at is required (ISO 8601)")
    scheduled_at = _parse_iso_datetime(args["scheduled_at"], "scheduled_at")
    sa = _resolve_allowed_account(api_key, args["social_account_id"])
    repeat = _parse_recurrence(args, workspace=api_key.workspace, scheduled_at=scheduled_at)
    parts = _parse_parts(args)
    tags, category_id = _post_taxonomy(args, context)
    # Platform quota is shared with REST; ``check_platform_quota``
    # raises ``HttpError(429,...)`` which we re-shape into a JSON-RPC
    # error so MCP clients see structured feedback rather than HTTP.
    try:
        check_platform_quota(sa)
    except HttpError as exc:
        raise JsonRpcError(
            INVALID_PARAMS,
            f"Per-platform daily quota reached for {sa.platform}: {exc.message}",
        ) from exc
    from django.db import transaction

    try:
        with transaction.atomic():
            post = create_post(
                workspace=api_key.workspace,
                social_account=sa,
                caption=args["caption"],
                title=args.get("title", ""),
                first_comment=args.get("first_comment", ""),
                internal_notes=args.get("internal_notes", ""),
                media_asset_ids=args.get("media_asset_ids") or [],
                scheduled_at=scheduled_at,
                author=api_key.issued_by if api_key.issued_by_id else None,
                status="scheduled",
                parts=parts,
                post_settings=args.get("post_settings"),
            )
            _apply_post_taxonomy(post, tags, category_id, context)
            if repeat is not None:
                save_recurrence(post, **repeat.rule_fields())
    except ValueError as exc:
        raise JsonRpcError(INVALID_PARAMS, str(exc)) from exc
    return _wrap_text(_serialize_post(post, context))


register_tool(
    Tool(
        name="schedule_post",
        description=(
            "Create a post and schedule it to publish at a specific UTC timestamp. "
            "The publisher polls every ~15s and will fire the post once the time elapses. "
            "Refused when the post can't publish: accounts with requires_video need a video in "
            "media_asset_ids, those with requires_media need at least one media item, and "
            "Pinterest needs a board, which only the composer can set (use create_draft for Pinterest). "
            "Also refused when the media breaks a platform media rule (aspect ratio, resolution, length, "
            "size or item count). Pass recurrence to repeat the post."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "social_account_id": {"type": "string", "format": "uuid"},
                "caption": {"type": "string", "maxLength": 10000},
                "scheduled_at": {
                    "type": "string",
                    "description": "ISO 8601 UTC timestamp (e.g. 2026-06-01T14:00:00Z)",
                },
                "title": {"type": "string", "default": "", "maxLength": 255},
                "first_comment": {"type": "string", "default": ""},
                "internal_notes": {
                    "type": "string",
                    "default": "",
                    "maxLength": 10000,
                    "description": "Private team-only note. Never published to any platform.",
                },
                "media_asset_ids": {
                    "type": "array",
                    "items": {"type": "string", "format": "uuid"},
                    "default": [],
                },
                "recurrence": _RECURRENCE_INPUT_SCHEMA,
                "parts": _PARTS_INPUT_SCHEMA,
                "post_settings": _POST_SETTINGS_INPUT_SCHEMA,
                **_TAXONOMY_INPUT_PROPERTIES,
                **IDEMPOTENCY_KEY_INPUT_PROPERTY,
            },
            "required": ["social_account_id", "caption", "scheduled_at"],
            "additionalProperties": False,
        },
        handler=_idempotent("schedule_post", _schedule_post),
    )
)


# ---------------------------------------------------------------------------
# Tool: get_post
# ---------------------------------------------------------------------------


def _get_post(args: dict, context: dict[str, Any]) -> dict:
    if "post_id" not in args:
        raise JsonRpcError(INVALID_PARAMS, "post_id is required")
    api_key = context["api_key"]
    post = _get_post_for_key(api_key, args["post_id"])
    return _wrap_text(_serialize_post(post, context))


register_tool(
    Tool(
        name="get_post",
        description=(
            "Retrieve a post by ID, including aggregate status and per-platform child state. "
            "Returns 'Post not found' for posts outside the API key's allowlist (same as for "
            "truly nonexistent IDs — the API never reveals which is which)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "post_id": {"type": "string", "format": "uuid"},
                "confirm_unconfirmed": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Required true when the stopped part is 'unconfirmed': you checked the platform and "
                        "it isn't posted. Without it, resuming an unconfirmed part is refused."
                    ),
                },
            },
            "required": ["post_id"],
            "additionalProperties": False,
        },
        handler=_get_post,
        read_only=True,
    )
)


# ---------------------------------------------------------------------------
# Tool: list_posts
# ---------------------------------------------------------------------------


_MCP_POST_LIMIT_DEFAULT = 50
_MCP_POST_LIMIT_MAX = 100


def _list_posts(args: dict, context: dict[str, Any]) -> dict:
    raw_limit = args.get("limit")
    result = _run_op(
        ops.list_posts,
        context,
        status=args.get("status"),
        social_account_id=_parse_uuid(args["social_account_id"], "social_account_id")
        if args.get("social_account_id")
        else None,
        date_from=_parse_iso_datetime(args["date_from"], "date_from") if args.get("date_from") else None,
        date_to=_parse_iso_datetime(args["date_to"], "date_to") if args.get("date_to") else None,
        tag=args.get("tag"),
        search=args.get("search"),
        # ``or`` would swallow an explicit 0 as "unset" and hand back the default.
        limit=_MCP_POST_LIMIT_DEFAULT if raw_limit is None else raw_limit,
        cursor=args.get("cursor"),
    )
    return _wrap_text(result)


register_tool(
    Tool(
        name="list_posts",
        description=(
            "List and search posts in this API key's workspace, newest first (by creation time). Fills the gap "
            "left by get_post (which needs an id you may not have yet). Each item has the same shape "
            "as get_post (aggregate status + per-platform child state, tags, category). Only posts whose every "
            "platform target is in the key's allowlist are returned. Filters, all optional and combinable: "
            "`status` (a post matches if any target has it), `social_account_id` (must be in the allowlist; "
            "holds on the same target as status), `date_from`/`date_to` (ISO 8601; a post's date is when it "
            "goes out, or when it was created while it has no schedule), `tag` (exact tag name) and `search` "
            "(matches caption or title). `limit` defaults to 50, max 100. When more posts remain, `next_cursor` "
            "is non-null — pass it back as `cursor` for the next page; a null `next_cursor` means you have them all."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": list(PlatformPost.Status.values),
                    "description": "Optional per-platform status; a post matches if any target has it.",
                },
                "social_account_id": {"type": "string", "format": "uuid"},
                "date_from": {"type": "string", "description": "ISO 8601 lower bound (UTC if no offset)."},
                "date_to": {"type": "string", "description": "ISO 8601 upper bound (UTC if no offset)."},
                "tag": {"type": "string"},
                "search": {"type": "string", "description": "Matches the caption or title."},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _MCP_POST_LIMIT_MAX,
                    "default": _MCP_POST_LIMIT_DEFAULT,
                },
                "cursor": {
                    "type": "string",
                    "description": "Opaque cursor from a previous call's `next_cursor`.",
                },
            },
            "additionalProperties": False,
        },
        handler=_list_posts,
        read_only=True,
    )
)


# ---------------------------------------------------------------------------
# Tool: cancel_post
# ---------------------------------------------------------------------------


def _cancel_post(args: dict, context: dict[str, Any]) -> dict:
    from django.db import transaction

    _require_perm(context, "create_posts")
    if "post_id" not in args:
        raise JsonRpcError(INVALID_PARAMS, "post_id is required")
    api_key = context["api_key"]
    post = _get_post_for_key(api_key, args["post_id"])
    scheduled = [pp for pp in post.platform_posts.all() if pp.status == "scheduled"]
    if not scheduled:
        raise JsonRpcError(INVALID_PARAMS, "No scheduled platform posts to cancel")
    # Wrap the per-child loop in a single outer atomic so a mid-loop
    # ValueError (concurrent admin transition, state-machine rejection
    # on a later child) rolls back any earlier ``draft`` commits.
    # Mirrors the REST ``/cancel`` route's atomic block — without this,
    # a multi-account post could end up in a mixed draft/scheduled state
    # that neither the publisher nor the agent expects. Codex PR #53
    # flagged this asymmetry between REST and MCP.
    with transaction.atomic():
        for pp in scheduled:
            try:
                transition_platform_post(pp, "draft")
            except ValueError as exc:
                raise JsonRpcError(INVALID_PARAMS, str(exc)) from exc
    post.refresh_from_db()
    return _wrap_text(_serialize_post(post, context))


register_tool(
    Tool(
        name="cancel_post",
        description=(
            "Cancel a scheduled post, transitioning it back to draft. "
            "No-op error if there are no scheduled children to cancel."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "post_id": {"type": "string", "format": "uuid"},
                "confirm_unconfirmed": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Required true when the stopped part is 'unconfirmed': you checked the platform and "
                        "it isn't posted. Without it, resuming an unconfirmed part is refused."
                    ),
                },
            },
            "required": ["post_id"],
            "additionalProperties": False,
        },
        handler=_cancel_post,
    )
)


# ---------------------------------------------------------------------------
# Tool: schedule_draft — REST-parity transition of an existing draft post
# ---------------------------------------------------------------------------


def _schedule_draft(args: dict, context: dict[str, Any]) -> dict:
    """Promote draft or finally approved children of an existing post to ``scheduled``.

    Mirrors the REST ``POST /api/v1/posts/{post_id}/schedule`` route.
    Closes the asymmetry where MCP previously had no way to transition
    an existing draft to scheduled — ``schedule_post`` always creates a
    NEW post in scheduled state. Without this tool, "draft now, schedule
    later" via pure MCP forced clients to recreate the post or fall back
    to REST for the one transition.
    """
    from django.db import transaction

    _require_perm(context, "create_posts")
    # Same permission contract as the REST route: pushing a post into
    # the publisher's poll loop requires ``publish_directly``.
    _require_perm(context, "publish_directly")
    if "post_id" not in args:
        raise JsonRpcError(INVALID_PARAMS, "post_id is required")
    if "scheduled_at" not in args:
        raise JsonRpcError(INVALID_PARAMS, "scheduled_at is required (ISO 8601)")
    scheduled_at = _parse_iso_datetime(args["scheduled_at"], "scheduled_at")

    api_key = context["api_key"]
    post = _get_post_for_key(api_key, args["post_id"])
    schedulable = [pp for pp in post.platform_posts.all() if pp.status in {"draft", "approved"}]
    if not schedulable:
        raise JsonRpcError(INVALID_PARAMS, "No draft or approved platform posts to schedule")
    repeat = _parse_recurrence(args, workspace=post.workspace, scheduled_at=scheduled_at)
    if repeat is not None and post.recurrence_source_id is not None:
        raise JsonRpcError(
            INVALID_PARAMS,
            f"This post is a copy in a repeating series; change the series on post {post.recurrence_source_id}.",
        )

    # Per-platform 24h quota check, one per child, BEFORE we mutate
    # anything — over-quota fails the whole call with no partial commit.
    for pp in schedulable:
        try:
            check_platform_quota(pp.social_account)
        except HttpError as exc:
            raise JsonRpcError(
                INVALID_PARAMS,
                f"Per-platform daily quota reached for {pp.social_account.platform}: {exc.message}",
            ) from exc

    # Wrap the per-child loop in a single outer atomic — same reasoning
    # as ``cancel_post``: a mid-loop ValueError (concurrent admin
    # transition, state-machine rejection on a later child, workspace
    # approval-mode rejection from ``transition_platform_post``) rolls
    # back any earlier ``scheduled`` commits.
    with transaction.atomic():
        for pp in schedulable:
            try:
                transition_platform_post(pp, "scheduled", scheduled_at=scheduled_at)
            except ValueError as exc:
                raise JsonRpcError(INVALID_PARAMS, str(exc)) from exc
        if repeat is not None:
            post.refresh_from_db()
            save_recurrence(post, **repeat.rule_fields(), refresh=True)
    post.refresh_from_db()
    return _wrap_text(_serialize_post(post, context))


register_tool(
    Tool(
        name="schedule_draft",
        description=(
            "Schedule an EXISTING draft or approved post — transitions eligible children to scheduled "
            "at the given UTC timestamp. Use this for the two-step flow "
            "'create_draft now, schedule_draft later'. For one-shot create-and-schedule, "
            "use schedule_post instead. Approval-required workspaces must complete human approval first. "
            "Requires both create_posts and publish_directly. "
            "Refused when the post can't publish: accounts with requires_video need a video "
            "attached, those with requires_media need at least one media item, a Pinterest "
            "draft needs a board chosen in the composer, and the media must keep within each "
            "platform's media rules. Pass recurrence to repeat the post."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "post_id": {"type": "string", "format": "uuid"},
                "scheduled_at": {
                    "type": "string",
                    "description": "ISO 8601 UTC timestamp (e.g. 2026-06-01T14:00:00Z)",
                },
                "recurrence": _RECURRENCE_INPUT_SCHEMA,
            },
            "required": ["post_id", "scheduled_at"],
            "additionalProperties": False,
        },
        handler=_schedule_draft,
    )
)


# ---------------------------------------------------------------------------
# Tool: resume_thread — REST parity with POST /posts/{id}/resume-thread
# ---------------------------------------------------------------------------


def _resume_thread(args: dict, context: dict[str, Any]) -> dict:
    """Pick a partially published thread back up from the part where it stopped."""
    from apps.publisher.engine import resume_thread

    _require_perm(context, "create_posts")
    _require_perm(context, "publish_directly")
    if "post_id" not in args:
        raise JsonRpcError(INVALID_PARAMS, "post_id is required")
    api_key = context["api_key"]
    post = _get_post_for_key(api_key, args["post_id"])
    stopped = [pp for pp in post.platform_posts.all() if pp.is_thread_partial]
    if not stopped:
        raise JsonRpcError(INVALID_PARAMS, "No thread on this post is stopped part-way")
    for pp in stopped:
        try:
            resume_thread(pp, confirm_unconfirmed=args.get("confirm_unconfirmed") is True)
        except ValueError as exc:
            raise JsonRpcError(INVALID_PARAMS, str(exc)) from exc
    return _wrap_text(_serialize_post(_get_post_for_key(api_key, args["post_id"]), context))


register_tool(
    Tool(
        name="resume_thread",
        description=(
            "Resume a thread that stopped part-way (a platform post whose thread_status is 'partial'). "
            "Parts already published are left alone; publishing restarts at the part that failed. "
            "If that part's status is 'unconfirmed' the platform may already have published it: check the "
            "platform first, then pass confirm_unconfirmed=true to send it again. "
            "Requires both create_posts and publish_directly."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "post_id": {"type": "string", "format": "uuid"},
                "confirm_unconfirmed": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Required true when the stopped part is 'unconfirmed': you checked the platform and "
                        "it isn't posted. Without it, resuming an unconfirmed part is refused."
                    ),
                },
            },
            "required": ["post_id"],
            "additionalProperties": False,
        },
        handler=_resume_thread,
    )
)


# ---------------------------------------------------------------------------
# Media tools: search_media, get_media, upload_media (Gap 1 + 1b)
# ---------------------------------------------------------------------------


_MCP_MEDIA_LIMIT_DEFAULT = 20
_MCP_MEDIA_LIMIT_MAX = 100
# JSON-RPC payload sanity cap. Kept below Django's DATA_UPLOAD_MAX_MEMORY_SIZE
# default of 2.5 MB so this check fires with a structured JSON-RPC error
# before Django's RequestDataTooBig fires with an opaque HTML 500.
# For anything larger, agents must use POST /api/v1/media/ over REST.
_MCP_UPLOAD_MAX_BYTES = 1024 * 1024  # 1 MB raw


def _visible_media_qs(api_key):
    from apps.media_library.models import MediaAsset

    workspace = api_key.workspace
    return MediaAsset.objects.for_workspace_with_shared(
        workspace_id=workspace.id,
        organization_id=workspace.organization_id,
    )


def _serialize_media(asset) -> dict:
    """Return the same shape as ``GET /api/v1/media/{id}``."""
    from apps.api.schemas import MediaAssetResponse

    return MediaAssetResponse.from_asset(asset, last_used_at=getattr(asset, "last_used_at", None)).model_dump(
        mode="json"
    )


def _search_media(args: dict, context: dict[str, Any]) -> dict:
    from apps.media_library.models import MediaAsset

    api_key = context["api_key"]
    query = args.get("query") or None
    media_type = args.get("media_type") or None
    tags = args.get("tags") or []
    folder_id_raw = args.get("folder_id") or None
    is_starred = args.get("is_starred")
    limit = int(args.get("limit") or _MCP_MEDIA_LIMIT_DEFAULT)
    if limit < 1 or limit > _MCP_MEDIA_LIMIT_MAX:
        raise JsonRpcError(INVALID_PARAMS, f"limit must be between 1 and {_MCP_MEDIA_LIMIT_MAX}")

    qs = MediaAsset.objects.with_last_used_at(_visible_media_qs(api_key))
    # Default to ``completed`` so agents never reference half-processed
    # assets via MCP. Mirrors the REST default.
    qs = qs.filter(processing_status="completed")
    if media_type:
        qs = qs.filter(media_type=media_type)
    if folder_id_raw:
        qs = qs.filter(folder_id=_parse_uuid(folder_id_raw, "folder_id"))
    if is_starred is not None:
        qs = qs.filter(is_starred=bool(is_starred))
    if isinstance(tags, list):
        for tag in tags:
            if not isinstance(tag, str):
                raise JsonRpcError(INVALID_PARAMS, "tags must be a list of strings")
            qs = qs.filter(tags__contains=[tag])
    if query:
        qs = MediaAsset.objects.search(query, queryset=qs)

    qs = qs.order_by("-created_at", "id")[:limit]
    return _wrap_text({"items": [_serialize_media(a) for a in qs]})


register_tool(
    Tool(
        name="search_media",
        description=(
            "Find media assets already uploaded to this workspace. Defaults to the 20 most "
            "recent assets that are ready to reference. Use this before uploading to avoid "
            "duplicating evergreen content. Returns the same item shape as GET /api/v1/media/."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Optional substring match on filename and tags.",
                },
                "media_type": {
                    "type": "string",
                    "enum": ["image", "video", "gif", "document"],
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "All tags must match (AND semantics).",
                },
                "folder_id": {"type": "string", "format": "uuid"},
                "is_starred": {"type": "boolean"},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _MCP_MEDIA_LIMIT_MAX,
                    "default": _MCP_MEDIA_LIMIT_DEFAULT,
                },
            },
            "additionalProperties": False,
        },
        handler=_search_media,
        read_only=True,
    )
)


def _get_media(args: dict, context: dict[str, Any]) -> dict:
    from apps.media_library.models import MediaAsset

    if "media_id" not in args:
        raise JsonRpcError(INVALID_PARAMS, "media_id is required")
    media_id = _parse_uuid(args["media_id"], "media_id")
    api_key = context["api_key"]
    qs = MediaAsset.objects.with_last_used_at(_visible_media_qs(api_key))
    try:
        asset = qs.get(id=media_id)
    except MediaAsset.DoesNotExist as exc:
        raise JsonRpcError(INVALID_PARAMS, "Media asset not found") from exc
    return _wrap_text(_serialize_media(asset))


register_tool(
    Tool(
        name="get_media",
        description=(
            "Retrieve a single media asset by id. Same response shape as "
            "GET /api/v1/media/{id}. Use this to poll an upload's processing_status "
            "until it transitions from 'pending' to 'completed'."
        ),
        input_schema={
            "type": "object",
            "properties": {"media_id": {"type": "string", "format": "uuid"}},
            "required": ["media_id"],
            "additionalProperties": False,
        },
        handler=_get_media,
        read_only=True,
    )
)


def _upload_media(args: dict, context: dict[str, Any]) -> dict:
    """MCP-side upload accepts base64 content (≤5 MB).

    For larger files agents must use ``POST /api/v1/media/`` over REST —
    multipart can't ride a JSON-RPC envelope cleanly.
    """
    import base64
    import binascii

    from django.core.exceptions import ValidationError
    from django.core.files.uploadedfile import SimpleUploadedFile

    from apps.media_library.quotas import StorageQuotaExceededError
    from apps.media_library.services import create_asset as media_create_asset

    _require_perm(context, "upload_media")
    if "filename" not in args:
        raise JsonRpcError(INVALID_PARAMS, "filename is required")
    if "content_base64" not in args:
        raise JsonRpcError(INVALID_PARAMS, "content_base64 is required")

    filename = args["filename"]
    if not isinstance(filename, str) or not filename.strip():
        raise JsonRpcError(INVALID_PARAMS, "filename must be a non-empty string")

    try:
        raw = base64.b64decode(args["content_base64"], validate=True)
    except (ValueError, binascii.Error) as exc:
        raise JsonRpcError(INVALID_PARAMS, "content_base64 is not valid base64") from exc

    if len(raw) > _MCP_UPLOAD_MAX_BYTES:
        raise JsonRpcError(
            INVALID_PARAMS,
            (
                f"MCP upload limit is {_MCP_UPLOAD_MAX_BYTES // 1024 // 1024} MB. "
                "Use POST /api/v1/media/ (multipart) for larger files."
            ),
        )

    content_type = args.get("content_type") or "application/octet-stream"
    uploaded = SimpleUploadedFile(name=filename, content=raw, content_type=content_type)

    api_key = context["api_key"]
    workspace = api_key.workspace
    folder = _resolve_media_folder(workspace, args)
    tags = _parse_media_tags(args)

    try:
        asset = media_create_asset(
            organization=workspace.organization,
            workspace=workspace,
            uploaded_file=uploaded,
            uploaded_by=api_key.issued_by if api_key.issued_by_id else None,
            folder=folder,
            alt_text=args.get("alt_text", "") or "",
            title=args.get("title", "") or "",
            tags=tags,
        )
    except StorageQuotaExceededError as exc:
        raise JsonRpcError(
            INVALID_PARAMS,
            f"Storage quota exceeded: used={exc.used} limit={exc.limit} attempted={exc.attempted}",
        ) from exc
    except ValidationError as exc:
        raise JsonRpcError(INVALID_PARAMS, "; ".join(getattr(exc, "messages", [str(exc)]))) from exc

    from apps.media_library.tasks import process_media_asset

    process_media_asset(str(asset.id))

    return _wrap_text(_serialize_media(asset))


register_tool(
    Tool(
        name="upload_media",
        description=(
            "Upload a small media file (≤1 MB raw / ~1.3 MB base64) via base64. "
            "For anything larger use POST /api/v1/media/ over REST instead — multipart "
            "can't ride a JSON-RPC envelope. Returns the same shape as the REST upload "
            "response; processing_status starts at 'pending' until the background task "
            "transitions it to 'completed'."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "filename": {"type": "string", "maxLength": 255},
                "content_base64": {
                    "type": "string",
                    "description": "Base64-encoded file content. Decoded size must be ≤5 MB.",
                },
                "content_type": {"type": "string"},
                "alt_text": {"type": "string", "maxLength": 2000},
                "title": {"type": "string", "maxLength": 255},
                "folder_id": {"type": "string", "format": "uuid"},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["filename", "content_base64"],
            "additionalProperties": False,
        },
        handler=_upload_media,
    )
)


# ---------------------------------------------------------------------------
# Tools: request_media_upload / finalize_media_upload (presigned direct-to-R2)
# ---------------------------------------------------------------------------
#
# Large media (videos especially) can't ride a base64 JSON-RPC envelope, so the
# base64 upload_media above caps at 1 MB. These two tools let an OAuth caller
# upload large files entirely over MCP — no REST API key needed: request a
# presigned POST, upload the bytes straight to object storage, then finalize so
# the server validates the stored object and registers the asset. Because upload
# and the later create_draft both ride the same OAuth connection, they resolve
# the same workspace — the media is always found.

_MCP_PRESIGN_LOCAL_MODE_MSG = (
    "Presigned upload is not available on this deployment's storage. Use "
    "upload_media (base64, ≤1 MB) or POST /api/v1/media/ (multipart) instead."
)


def _request_media_upload(args: dict, context: dict[str, Any]) -> dict:
    from apps.media_library.services import create_pending_upload
    from apps.media_library.storage import supports_presigned_post
    from apps.media_library.validators import ALL_ALLOWED_MIMES

    _require_perm(context, "upload_media")
    # Capability, not just "is it S3": R2 answers presigned POST with 501, and
    # a URL the bucket will reject is worse than a clear refusal here.
    if not supports_presigned_post():
        raise JsonRpcError(INVALID_PARAMS, _MCP_PRESIGN_LOCAL_MODE_MSG)

    filename = args.get("filename")
    if not isinstance(filename, str) or not filename.strip():
        raise JsonRpcError(INVALID_PARAMS, "filename must be a non-empty string")
    # media_type is constrained to the enum by the input schema. content_type is
    # pinned into the presigned POST policy and becomes the stored object's
    # Content-Type, so constrain it to the upload allowlist (or octet-stream):
    # gives a clear up-front error instead of an opaque storage rejection, and
    # stops a caller from pinning a renderable type (e.g. text/html).
    media_type = args["media_type"]
    content_type = args.get("content_type") or "application/octet-stream"
    if content_type != "application/octet-stream" and content_type not in ALL_ALLOWED_MIMES:
        raise JsonRpcError(
            INVALID_PARAMS,
            "content_type must be one of " + ", ".join(sorted(ALL_ALLOWED_MIMES)) + " (or omitted).",
        )

    api_key = context["api_key"]
    workspace = api_key.workspace
    pending, presigned = create_pending_upload(
        organization=workspace.organization,
        workspace=workspace,
        created_by=api_key.issued_by if api_key.issued_by_id else None,
        declared_filename=filename,
        content_type=content_type,
        requested_media_type=media_type,
    )
    return _wrap_text(
        {
            "upload_id": str(pending.id),
            "method": presigned["method"],
            "url": presigned["url"],
            "fields": presigned["fields"],
            "max_bytes": pending.max_bytes,
            "expires_at": pending.expires_at.isoformat(),
            "instructions": (
                "Upload the raw bytes to 'url' as a multipart/form-data POST: send every "
                "key/value in 'fields' as form fields, then a final 'file' field holding the "
                "binary body. Then call finalize_media_upload with this upload_id."
            ),
        }
    )


def _finalize_media_upload(args: dict, context: dict[str, Any]) -> dict:
    from django.core.exceptions import ValidationError
    from django.db import transaction
    from django.utils import timezone

    from apps.media_library.models import MediaAsset, PendingUpload
    from apps.media_library.quotas import StorageQuotaExceededError
    from apps.media_library.services import inspect_uploaded_object, register_uploaded_asset
    from apps.media_library.storage import supports_presigned_post
    from apps.media_library.tasks import process_media_asset

    _require_perm(context, "upload_media")
    if not supports_presigned_post():
        raise JsonRpcError(INVALID_PARAMS, _MCP_PRESIGN_LOCAL_MODE_MSG)

    upload_id = _parse_uuid(args.get("upload_id"), "upload_id")
    api_key = context["api_key"]
    workspace = api_key.workspace

    # Validate caller args up front — no lock, no remote I/O.
    folder = _resolve_media_folder(workspace, args)
    tags = _parse_media_tags(args)

    # Tenant-scoped fetch (no lock) for the fast replay path and to read the key.
    try:
        pending = PendingUpload.objects.get(id=upload_id, workspace_id=workspace.id)
    except PendingUpload.DoesNotExist as exc:
        raise JsonRpcError(INVALID_PARAMS, "Upload not found") from exc

    if pending.finalized_at:
        # Idempotent replay — return the existing asset, never mint a second.
        if pending.media_asset_id is None:
            # Finalized earlier but the asset was since deleted; don't re-create.
            raise JsonRpcError(INVALID_PARAMS, "This upload was already finalized; its media asset no longer exists.")
        asset = pending.media_asset
    else:
        if pending.expires_at < timezone.now():
            raise JsonRpcError(INVALID_PARAMS, "This upload request has expired; request a new one.")
        # Inspect the stored object (HEAD + range-GET) OUTSIDE the row lock so the
        # lock never spans remote round-trips.
        try:
            inspected = inspect_uploaded_object(pending)
        except FileNotFoundError as exc:
            raise JsonRpcError(INVALID_PARAMS, str(exc)) from exc
        except StorageQuotaExceededError as exc:
            raise JsonRpcError(
                INVALID_PARAMS,
                f"Storage quota exceeded: used={exc.used} limit={exc.limit} attempted={exc.attempted}",
            ) from exc
        except ValidationError as exc:
            raise JsonRpcError(INVALID_PARAMS, "; ".join(getattr(exc, "messages", [str(exc)]))) from exc

        # Create + mark finalized under a short row lock, re-checking idempotency
        # so a finalize that won the race while we inspected still wins.
        with transaction.atomic():
            locked = PendingUpload.objects.select_for_update().get(id=upload_id, workspace_id=workspace.id)
            if locked.finalized_at and locked.media_asset_id:
                asset = locked.media_asset
            else:
                asset = register_uploaded_asset(
                    pending=locked,
                    inspected=inspected,
                    uploaded_by=api_key.issued_by if api_key.issued_by_id else None,
                    folder=folder,
                    alt_text=args.get("alt_text", "") or "",
                    title=args.get("title", "") or "",
                    tags=tags,
                )
                locked.finalized_at = timezone.now()
                locked.media_asset = asset
                locked.save(update_fields=["finalized_at", "media_asset"])

    # ``asset`` is non-None in both branches above (the replay branch is guarded
    # by ``media_asset_id``); assert it so mypy narrows the nullable FK.
    assert asset is not None

    # Ensure processing is queued — for a fresh asset, or to self-heal a replay
    # whose original enqueue was lost (asset still stuck at 'pending').
    if asset.processing_status == MediaAsset.ProcessingStatus.PENDING:
        process_media_asset(str(asset.id))
    return _wrap_text(_serialize_media(asset))


register_tool(
    Tool(
        name="request_media_upload",
        description=(
            "Step 1 of uploading large media (video, or any file >1 MB) over MCP — no REST "
            "API key required. Returns a short-lived presigned POST: 'url' plus 'fields' to "
            "upload the bytes directly to storage (multipart/form-data: send every 'fields' "
            "entry, then a 'file' field with the body), and an 'upload_id'. After the upload "
            "succeeds, call finalize_media_upload with the upload_id. For files ≤1 MB you can "
            "use upload_media (base64) instead. Requires the upload_media permission."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "filename": {"type": "string", "maxLength": 255},
                "media_type": {
                    "type": "string",
                    "enum": ["image", "video", "gif", "document"],
                    "description": "Used only to size the upload cap; the stored type is re-sniffed at finalize.",
                },
                "content_type": {"type": "string", "description": "MIME type the client will send (e.g. video/mp4)."},
            },
            "required": ["filename", "media_type"],
            "additionalProperties": False,
        },
        handler=_request_media_upload,
    )
)


register_tool(
    Tool(
        name="finalize_media_upload",
        description=(
            "Step 2 of a presigned upload: call with the 'upload_id' from request_media_upload "
            "once the bytes are uploaded. The server validates the stored object (size, real "
            "MIME by magic bytes, storage quota) and registers the media asset. Returns the same "
            "shape as get_media; processing_status starts at 'pending'. Safe to retry — a second "
            "call with the same upload_id returns the same asset. Requires the upload_media permission."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "upload_id": {"type": "string", "format": "uuid"},
                "alt_text": {"type": "string", "maxLength": 2000},
                "title": {"type": "string", "maxLength": 255},
                "folder_id": {"type": "string", "format": "uuid"},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["upload_id"],
            "additionalProperties": False,
        },
        handler=_finalize_media_upload,
    )
)


# ---------------------------------------------------------------------------
# Tool: get_account_analytics
# ---------------------------------------------------------------------------


def _get_account_analytics(args: dict, context: dict[str, Any]) -> dict:
    """Per-channel KPI summary over a rolling window.

    Body is byte-equal to ``GET /api/v1/analytics/accounts/{account_id}``
    because we reuse the same builder; ``test_rest_parity`` enforces
    that.
    """
    _require_perm(context, "view_analytics")
    if "account_id" not in args:
        raise JsonRpcError(INVALID_PARAMS, "account_id is required")
    days_raw = args.get("days", 30)
    # Match the REST surface's ``Query(ge=7, le=90)`` constraint so an
    # agent can't pick a wider window via MCP than via REST.
    if not isinstance(days_raw, int) or isinstance(days_raw, bool) or days_raw < 7 or days_raw > 90:
        raise JsonRpcError(INVALID_PARAMS, "days must be an integer between 7 and 90")
    api_key = context["api_key"]
    sa = _resolve_allowed_account(api_key, args["account_id"])
    return _wrap_text(build_account_analytics(sa, days_raw).model_dump(mode="json"))


register_tool(
    Tool(
        name="get_account_analytics",
        description=(
            "Read a channel's analytics summary over a rolling window: hero KPI metrics "
            "(views/likes/reach/etc.), an engagement-rate card when the platform supports it, "
            "and follower growth. Each metric is returned as ``{value, delta, series, kind}`` "
            "where ``delta`` is the percent change vs. the prior equal-length window and "
            "``series`` is the daily sparkline. Metrics with ``calculated_by_brightbean: true`` (the "
            f"engagement rate, daily averages, estimates) and every ``delta`` are calculated by {settings.BRAND_NAME}, "
            "not reported by the platform; say so when showing them to a user. Includes ``captured_at`` and ``next_sync_eta`` "
            "YouTube uses native period reports, separate raw count cards, ``engagement: null``, Pacific reporting-period metadata "
            "and ``comparison_available``. Missing numeric metrics are omitted and daily gaps remain null. "
            "Disclosure alone does not permit additional derived YouTube analytics. "
            "so an agent can pick a sensible poll delay. Platforms without an analytics surface "
            "(LinkedIn Personal, Bluesky, Mastodon) return ``analytics_available: false`` with "
            "``unavailable_reason``. Requires the view_analytics permission."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "account_id": {
                    "type": "string",
                    "format": "uuid",
                    "description": "SocialAccount ID. Must be in this API key's allowlist.",
                },
                "days": {
                    "type": "integer",
                    "minimum": 7,
                    "maximum": 90,
                    "default": 30,
                    "description": "Rolling window size in days. 7, 30, and 90 are the typical values.",
                },
            },
            "required": ["account_id"],
            "additionalProperties": False,
        },
        handler=_get_account_analytics,
        read_only=True,
    )
)


# ---------------------------------------------------------------------------
# Tool: get_post_analytics
# ---------------------------------------------------------------------------


def _get_post_analytics(args: dict, context: dict[str, Any]) -> dict:
    """Per-post analytics with one envelope per PlatformPost child.

    Designed for the polling loop after ``schedule_post`` /
    ``create_draft``: pass the same ``post_id`` you got back from
    creation and iterate until ``next_sync_eta`` recommends the next
    poll. Drafts and scheduled posts return a valid envelope with empty
    ``metric_tiles`` so the loop has a stable shape from day zero.
    """
    _require_perm(context, "view_analytics")
    if "post_id" not in args:
        raise JsonRpcError(INVALID_PARAMS, "post_id is required")
    api_key = context["api_key"]
    post = _get_post_for_key(api_key, args["post_id"])
    return _wrap_text(build_post_analytics(post).model_dump(mode="json"))


register_tool(
    Tool(
        name="get_post_analytics",
        description=(
            "Read a post's analytics, broken down per platform. For each PlatformPost child "
            "returns the latest value and a since-publish daily sparkline for every metric the "
            "platform reports, plus ``captured_at`` and ``next_sync_eta`` for polling. Drafts "
            "and scheduled posts return an empty ``metric_tiles`` array (not an error), so this "
            "tool is safe to call in a polling loop right after ``schedule_post``. Platforms "
            "without analytics (LinkedIn Personal, Bluesky, Mastodon) carry "
            "``analytics_available: false`` per child. Requires the view_analytics permission."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "post_id": {
                    "type": "string",
                    "format": "uuid",
                    "description": "Parent Post ID (the same one returned by create_draft / schedule_post).",
                },
            },
            "required": ["post_id"],
            "additionalProperties": False,
        },
        handler=_get_post_analytics,
        read_only=True,
    )
)


# ---------------------------------------------------------------------------
# Tool: get_workspace_analytics
# ---------------------------------------------------------------------------


def _parse_iso_date(value: Any, field_name: str):
    from datetime import date

    if not isinstance(value, str):
        raise JsonRpcError(INVALID_PARAMS, f"{field_name} must be an ISO date (YYYY-MM-DD)")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise JsonRpcError(INVALID_PARAMS, f"{field_name} must be an ISO date (YYYY-MM-DD)") from None


def _get_workspace_analytics(args: dict, context: dict[str, Any]) -> dict:
    """Workspace-wide rollup; body is byte-equal to ``GET /api/v1/analytics/overview``."""
    from apps.analytics.ranges import api_range
    from apps.billing.entitlements import require_feature
    from apps.billing.plans import Feature

    _require_perm(context, "view_analytics")
    api_key = context["api_key"]
    workspace = api_key.workspace
    require_feature(workspace.organization, Feature.ANALYTICS)
    days = args.get("days")
    if days is not None and (not isinstance(days, int) or isinstance(days, bool) or days < 1 or days > 90):
        raise JsonRpcError(INVALID_PARAMS, "days must be an integer between 1 and 90")
    start = _parse_iso_date(args["start"], "start") if "start" in args else None
    end = _parse_iso_date(args["end"], "end") if "end" in args else None
    if (start is None) != (end is None):
        raise JsonRpcError(INVALID_PARAMS, "start and end must be given together")
    compare = args.get("compare", True)
    if not isinstance(compare, bool):
        raise JsonRpcError(INVALID_PARAMS, "compare must be a boolean")
    accounts = connected_accounts(api_key)
    rng = api_range(workspace, days=days, start=start, end=end, compare=compare)
    return _wrap_text(
        build_workspace_overview(workspace, accounts, rng, args.get("top", "exposure")).model_dump(mode="json")
    )


register_tool(
    Tool(
        name="get_workspace_analytics",
        description=(
            "Read analytics across every channel this API key can see, over a date range: headline totals "
            "(followers, new followers, views/impressions, engagements, engagement rate, posts published), a "
            "per-network and per-account breakdown, daily totals, and the top posts. Metrics an account does "
            "not report are ``null`` (never 0), and each total says how many accounts reported it. With "
            f"``compare`` (default) the previous equal-length period is included and ``delta`` is {settings.BRAND_NAME}'s "
            "percent change over accounts that reported in both periods. Days are counted in the workspace "
            "timezone. Requires the view_analytics permission and a plan that includes analytics."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 90,
                    "default": 30,
                    "description": "Rolling window ending today.",
                },
                "start": {
                    "type": "string",
                    "format": "date",
                    "description": "Custom range start (inclusive); pair with end.",
                },
                "end": {
                    "type": "string",
                    "format": "date",
                    "description": "Custom range end (inclusive); pair with start.",
                },
                "compare": {"type": "boolean", "default": True, "description": "Include the previous period."},
                "top": {
                    "type": "string",
                    "enum": ["exposure", "engagements"],
                    "default": "exposure",
                    "description": "Rank top posts by views/impressions or by engagements.",
                },
            },
            "additionalProperties": False,
        },
        handler=_get_workspace_analytics,
        read_only=True,
    )
)


# ---------------------------------------------------------------------------
# Inbox: shared helpers
# ---------------------------------------------------------------------------


def _inbox_allowed_account_ids(api_key) -> list:
    return [sa.id for sa in api_key.social_accounts.all()]


def _visible_inbox_qs(api_key):
    """InboxMessages this key may see: in its workspace, on an allowlisted account.

    Fails closed on an empty allowlist rather than relying on ``__in=[]`` folding.
    """
    allowed = _inbox_allowed_account_ids(api_key)
    if not allowed:
        return InboxMessage.objects.none()
    return InboxMessage.objects.filter(
        workspace_id=api_key.workspace_id,
        social_account_id__in=allowed,
    ).select_related("social_account")


def _get_inbox_message_for_key(api_key, message_id_str: str) -> InboxMessage:
    message_id = _parse_uuid(message_id_str, "message_id")
    try:
        return _visible_inbox_qs(api_key).prefetch_related("replies__author").get(id=message_id)
    except InboxMessage.DoesNotExist as exc:
        raise JsonRpcError(INVALID_PARAMS, "Inbox message not found") from exc


def _get_inbox_reply_for_key(api_key, reply_id_str: str) -> InboxReply:
    reply_id = _parse_uuid(reply_id_str, "reply_id")
    allowed = _inbox_allowed_account_ids(api_key)
    try:
        reply = InboxReply.objects.select_related("inbox_message", "inbox_message__social_account", "author").get(
            id=reply_id, inbox_message__workspace_id=api_key.workspace_id
        )
    except InboxReply.DoesNotExist as exc:
        raise JsonRpcError(INVALID_PARAMS, "Reply not found") from exc
    if reply.inbox_message.social_account_id not in allowed:
        raise JsonRpcError(INVALID_PARAMS, "Reply not found")
    return reply


def _serialize_inbox_message(message: InboxMessage) -> dict:
    from apps.api.schemas import InboxMessageResponse

    return InboxMessageResponse.from_message(message, include_replies=True).model_dump(mode="json")


def _serialize_inbox_reply(reply: InboxReply) -> dict:
    from apps.api.schemas import InboxReplyResponse

    return InboxReplyResponse.from_reply(reply).model_dump(mode="json")


# ---------------------------------------------------------------------------
# Tool: list_inbox_messages
# ---------------------------------------------------------------------------


_MCP_INBOX_LIMIT_DEFAULT = 50
_MCP_INBOX_LIMIT_MAX = 100


def _list_inbox_messages(args: dict, context: dict[str, Any]) -> dict:
    _require_perm(context, "use_inbox")
    api_key = context["api_key"]

    status = args.get("status")
    if status is not None and status not in InboxMessage.Status.values:
        raise JsonRpcError(INVALID_PARAMS, f"status must be one of {', '.join(InboxMessage.Status.values)}")
    message_type = args.get("message_type")
    if message_type is not None and message_type not in InboxMessage.MessageType.values:
        raise JsonRpcError(INVALID_PARAMS, f"message_type must be one of {', '.join(InboxMessage.MessageType.values)}")

    raw_limit = args.get("limit")
    try:
        limit = _MCP_INBOX_LIMIT_DEFAULT if raw_limit is None else int(raw_limit)
    except (TypeError, ValueError) as exc:
        raise JsonRpcError(INVALID_PARAMS, f"limit must be an integer between 1 and {_MCP_INBOX_LIMIT_MAX}") from exc
    if limit < 1 or limit > _MCP_INBOX_LIMIT_MAX:
        raise JsonRpcError(INVALID_PARAMS, f"limit must be between 1 and {_MCP_INBOX_LIMIT_MAX}")

    try:
        offset = decode_offset_cursor(args.get("cursor"))
    except ValueError as exc:
        raise JsonRpcError(INVALID_PARAMS, "cursor is not a valid pagination cursor") from exc

    qs = _visible_inbox_qs(api_key).prefetch_related("replies__author")
    if status:
        qs = qs.filter(status=status)
    if message_type:
        qs = qs.filter(message_type=message_type)
    sa_id = args.get("social_account_id")
    if sa_id is not None:
        sa_uuid = _parse_uuid(sa_id, "social_account_id")
        if sa_uuid not in set(_inbox_allowed_account_ids(api_key)):
            raise JsonRpcError(INVALID_PARAMS, "social_account_id is not in this API key's allowlist")
        qs = qs.filter(social_account_id=sa_uuid)
    qs = qs.order_by("-received_at", "id")

    rows = list(qs[offset : offset + limit + 1])
    has_more = len(rows) > limit
    rows = rows[:limit]
    return _wrap_text(
        {
            "messages": [_serialize_inbox_message(m) for m in rows],
            "limit": limit,
            "next_cursor": encode_offset_cursor(offset + limit) if has_more else None,
        }
    )


register_tool(
    Tool(
        name="list_inbox_messages",
        description=(
            "List inbound inbox items (comments, mentions, DMs, reviews) for the accounts this "
            "API key is allowed to act on, newest first. Each item carries its reply thread "
            "(including any draft replies). Optional `status` (unread/open/resolved/archived), "
            "`message_type` (comment/mention/dm/review) and `social_account_id` filters, plus "
            "`limit` (default 50, max 100). When more remain, `next_cursor` is non-null — pass it "
            "back as `cursor`. Requires the use_inbox permission."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": list(InboxMessage.Status.values)},
                "message_type": {"type": "string", "enum": list(InboxMessage.MessageType.values)},
                "social_account_id": {"type": "string", "format": "uuid"},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _MCP_INBOX_LIMIT_MAX,
                    "default": _MCP_INBOX_LIMIT_DEFAULT,
                },
                "cursor": {"type": "string", "description": "Opaque cursor from a previous call's next_cursor."},
            },
            "additionalProperties": False,
        },
        handler=_list_inbox_messages,
        read_only=True,
    )
)


# ---------------------------------------------------------------------------
# Tool: get_inbox_message
# ---------------------------------------------------------------------------


def _get_inbox_message(args: dict, context: dict[str, Any]) -> dict:
    _require_perm(context, "use_inbox")
    if "message_id" not in args:
        raise JsonRpcError(INVALID_PARAMS, "message_id is required")
    message = _get_inbox_message_for_key(context["api_key"], args["message_id"])
    return _wrap_text(_serialize_inbox_message(message))


register_tool(
    Tool(
        name="get_inbox_message",
        description=(
            "Retrieve one inbox message by ID, including its full reply thread and any draft "
            "replies. Returns 'Inbox message not found' for IDs outside this key's workspace or "
            "account allowlist (same as a truly nonexistent ID). Requires the use_inbox permission."
        ),
        input_schema={
            "type": "object",
            "properties": {"message_id": {"type": "string", "format": "uuid"}},
            "required": ["message_id"],
            "additionalProperties": False,
        },
        handler=_get_inbox_message,
        read_only=True,
    )
)


# ---------------------------------------------------------------------------
# Tool: create_reply_draft
# ---------------------------------------------------------------------------


def _create_reply_draft(args: dict, context: dict[str, Any]) -> dict:
    _require_perm(context, "use_inbox")
    api_key = context["api_key"]
    if "message_id" not in args:
        raise JsonRpcError(INVALID_PARAMS, "message_id is required")
    if not args.get("body"):
        raise JsonRpcError(INVALID_PARAMS, "body is required")
    message = _get_inbox_message_for_key(api_key, args["message_id"])
    try:
        reply = create_reply_draft(
            message=message,
            body=args["body"],
            author=api_key.issued_by if api_key.issued_by_id else None,
        )
    except ValueError as exc:
        raise JsonRpcError(INVALID_PARAMS, str(exc)) from exc
    return _wrap_text(_serialize_inbox_reply(reply))


register_tool(
    Tool(
        name="create_reply_draft",
        description=(
            "Draft a reply to an inbox message. The draft is saved but NOT sent to the platform; "
            "a human can review it in the inbox, or call send_reply to deliver it. Requires the "
            "use_inbox permission (drafting is not sending)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "format": "uuid"},
                "body": {"type": "string", "minLength": 1, "maxLength": 10000},
            },
            "required": ["message_id", "body"],
            "additionalProperties": False,
        },
        handler=_create_reply_draft,
    )
)


# ---------------------------------------------------------------------------
# Tool: update_reply_draft
# ---------------------------------------------------------------------------


def _update_reply_draft(args: dict, context: dict[str, Any]) -> dict:
    _require_perm(context, "use_inbox")
    if "reply_id" not in args:
        raise JsonRpcError(INVALID_PARAMS, "reply_id is required")
    if not args.get("body"):
        raise JsonRpcError(INVALID_PARAMS, "body is required")
    reply = _get_inbox_reply_for_key(context["api_key"], args["reply_id"])
    try:
        update_reply_draft(reply, body=args["body"])
    except (ReplyStateError, ValueError) as exc:
        raise JsonRpcError(INVALID_PARAMS, str(exc)) from exc
    return _wrap_text(_serialize_inbox_reply(reply))


register_tool(
    Tool(
        name="update_reply_draft",
        description=(
            "Replace the body of an existing draft (or failed) reply. Sent replies cannot be "
            "edited. Requires the use_inbox permission."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "reply_id": {"type": "string", "format": "uuid"},
                "body": {"type": "string", "minLength": 1, "maxLength": 10000},
            },
            "required": ["reply_id", "body"],
            "additionalProperties": False,
        },
        handler=_update_reply_draft,
    )
)


# ---------------------------------------------------------------------------
# Tool: discard_reply_draft
# ---------------------------------------------------------------------------


def _discard_reply_draft(args: dict, context: dict[str, Any]) -> dict:
    _require_perm(context, "use_inbox")
    if "reply_id" not in args:
        raise JsonRpcError(INVALID_PARAMS, "reply_id is required")
    reply = _get_inbox_reply_for_key(context["api_key"], args["reply_id"])
    reply_id = str(reply.id)
    try:
        discard_reply_draft(reply)
    except ReplyStateError as exc:
        raise JsonRpcError(INVALID_PARAMS, str(exc)) from exc
    return _wrap_text({"discarded": True, "reply_id": reply_id})


register_tool(
    Tool(
        name="discard_reply_draft",
        description=(
            "Delete a draft (or failed) reply. Sent replies are permanent and cannot be "
            "discarded. Requires the use_inbox permission."
        ),
        input_schema={
            "type": "object",
            "properties": {"reply_id": {"type": "string", "format": "uuid"}},
            "required": ["reply_id"],
            "additionalProperties": False,
        },
        handler=_discard_reply_draft,
    )
)


# ---------------------------------------------------------------------------
# Tool: send_reply
# ---------------------------------------------------------------------------


def _send_reply(args: dict, context: dict[str, Any]) -> dict:
    # Sending pushes text to the real platform on the workspace's behalf,
    # so it needs the stronger inbox permission — same split as the web UI.
    _require_perm(context, "reply_from_inbox")
    api_key = context["api_key"]
    actor = api_key.issued_by if api_key.issued_by_id else None

    if "reply_id" in args and ("message_id" in args or "body" in args):
        raise JsonRpcError(INVALID_PARAMS, "reply_id cannot be combined with message_id or body")
    if "reply_id" in args:
        reply = _get_inbox_reply_for_key(api_key, args["reply_id"])
    else:
        if "message_id" not in args or not args.get("body"):
            raise JsonRpcError(
                INVALID_PARAMS,
                "Provide either reply_id (to send an existing draft) or message_id + body",
            )
        message = _get_inbox_message_for_key(api_key, args["message_id"])
        try:
            reply = create_reply_draft(message=message, body=args["body"], author=actor)
        except ValueError as exc:
            raise JsonRpcError(INVALID_PARAMS, str(exc)) from exc

    try:
        send_reply_now(reply, actor=actor)
    except ReplyStateError as exc:
        raise JsonRpcError(INVALID_PARAMS, str(exc)) from exc
    except Exception as exc:  # platform refused it — reply is left in "failed"
        raise JsonRpcError(INVALID_PARAMS, f"Reply not sent: {reply.send_error or 'Please try again later.'}") from exc

    return _wrap_text(_serialize_inbox_reply(reply))


register_tool(
    Tool(
        name="send_reply",
        description=(
            "Deliver a reply to an inbox message's platform. Either pass `reply_id` to send an "
            "existing draft, or `message_id` + `body` to create and send in one step. On a "
            "platform refusal the reply is kept in `failed` state (retry with the same reply_id) "
            "and an error is returned. Requires the reply_from_inbox permission."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "reply_id": {
                    "type": "string",
                    "format": "uuid",
                    "description": "ID of an existing draft/failed reply to send.",
                },
                "message_id": {
                    "type": "string",
                    "format": "uuid",
                    "description": "Inbox message to reply to (with `body`) when not using `reply_id`.",
                },
                "body": {"type": "string", "minLength": 1, "maxLength": 10000},
            },
            "oneOf": [
                {
                    "required": ["reply_id"],
                    "not": {"anyOf": [{"required": ["message_id"]}, {"required": ["body"]}]},
                },
                {"required": ["message_id", "body"], "not": {"required": ["reply_id"]}},
            ],
            "additionalProperties": False,
        },
        handler=_send_reply,
    )
)


# ---------------------------------------------------------------------------
# Tools: AI writing assistant — same services as REST /ai/* and the composer
# ---------------------------------------------------------------------------


def _ai_context(context: dict[str, Any]):
    from apps.ai_writer.models import AIRequestLog
    from apps.ai_writer.services import AIContext

    api_key = context["api_key"]
    return AIContext(
        workspace=api_key.workspace,
        user=api_key.issued_by if api_key.issued_by_id else None,
        source=AIRequestLog.Source.MCP,
    )


def _resolve_allowed_accounts(api_key, ids: list) -> list[SocialAccount]:
    return [_resolve_allowed_account(api_key, i) for i in dict.fromkeys(ids)]


def _generate_caption(args: dict, context: dict[str, Any]) -> dict:
    from apps.ai_writer.client import AIError
    from apps.ai_writer.services import write_caption

    _require_perm(context, "create_posts")
    accounts = _resolve_allowed_accounts(context["api_key"], args.get("social_account_ids") or [])
    try:
        caption = write_caption(_ai_context(context), args["prompt"], accounts)
    except (AIError, ValueError) as exc:
        raise JsonRpcError(INVALID_PARAMS, str(exc)) from exc
    return _wrap_text({"caption": caption})


register_tool(
    Tool(
        name="generate_caption",
        description=(
            "Write a social media caption with the workspace's AI writing assistant, in its brand voice. "
            "Pass social_account_ids to keep it within the smallest of those accounts' character limits. "
            "Returns {caption}; nothing is saved — pass it to create_draft or schedule_post. Counts against "
            "the plan's daily AI allowance. Requires the create_posts permission."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 2000,
                    "description": "What the post should say.",
                },
                "social_account_ids": {
                    "type": "array",
                    "items": {"type": "string", "format": "uuid"},
                    "maxItems": 25,
                    "default": [],
                    "description": "Accounts the caption is for, from this key's allowlist (see list_accounts).",
                },
            },
            "required": ["prompt"],
            "additionalProperties": False,
        },
        handler=_generate_caption,
    )
)


def _adapt_caption(args: dict, context: dict[str, Any]) -> dict:
    from apps.ai_writer.client import AIError
    from apps.ai_writer.services import adapt_for_networks

    _require_perm(context, "create_posts")
    accounts = _resolve_allowed_accounts(context["api_key"], args["social_account_ids"])
    try:
        adapted = adapt_for_networks(_ai_context(context), args["caption"], accounts)
    except (AIError, ValueError) as exc:
        raise JsonRpcError(INVALID_PARAMS, str(exc)) from exc
    return _wrap_text(
        {
            "captions": [
                {
                    "social_account_id": str(sa.id),
                    "platform": sa.platform,
                    "caption": adapted[str(sa.id)],
                    "length": sa.caption_wire_length(adapted[str(sa.id)]),
                    "char_limit": sa.char_limit,
                }
                for sa in accounts
            ]
        }
    )


register_tool(
    Tool(
        name="adapt_caption",
        description=(
            "Rewrite a caption for each account with the AI writing assistant: each network's style, the "
            "workspace's brand voice, and within each account's character limit. Returns one caption per "
            "social_account_id, ready to use as that account's override; nothing is saved. Counts against "
            "the plan's daily AI allowance. Requires the create_posts permission."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "caption": {"type": "string", "minLength": 1, "maxLength": 10000},
                "social_account_ids": {
                    "type": "array",
                    "items": {"type": "string", "format": "uuid"},
                    "minItems": 1,
                    "maxItems": 25,
                    "description": "Accounts to adapt it for, from this key's allowlist (see list_accounts).",
                },
            },
            "required": ["caption", "social_account_ids"],
            "additionalProperties": False,
        },
        handler=_adapt_caption,
    )
)


# ---------------------------------------------------------------------------
# Tools: outbound webhooks (same services as the REST /webhooks routes)
# ---------------------------------------------------------------------------

_MCP_WEBHOOK_DELIVERY_LIMIT_DEFAULT = 50
_MCP_WEBHOOK_DELIVERY_LIMIT_MAX = 100


def _require_webhook_access(context: dict[str, Any]) -> None:
    """The REST webhook routes' check: a managing permission, and a key covering every account."""
    if not webhook_services.can_manage(context["membership"].effective_permissions):
        raise JsonRpcError(
            INVALID_PARAMS, f"Permission denied: needs one of {', '.join(webhook_services.MANAGE_PERMISSIONS)}"
        )
    if not webhook_services.key_covers_workspace(context["api_key"]):
        raise JsonRpcError(
            INVALID_PARAMS,
            "Webhooks report on every account in the workspace, so they need a key that covers all of its accounts.",
        )


def _get_webhook_endpoint(args: dict, context: dict[str, Any]) -> WebhookEndpoint:
    if "endpoint_id" not in args:
        raise JsonRpcError(INVALID_PARAMS, "endpoint_id is required")
    endpoint_id = _parse_uuid(args["endpoint_id"], "endpoint_id")
    try:
        return WebhookEndpoint.objects.get(id=endpoint_id, workspace=context["workspace"])
    except WebhookEndpoint.DoesNotExist as exc:
        raise JsonRpcError(INVALID_PARAMS, "Webhook endpoint not found") from exc


def _list_webhook_endpoints(args: dict, context: dict[str, Any]) -> dict:
    _require_webhook_access(context)
    endpoints = WebhookEndpoint.objects.filter(workspace=context["workspace"])
    return _wrap_text(
        {"endpoints": [WebhookEndpointResponse.from_endpoint(e).model_dump(mode="json") for e in endpoints]}
    )


def _create_webhook_endpoint(args: dict, context: dict[str, Any]) -> dict:
    _require_webhook_access(context)
    for field in ("url", "event_types"):
        if field not in args:
            raise JsonRpcError(INVALID_PARAMS, f"{field} is required")
    user = context["api_key"].issued_by
    try:
        endpoint, secret = webhook_services.create_endpoint(
            context["workspace"],
            url=args["url"],
            event_types=args["event_types"],
            description=args.get("description", ""),
            is_enabled=args.get("is_enabled", True),
            created_by=user,
        )
    except WebhookError as exc:
        raise JsonRpcError(INVALID_PARAMS, str(exc)) from exc
    return _wrap_text(
        WebhookEndpointWithSecretResponse.from_endpoint_and_secret(endpoint, secret).model_dump(mode="json")
    )


def _update_webhook_endpoint(args: dict, context: dict[str, Any]) -> dict:
    _require_webhook_access(context)
    endpoint = _get_webhook_endpoint(args, context)
    changes = {k: args[k] for k in ("url", "event_types", "description", "is_enabled") if k in args}
    try:
        webhook_services.update_endpoint(endpoint, **changes)
    except WebhookError as exc:
        raise JsonRpcError(INVALID_PARAMS, str(exc)) from exc
    return _wrap_text(WebhookEndpointResponse.from_endpoint(endpoint).model_dump(mode="json"))


def _delete_webhook_endpoint(args: dict, context: dict[str, Any]) -> dict:
    _require_webhook_access(context)
    endpoint = _get_webhook_endpoint(args, context)
    endpoint_id = str(endpoint.id)
    endpoint.delete()
    return _wrap_text({"deleted": True, "endpoint_id": endpoint_id})


def _rotate_webhook_secret(args: dict, context: dict[str, Any]) -> dict:
    _require_webhook_access(context)
    endpoint = _get_webhook_endpoint(args, context)
    secret = webhook_services.rotate_secret(endpoint)
    return _wrap_text(
        WebhookEndpointWithSecretResponse.from_endpoint_and_secret(endpoint, secret).model_dump(mode="json")
    )


def _send_webhook_test(args: dict, context: dict[str, Any]) -> dict:
    _require_webhook_access(context)
    endpoint = _get_webhook_endpoint(args, context)
    delivery = webhook_services.send_test_event(endpoint)
    return _wrap_text(WebhookDeliveryResponse.from_delivery(delivery).model_dump(mode="json"))


def _list_webhook_deliveries(args: dict, context: dict[str, Any]) -> dict:
    _require_webhook_access(context)
    endpoint = _get_webhook_endpoint(args, context)
    status = args.get("status")
    if status is not None and status not in WebhookDeliveryStatus.values:
        raise JsonRpcError(INVALID_PARAMS, f"status must be one of {', '.join(WebhookDeliveryStatus.values)}")
    raw_limit = args.get("limit")
    try:
        limit = _MCP_WEBHOOK_DELIVERY_LIMIT_DEFAULT if raw_limit is None else int(raw_limit)
    except (TypeError, ValueError) as exc:
        raise JsonRpcError(
            INVALID_PARAMS, f"limit must be an integer between 1 and {_MCP_WEBHOOK_DELIVERY_LIMIT_MAX}"
        ) from exc
    if limit < 1 or limit > _MCP_WEBHOOK_DELIVERY_LIMIT_MAX:
        raise JsonRpcError(INVALID_PARAMS, f"limit must be between 1 and {_MCP_WEBHOOK_DELIVERY_LIMIT_MAX}")
    try:
        offset = decode_offset_cursor(args.get("cursor"))
    except ValueError as exc:
        raise JsonRpcError(INVALID_PARAMS, "cursor is not a valid pagination cursor") from exc

    qs = endpoint.deliveries.all()
    if status:
        qs = qs.filter(status=status)
    rows = list(qs[offset : offset + limit + 1])
    has_more = len(rows) > limit
    rows = rows[:limit]
    return _wrap_text(
        {
            "deliveries": [WebhookDeliveryResponse.from_delivery(d).model_dump(mode="json") for d in rows],
            "limit": limit,
            "next_cursor": encode_offset_cursor(offset + limit) if has_more else None,
        }
    )


def _resend_webhook_delivery(args: dict, context: dict[str, Any]) -> dict:
    _require_webhook_access(context)
    endpoint = _get_webhook_endpoint(args, context)
    if "delivery_id" not in args:
        raise JsonRpcError(INVALID_PARAMS, "delivery_id is required")
    delivery_id = _parse_uuid(args["delivery_id"], "delivery_id")
    try:
        delivery = endpoint.deliveries.get(id=delivery_id)
    except WebhookDelivery.DoesNotExist as exc:
        raise JsonRpcError(INVALID_PARAMS, "Webhook delivery not found") from exc
    try:
        webhook_services.resend(delivery)
    except WebhookError as exc:
        raise JsonRpcError(INVALID_PARAMS, str(exc)) from exc
    return _wrap_text(WebhookDeliveryResponse.from_delivery(delivery).model_dump(mode="json"))


_WEBHOOK_ENDPOINT_ID_SCHEMA = {"type": "string", "format": "uuid", "description": "From list_webhook_endpoints."}
_WEBHOOK_EVENT_TYPES_SCHEMA = {
    "type": "array",
    "items": {"type": "string", "enum": list(SUBSCRIBABLE_EVENT_TYPES)},
    "minItems": 1,
    "description": "Events the endpoint is told about.",
}
_WEBHOOK_NOTE = (
    "Requires manage_workspace_settings or manage_social_accounts, and a key that covers every account in the "
    "workspace."
)

register_tool(
    Tool(
        name="list_webhook_endpoints",
        description=f"List the workspace's outbound webhook endpoints (never the secrets). {_WEBHOOK_NOTE}",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=_list_webhook_endpoints,
        read_only=True,
    )
)
register_tool(
    Tool(
        name="create_webhook_endpoint",
        description=(
            "Create an outbound webhook endpoint. The URL must be a public http(s) address. The response "
            f"includes the signing `secret`, shown only now (rotate it to get another). {_WEBHOOK_NOTE}"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string", "maxLength": 2000},
                "event_types": _WEBHOOK_EVENT_TYPES_SCHEMA,
                "description": {"type": "string", "maxLength": 255},
                "is_enabled": {"type": "boolean", "default": True},
            },
            "required": ["url", "event_types"],
            "additionalProperties": False,
        },
        handler=_create_webhook_endpoint,
    )
)
register_tool(
    Tool(
        name="update_webhook_endpoint",
        description=(
            "Change a webhook endpoint's url, event_types, description or is_enabled. Setting is_enabled to "
            f"true also clears an automatic shut-off. {_WEBHOOK_NOTE}"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "endpoint_id": _WEBHOOK_ENDPOINT_ID_SCHEMA,
                "url": {"type": "string", "maxLength": 2000},
                "event_types": _WEBHOOK_EVENT_TYPES_SCHEMA,
                "description": {"type": "string", "maxLength": 255},
                "is_enabled": {"type": "boolean"},
            },
            "required": ["endpoint_id"],
            "additionalProperties": False,
        },
        handler=_update_webhook_endpoint,
    )
)
register_tool(
    Tool(
        name="delete_webhook_endpoint",
        description=f"Delete a webhook endpoint and its delivery log. {_WEBHOOK_NOTE}",
        input_schema={
            "type": "object",
            "properties": {"endpoint_id": _WEBHOOK_ENDPOINT_ID_SCHEMA},
            "required": ["endpoint_id"],
            "additionalProperties": False,
        },
        handler=_delete_webhook_endpoint,
    )
)
register_tool(
    Tool(
        name="rotate_webhook_secret",
        description=(
            "Replace a webhook endpoint's signing secret. The old one stops working at once; the new one is "
            f"returned only in this response. {_WEBHOOK_NOTE}"
        ),
        input_schema={
            "type": "object",
            "properties": {"endpoint_id": _WEBHOOK_ENDPOINT_ID_SCHEMA},
            "required": ["endpoint_id"],
            "additionalProperties": False,
        },
        handler=_rotate_webhook_secret,
    )
)
register_tool(
    Tool(
        name="send_webhook_test",
        description=(
            "Queue a `webhook.test` ping to an endpoint (even a turned-off one). Read the result with "
            f"list_webhook_deliveries. {_WEBHOOK_NOTE}"
        ),
        input_schema={
            "type": "object",
            "properties": {"endpoint_id": _WEBHOOK_ENDPOINT_ID_SCHEMA},
            "required": ["endpoint_id"],
            "additionalProperties": False,
        },
        handler=_send_webhook_test,
    )
)
register_tool(
    Tool(
        name="list_webhook_deliveries",
        description=(
            "List an endpoint's recent deliveries, newest first, with status code, duration, attempt count, "
            "the request payload and a truncated response. Optional `status` (pending/delivered/failed) and "
            "`limit` (default 50, max 100); when more remain `next_cursor` is non-null — pass it back as "
            f"`cursor`. {_WEBHOOK_NOTE}"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "endpoint_id": _WEBHOOK_ENDPOINT_ID_SCHEMA,
                "status": {"type": "string", "enum": list(WebhookDeliveryStatus.values)},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _MCP_WEBHOOK_DELIVERY_LIMIT_MAX,
                    "default": _MCP_WEBHOOK_DELIVERY_LIMIT_DEFAULT,
                },
                "cursor": {"type": "string", "description": "Opaque cursor from a previous call's next_cursor."},
            },
            "required": ["endpoint_id"],
            "additionalProperties": False,
        },
        handler=_list_webhook_deliveries,
        read_only=True,
    )
)
register_tool(
    Tool(
        name="resend_webhook_delivery",
        description=(f"Send a delivery again: same payload, same delivery id, fresh retry schedule. {_WEBHOOK_NOTE}"),
        input_schema={
            "type": "object",
            "properties": {
                "endpoint_id": _WEBHOOK_ENDPOINT_ID_SCHEMA,
                "delivery_id": {"type": "string", "format": "uuid"},
            },
            "required": ["endpoint_id", "delivery_id"],
            "additionalProperties": False,
        },
        handler=_resend_webhook_delivery,
    )
)
