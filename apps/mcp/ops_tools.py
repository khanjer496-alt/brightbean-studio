"""MCP tools over the shared workspace operations in :mod:`apps.api.ops`.

Each tool is the twin of a REST route: it calls the same operation as the same
actor, so permissions, the account allowlist, validation and the response shape
match. Descriptions say what the tool does, when to reach for it and which
permission it needs, because an agent picks tools from them alone.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from typing import Any

from apps.api import ops
from apps.mcp.handlers import _parse_uuid, _run_op, _wrap_text
from apps.mcp.protocol import INVALID_PARAMS, JsonRpcError
from apps.mcp.tools import Tool, register_tool

_Parse = Callable[[dict], dict[str, Any]]


# ---------------------------------------------------------------------------
# Schema + argument helpers
# ---------------------------------------------------------------------------


def _uuid_prop(description: str = "") -> dict:
    prop: dict[str, Any] = {"type": "string", "format": "uuid"}
    if description:
        prop["description"] = description
    return prop


def _text_prop(description: str = "", *, max_length: int | None = None, min_length: int | None = None) -> dict:
    prop: dict[str, Any] = {"type": "string"}
    if description:
        prop["description"] = description
    if max_length is not None:
        prop["maxLength"] = max_length
    if min_length is not None:
        prop["minLength"] = min_length
    return prop


def _list_props(description: str = "") -> dict:
    return {
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": ops.LIMIT_MAX,
            "default": ops.LIMIT_DEFAULT,
        },
        "cursor": {
            "type": "string",
            "description": description or "Opaque cursor from a previous call's `next_cursor`.",
        },
    }


def _tags_prop(description: str) -> dict:
    return {"type": "array", "items": {"type": "string", "maxLength": 100}, "maxItems": 25, "description": description}


def _uuid_arg(args: dict, name: str, *, required: bool = False):
    if args.get(name) is None:
        if required:
            raise JsonRpcError(INVALID_PARAMS, f"{name} is required")
        return None
    return _parse_uuid(args[name], name)


def _uuid_list_arg(args: dict, name: str):
    if args.get(name) is None:
        return None
    return [_parse_uuid(v, name) for v in args[name]]


def _date_arg(args: dict, name: str, *, required: bool = False):
    value = args.get(name)
    if value is None:
        if required:
            raise JsonRpcError(INVALID_PARAMS, f"{name} is required")
        return None
    try:
        return dt.date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise JsonRpcError(INVALID_PARAMS, f"{name} must be a YYYY-MM-DD date") from exc


def _time_arg(args: dict, name: str):
    value = args.get(name)
    if value is None:
        return None
    try:
        return dt.time.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise JsonRpcError(INVALID_PARAMS, f"{name} must be an HH:MM time") from exc


def _page(args: dict) -> dict[str, Any]:
    return {"limit": ops.LIMIT_DEFAULT if args.get("limit") is None else args["limit"], "cursor": args.get("cursor")}


def _tool(
    name: str,
    description: str,
    op: Callable[..., Any],
    parse: _Parse,
    *,
    properties: dict | None = None,
    required: list[str] | None = None,
    read_only: bool = False,
) -> None:
    """Register a tool that runs ``op`` with the keyword arguments ``parse`` builds from the call."""

    def handler(args: dict, context: dict[str, Any]) -> dict:
        kwargs = parse(args)
        positional = kwargs.pop("_args", ())
        return _wrap_text(_run_op(op, context, *positional, **kwargs))

    schema: dict[str, Any] = {"type": "object", "properties": properties or {}, "additionalProperties": False}
    if required:
        schema["required"] = required
    register_tool(Tool(name=name, description=description, input_schema=schema, handler=handler, read_only=read_only))


# ---------------------------------------------------------------------------
# Posts: delete, duplicate
# ---------------------------------------------------------------------------

_tool(
    "delete_post",
    (
        "Permanently delete a draft, approved or scheduled post (a series' first post also removes its upcoming "
        "copies). Use it to discard content that should not go out; to keep a scheduled post but stop it, use "
        "cancel_post instead. A post that is publishing or already published is an audit record and is refused. "
        "Needs create_posts (plus edit_others_posts for someone else's post). Returns {deleted, id}."
    ),
    ops.delete_post,
    lambda a: {"_args": (_uuid_arg(a, "post_id", required=True),)},
    properties={"post_id": _uuid_prop()},
    required=["post_id"],
)

_tool(
    "duplicate_post",
    (
        "Copy a post into a new draft: caption, title, first comment, media, thread parts, tags, category and "
        "per-platform settings, with no schedule. Use it to reuse a published post or branch a draft; then edit it "
        "or call schedule_draft. Needs create_posts. Returns the new post, like get_post."
    ),
    ops.duplicate_post,
    lambda a: {"_args": (_uuid_arg(a, "post_id", required=True),)},
    properties={"post_id": _uuid_prop()},
    required=["post_id"],
)


# ---------------------------------------------------------------------------
# Approvals and comments
# ---------------------------------------------------------------------------

_tool(
    "list_approvals",
    (
        "List posts in the approval workflow, soonest first. Defaults to status `pending` (waiting on a reviewer or "
        "the client); pass pending_review, pending_client, approved, rejected, changes_requested, on_hold or all. "
        "Use it to find what needs a decision, then call approve_post, request_post_changes or reject_post. Only "
        "posts whose every target is in the key's allowlist appear. Needs approve_posts or create_posts. "
        "Paginated: pass next_cursor back as cursor."
    ),
    ops.list_approvals,
    lambda a: {
        "status": a.get("status"),
        "social_account_id": _uuid_arg(a, "social_account_id"),
        **_page(a),
    },
    properties={
        "status": {
            "type": "string",
            "enum": ["pending", "all", *ops.APPROVAL_STATUSES],
            "description": "Default `pending` = pending_review + pending_client.",
        },
        "social_account_id": _uuid_prop("Only posts targeting this account (must be in the allowlist)."),
        **_list_props(),
    },
    read_only=True,
)

_DECISION_PROPS = {
    "post_id": _uuid_prop(),
    "comment": _text_prop("Feedback for the author.", max_length=10_000),
}


def _decision(decision: str) -> _Parse:
    return lambda a: {"_args": (_uuid_arg(a, "post_id", required=True), decision, a.get("comment", ""))}


_tool(
    "approve_post",
    (
        "Approve a post that is waiting for review, as the person behind this key. In the two-stage workflow an "
        "internal approval moves the post on to client sign-off; otherwise it becomes approved and can be scheduled. "
        "An optional comment is kept in the approval history. Refused (409) when nothing is waiting on this "
        "reviewer. Needs approve_posts (the key's and its issuer's)."
    ),
    ops.decide_approval,
    _decision("approve"),
    properties=_DECISION_PROPS,
    required=["post_id"],
)

_tool(
    "request_post_changes",
    (
        "Send a post under review back to its author for changes. A comment saying what to change is required and "
        "the author is notified. Use it when the post is fixable; use reject_post when it should not go out. "
        "Needs approve_posts."
    ),
    ops.decide_approval,
    _decision("request_changes"),
    properties=_DECISION_PROPS,
    required=["post_id", "comment"],
)

_tool(
    "reject_post",
    (
        "Reject a post under review. A comment giving the reason is required and the author is notified. Use "
        "request_post_changes instead when the author should revise it. Needs approve_posts."
    ),
    ops.decide_approval,
    _decision("reject"),
    properties=_DECISION_PROPS,
    required=["post_id", "comment"],
)

_tool(
    "list_post_comments",
    (
        "Read the comment threads on a post (top-level comments with their replies, oldest first). Client members "
        "only see `external` comments; team members see `internal` ones too. Needs create_posts or approve_posts."
    ),
    ops.list_comments,
    lambda a: {"_args": (_uuid_arg(a, "post_id", required=True),)},
    properties={"post_id": _uuid_prop()},
    required=["post_id"],
    read_only=True,
)

_tool(
    "add_post_comment",
    (
        "Comment on a post, optionally replying to a top-level comment (parent_id). `visibility` is `external` "
        "(default; clients can see it) or `internal` (team only; client members can't write these). `@name` "
        "mentions notify teammates. Needs create_posts or approve_posts."
    ),
    ops.add_comment,
    lambda a: {
        "_args": (_uuid_arg(a, "post_id", required=True),),
        "body": a.get("body", ""),
        "visibility": a.get("visibility", "external"),
        "parent_id": _uuid_arg(a, "parent_id"),
    },
    properties={
        "post_id": _uuid_prop(),
        "body": _text_prop(max_length=10_000, min_length=1),
        "visibility": {"type": "string", "enum": ["internal", "external"], "default": "external"},
        "parent_id": _uuid_prop("A top-level comment on the same post to reply to."),
    },
    required=["post_id", "body"],
)


# ---------------------------------------------------------------------------
# Queues and posting slots
# ---------------------------------------------------------------------------

_tool(
    "list_queues",
    (
        "List the publishing queues on the accounts this key can act on, each with its entry count and next free "
        "posting slot. A queue schedules posts into its account's weekly posting slots. Call this to get a queue_id "
        "for add_post_to_queue."
    ),
    ops.list_queues,
    lambda a: {},
    read_only=True,
)

_tool(
    "get_queue",
    "Read one queue with its entries (post, position and the publish time each was given), earliest first.",
    ops.get_queue,
    lambda a: {"_args": (_uuid_arg(a, "queue_id", required=True),)},
    properties={"queue_id": _uuid_prop()},
    required=["queue_id"],
    read_only=True,
)

_tool(
    "add_post_to_queue",
    (
        "Schedule an existing draft or approved post into a queue's next free posting slot — the way to keep an "
        "account posting on its usual rhythm without choosing times yourself. `priority: true` takes the earliest "
        "slot and pushes queued posts later. The post must target the queue's account. Refused when the queue has no "
        "open slot within 60 days (409) or the account hit its daily cap. Needs create_posts and publish_directly. "
        "Returns the queue entry with assigned_slot_datetime."
    ),
    ops.add_post_to_queue,
    lambda a: {
        "_args": (
            _uuid_arg(a, "queue_id", required=True),
            _uuid_arg(a, "post_id", required=True),
            bool(a.get("priority", False)),
        )
    },
    properties={
        "queue_id": _uuid_prop(),
        "post_id": _uuid_prop(),
        "priority": {"type": "boolean", "default": False},
    },
    required=["queue_id", "post_id"],
)

_tool(
    "remove_from_queue",
    (
        "Take a post out of a queue by its queue entry id (see get_queue). The post goes back to draft and its slot "
        "is left open for the next post. Needs create_posts."
    ),
    ops.remove_queue_entry,
    lambda a: {"_args": (_uuid_arg(a, "queue_id", required=True), _uuid_arg(a, "entry_id", required=True))},
    properties={"queue_id": _uuid_prop(), "entry_id": _uuid_prop()},
    required=["queue_id", "entry_id"],
)

_tool(
    "list_posting_slots",
    (
        "List the weekly posting slots (day of week 0=Monday…6=Sunday, wall-clock time in the workspace timezone) of "
        "the accounts this key can act on, optionally for one account. Queues pick their publish times from these."
    ),
    ops.list_slots,
    lambda a: {"_args": (_uuid_arg(a, "social_account_id"),)},
    properties={"social_account_id": _uuid_prop("Only this account (must be in the allowlist).")},
    read_only=True,
)

_tool(
    "create_posting_slot",
    (
        "Add a weekly posting slot to an account (e.g. Tuesday 09:30). 409 if it already exists. Needs "
        "manage_social_accounts."
    ),
    ops.create_slot,
    lambda a: {
        "social_account_id": _uuid_arg(a, "social_account_id", required=True),
        "day_of_week": a.get("day_of_week"),
        "time": _time_arg(a, "time"),
    },
    properties={
        "social_account_id": _uuid_prop(),
        "day_of_week": {"type": "integer", "minimum": 0, "maximum": 6, "description": "0=Monday … 6=Sunday."},
        "time": _text_prop("HH:MM wall-clock time in the workspace timezone."),
    },
    required=["social_account_id", "day_of_week", "time"],
)

_tool(
    "update_posting_slot",
    (
        "Change a posting slot's time and/or switch it on or off (inactive slots are skipped by queues). 409 if the "
        "new time clashes with another slot that day. Needs manage_social_accounts."
    ),
    ops.update_slot,
    lambda a: {
        "_args": (_uuid_arg(a, "slot_id", required=True),),
        "time": _time_arg(a, "time"),
        "is_active": a.get("is_active"),
    },
    properties={
        "slot_id": _uuid_prop(),
        "time": _text_prop("HH:MM wall-clock time in the workspace timezone."),
        "is_active": {"type": "boolean"},
    },
    required=["slot_id"],
)

_tool(
    "delete_posting_slot",
    "Delete a posting slot. Posts already given that time keep it. Needs manage_social_accounts.",
    ops.delete_slot,
    lambda a: {"_args": (_uuid_arg(a, "slot_id", required=True),)},
    properties={"slot_id": _uuid_prop()},
    required=["slot_id"],
)

_tool(
    "find_next_free_slot",
    (
        "Find an account's next open posting slot(s): upcoming slot times that no scheduled post already holds, "
        "soonest first (UTC). Use it to pick a publish time for schedule_post / schedule_draft that fits the account's "
        "rhythm without double-booking. `count` 1-20 (default 1); fewer come back when the 60-day horizon runs out."
    ),
    ops.next_free_slots,
    lambda a: {
        "_args": (_uuid_arg(a, "social_account_id", required=True), a.get("count", 1)),
    },
    properties={
        "social_account_id": _uuid_prop("Must be in the key's allowlist."),
        "count": {"type": "integer", "minimum": 1, "maximum": 20, "default": 1},
    },
    required=["social_account_id"],
    read_only=True,
)


# ---------------------------------------------------------------------------
# Ideas (kanban)
# ---------------------------------------------------------------------------

_tool(
    "list_ideas",
    (
        "List ideas from the content-ideas kanban board, in board order. Filter by kanban column (group_id), exact "
        "tag, or a search over title and description. Needs create_posts. Paginated."
    ),
    ops.list_ideas,
    lambda a: {
        "group_id": _uuid_arg(a, "group_id"),
        "tag": a.get("tag"),
        "search": a.get("search"),
        **_page(a),
    },
    properties={
        "group_id": _uuid_prop("Only ideas in this kanban column (see list_idea_groups)."),
        "tag": _text_prop(),
        "search": _text_prop("Matches the title or description."),
        **_list_props(),
    },
    read_only=True,
)

_tool(
    "get_idea",
    "Read one idea (title, description, tags, kanban column and position) by id; find ids with list_ideas. Needs create_posts.",
    ops.get_idea,
    lambda a: {"_args": (_uuid_arg(a, "idea_id", required=True),)},
    properties={"idea_id": _uuid_prop()},
    required=["idea_id"],
    read_only=True,
)

_tool(
    "create_idea",
    (
        "Capture a content idea on the kanban board (first column unless group_id is given). Use it to park topics "
        "for later instead of drafting a post. Needs create_posts."
    ),
    ops.create_idea,
    lambda a: {
        "title": a.get("title", ""),
        "description": a.get("description", ""),
        "tags": a.get("tags"),
        "group_id": _uuid_arg(a, "group_id"),
    },
    properties={
        "title": _text_prop(max_length=255, min_length=1),
        "description": _text_prop(max_length=10_000),
        "tags": _tags_prop("Tag names (new ones are added to the workspace's tags)."),
        "group_id": _uuid_prop("Kanban column; defaults to the first."),
    },
    required=["title"],
)

_tool(
    "update_idea",
    (
        "Edit an idea or move it on the board: change title, description or tags, or set group_id / position to move "
        "it. Omitted fields are unchanged. Needs create_posts."
    ),
    ops.update_idea,
    lambda a: {
        "_args": (_uuid_arg(a, "idea_id", required=True),),
        "title": a.get("title"),
        "description": a.get("description"),
        "tags": a.get("tags"),
        "group_id": _uuid_arg(a, "group_id"),
        "position": a.get("position"),
    },
    properties={
        "idea_id": _uuid_prop(),
        "title": _text_prop(max_length=255, min_length=1),
        "description": _text_prop(max_length=10_000),
        "tags": _tags_prop("Replaces the idea's tags."),
        "group_id": _uuid_prop("Move to this kanban column."),
        "position": {"type": "integer", "minimum": 0},
    },
    required=["idea_id"],
)

_tool(
    "delete_idea",
    "Permanently delete an idea from the kanban board (find its id with list_ideas). Use update_idea to move it instead. Needs create_posts.",
    ops.delete_idea,
    lambda a: {"_args": (_uuid_arg(a, "idea_id", required=True),)},
    properties={"idea_id": _uuid_prop()},
    required=["idea_id"],
)

_tool(
    "list_idea_groups",
    "List the kanban columns of the ideas board with how many ideas each holds. Needs create_posts.",
    ops.list_idea_groups,
    lambda a: {},
    read_only=True,
)

_tool(
    "create_idea_group",
    "Add a column to the ideas kanban board (placed last). Needs create_posts.",
    ops.create_idea_group,
    lambda a: {"name": a.get("name", "")},
    properties={"name": _text_prop(max_length=100, min_length=1)},
    required=["name"],
)

_tool(
    "delete_idea_group",
    "Delete an empty kanban column; refused (409) while it still holds ideas — move or delete them first. Needs create_posts.",
    ops.delete_idea_group,
    lambda a: {"_args": (_uuid_arg(a, "group_id", required=True),)},
    properties={"group_id": _uuid_prop()},
    required=["group_id"],
)


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

_TEMPLATE_PROPS = {
    "description": _text_prop(max_length=10_000),
    "caption": _text_prop("Caption text to start posts from.", max_length=10_000),
    "first_comment": _text_prop(max_length=10_000),
    "category_id": _uuid_prop("A content category (see list_categories)."),
    "tags": _tags_prop("Tag names."),
    "platform_account_ids": {
        "type": "array",
        "items": {"type": "string", "format": "uuid"},
        "description": "Accounts the template targets; all must be in the key's allowlist.",
    },
    "media_asset_ids": {"type": "array", "items": {"type": "string", "format": "uuid"}},
}


def _template_fields(a: dict) -> dict[str, Any]:
    return {
        "description": a.get("description"),
        "caption": a.get("caption"),
        "first_comment": a.get("first_comment"),
        "category_id": _uuid_arg(a, "category_id"),
        "tags": a.get("tags"),
        "platform_account_ids": _uuid_list_arg(a, "platform_account_ids"),
        "media_asset_ids": _uuid_list_arg(a, "media_asset_ids"),
    }


_tool(
    "list_templates",
    (
        "List the workspace's reusable post templates (name, caption, first comment, tags, category, target accounts "
        "and media), A-Z. Only the target accounts this key may act on are shown. Use a template's caption as the "
        "starting point for create_draft. Paginated."
    ),
    ops.list_templates,
    lambda a: {"search": a.get("search"), **_page(a)},
    properties={"search": _text_prop("Matches the name or description."), **_list_props()},
    read_only=True,
)

_tool(
    "get_template",
    "Read one post template by id (caption, first comment, tags, category, target accounts and media); find ids with list_templates.",
    ops.get_template,
    lambda a: {"_args": (_uuid_arg(a, "template_id", required=True),)},
    properties={"template_id": _uuid_prop()},
    required=["template_id"],
    read_only=True,
)

_tool(
    "create_template",
    (
        "Save a reusable post template. platform_account_ids must be accounts in the key's allowlist and "
        "media_asset_ids must belong to the workspace. Needs create_posts."
    ),
    ops.create_template,
    lambda a: {
        "name": a.get("name", ""),
        **{k: v for k, v in _template_fields(a).items() if v is not None},
    },
    properties={"name": _text_prop(max_length=200, min_length=1), **_TEMPLATE_PROPS},
    required=["name"],
)

_tool(
    "update_template",
    (
        "Edit a post template; omitted fields are unchanged. platform_account_ids replaces the accounts you can act "
        "on (accounts outside the key's allowlist are kept). Needs create_posts."
    ),
    ops.update_template,
    lambda a: {
        "_args": (_uuid_arg(a, "template_id", required=True),),
        "name": a.get("name"),
        **_template_fields(a),
    },
    properties={"template_id": _uuid_prop(), "name": _text_prop(max_length=200, min_length=1), **_TEMPLATE_PROPS},
    required=["template_id"],
)

_tool(
    "delete_template",
    "Permanently delete a post template (find its id with list_templates). Posts already made from it are unaffected. Needs create_posts.",
    ops.delete_template,
    lambda a: {"_args": (_uuid_arg(a, "template_id", required=True),)},
    properties={"template_id": _uuid_prop()},
    required=["template_id"],
)


# ---------------------------------------------------------------------------
# Tags and categories
# ---------------------------------------------------------------------------

_tool(
    "list_tags",
    "List the workspace's tags, A-Z, optionally filtered by a name substring. Paginated.",
    ops.list_tags,
    lambda a: {"search": a.get("search"), **_page(a)},
    properties={"search": _text_prop(), **_list_props()},
    read_only=True,
)

_tool(
    "create_tag",
    "Create a tag, or return the existing one with that name. Needs create_posts.",
    ops.create_tag,
    lambda a: {"name": a.get("name", "")},
    properties={"name": _text_prop(max_length=100, min_length=1)},
    required=["name"],
)

_tool(
    "rename_tag",
    "Rename a tag everywhere it is used (posts and ideas); 409 if the new name exists. Needs create_posts.",
    ops.rename_tag,
    lambda a: {"_args": (_uuid_arg(a, "tag_id", required=True),), "name": a.get("name", "")},
    properties={"tag_id": _uuid_prop(), "name": _text_prop(max_length=100, min_length=1)},
    required=["tag_id", "name"],
)

_tool(
    "delete_tag",
    "Delete a tag and remove it from the posts and ideas that carry it. Needs create_posts.",
    ops.delete_tag,
    lambda a: {"_args": (_uuid_arg(a, "tag_id", required=True),)},
    properties={"tag_id": _uuid_prop()},
    required=["tag_id"],
)

_tool(
    "list_categories",
    "List the workspace's content categories (id, name, colour, order), used to file posts and queues.",
    ops.list_categories,
    lambda a: {},
    read_only=True,
)

_tool(
    "create_category",
    "Create a content category; 409 if the name exists. `color` is #RRGGBB. Needs create_posts.",
    ops.create_category,
    lambda a: {"name": a.get("name", ""), "color": a.get("color", "#3B82F6")},
    properties={"name": _text_prop(max_length=100, min_length=1), "color": _text_prop("#RRGGBB")},
    required=["name"],
)

_tool(
    "update_category",
    "Rename, recolour or reorder a content category; omitted fields are unchanged. Needs create_posts.",
    ops.update_category,
    lambda a: {
        "_args": (_uuid_arg(a, "category_id", required=True),),
        "name": a.get("name"),
        "color": a.get("color"),
        "position": a.get("position"),
    },
    properties={
        "category_id": _uuid_prop(),
        "name": _text_prop(max_length=100, min_length=1),
        "color": _text_prop("#RRGGBB"),
        "position": {"type": "integer", "minimum": 0},
    },
    required=["category_id"],
)

_tool(
    "delete_category",
    "Delete a content category; posts that used it are left uncategorised. Needs create_posts.",
    ops.delete_category,
    lambda a: {"_args": (_uuid_arg(a, "category_id", required=True),)},
    properties={"category_id": _uuid_prop()},
    required=["category_id"],
)


# ---------------------------------------------------------------------------
# Calendar events
# ---------------------------------------------------------------------------

_tool(
    "list_calendar_events",
    (
        "List custom calendar events (product launches, campaigns, holidays) overlapping date_from…date_to "
        "(YYYY-MM-DD, inclusive, both optional), soonest first. Check it before scheduling around a launch. Paginated."
    ),
    ops.list_events,
    lambda a: {"date_from": _date_arg(a, "date_from"), "date_to": _date_arg(a, "date_to"), **_page(a)},
    properties={
        "date_from": _text_prop("YYYY-MM-DD"),
        "date_to": _text_prop("YYYY-MM-DD"),
        **_list_props(),
    },
    read_only=True,
)

_tool(
    "create_calendar_event",
    (
        "Add a custom event bar to the calendar (end_date defaults to start_date; `color` is #RRGGBB). It does not "
        "schedule or publish anything. Needs create_posts."
    ),
    ops.create_event,
    lambda a: {
        "title": a.get("title", ""),
        "start_date": _date_arg(a, "start_date", required=True),
        "end_date": _date_arg(a, "end_date"),
        "description": a.get("description", ""),
        "color": a.get("color", "#3B82F6"),
    },
    properties={
        "title": _text_prop(max_length=200, min_length=1),
        "start_date": _text_prop("YYYY-MM-DD"),
        "end_date": _text_prop("YYYY-MM-DD; defaults to start_date."),
        "description": _text_prop(max_length=10_000),
        "color": _text_prop("#RRGGBB"),
    },
    required=["title", "start_date"],
)

_tool(
    "delete_calendar_event",
    "Delete a custom calendar event (find its id with list_calendar_events). Posts scheduled on those days are not touched. Needs create_posts.",
    ops.delete_event,
    lambda a: {"_args": (_uuid_arg(a, "event_id", required=True),)},
    properties={"event_id": _uuid_prop()},
    required=["event_id"],
)


# ---------------------------------------------------------------------------
# Hashtag sets, signatures, brand voice, usage
# ---------------------------------------------------------------------------

_tool(
    "list_hashtag_sets",
    "List the workspace's saved hashtag groups; append a set's hashtags to a caption before creating a post.",
    ops.list_hashtag_sets,
    lambda a: {},
    read_only=True,
)

_tool(
    "list_signatures",
    "List the workspace's shared sign-offs (personal signatures are not included); append one to a caption.",
    ops.list_signatures,
    lambda a: {},
    read_only=True,
)

_tool(
    "get_brand_voice",
    (
        "Read the workspace's brand voice — tone, words to use, words to avoid and up to three example posts — which "
        "the AI writing tools apply. Read it before writing captions yourself so they sound on-brand. Needs "
        "create_posts or manage_workspace_settings."
    ),
    ops.get_brand_voice,
    lambda a: {},
    read_only=True,
)

_tool(
    "update_brand_voice",
    (
        "Change the brand voice (tone, words_to_use, words_to_avoid: up to 1,000 characters each; examples: up to 3 "
        "posts of 2,000 characters, replacing the list). Omitted fields are unchanged. It affects every AI request "
        "from this workspace, so change it only when asked. Needs manage_workspace_settings."
    ),
    ops.update_brand_voice,
    lambda a: {k: a[k] for k in ("tone", "words_to_use", "words_to_avoid", "examples") if k in a},
    properties={
        "tone": _text_prop(max_length=1000),
        "words_to_use": _text_prop(max_length=1000),
        "words_to_avoid": _text_prop(max_length=1000),
        "examples": {"type": "array", "items": {"type": "string", "maxLength": 2000}, "maxItems": 3},
    },
)

_tool(
    "get_usage",
    (
        "Read the organization's plan, its limits and current usage (channels, workspaces, scheduled posts this "
        "month, API & MCP calls today, AI requests today; limit null = unlimited). Read-only; check it before a big "
        "batch of scheduling. Self-hosted installs report billing_enabled false."
    ),
    ops.get_usage,
    lambda a: {},
    read_only=True,
)
