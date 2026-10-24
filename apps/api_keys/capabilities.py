"""Tool-level capabilities: what an agent token may do, in words an owner can act on.

This module is the single source of truth. Every Agent API route and every MCP
tool maps to exactly one capability here, and nothing else decides it:

* REST authentication (``apps.api.auth``) refuses a request whose route is outside the token's
  capabilities with a 403 naming the missing capability.
* MCP ``tools/list`` shows only the tools the token can use, and ``tools/call`` refuses the rest.
* The token forms and the OAuth consent screen list ``CAPABILITIES`` and the presets below.

A capability limit is *in addition to* workspace permissions: the effective reach of a token is its
permissions, intersected with the issuer's, intersected with its capabilities. ``capabilities=None``
means "not limited at the tool level" and is what keys issued before scoped tokens carry, so
nothing that works today stops working. A new token always stores an explicit list.

Adding a route or a tool means adding it to ``_ROUTES`` or ``_TOOLS`` below. A test fails until you do.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Prefix the Agent API is mounted under (config/urls.py); route keys below are relative to it.
API_PREFIX = "/api/v1"

#: Prefix of the OAuth scopes that carry a narrowed capability set (``cap:schedule_posts``).
SCOPE_PREFIX = "cap:"
#: Prefix of the OAuth scope that ties a grant's tokens to one organization (``org:<uuid>``). Only the consent
#: screen adds it; a client can't ask for one.
ORG_SCOPE_PREFIX = "org:"


@dataclass(frozen=True)
class Capability:
    key: str
    group: str
    label: str
    summary: str
    #: ``low`` reads, ``medium`` writes that stay private or in drafts, ``high`` anything public or administrative.
    risk: str
    #: Workspace permissions the capability needs to be useful; a token form pre-ticks these.
    permissions: tuple[str, ...] = ()
    #: Pre-ticked on the OAuth consent screen. High-risk capabilities start unticked.
    default_on: bool = True


CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        "read_calendar",
        "Content",
        "Read posts & calendar",
        "See posts, drafts, queues, posting slots, ideas, templates, tags, events, team members, connected accounts, "
        "engagement automations and followed feeds.",
        "low",
        # Ideas, templates and evergreen items are read with the drafting permission; reading changes nothing.
        ("create_posts",),
    ),
    Capability(
        "draft_posts",
        "Content",
        "Draft posts",
        "Write and edit drafts, duplicate posts, and keep ideas, templates, tags, categories and calendar events.",
        "medium",
        ("create_posts",),
    ),
    Capability(
        "schedule_posts",
        "Content",
        "Schedule & publish",
        "Schedule posts, cancel or delete them, manage queues, posting slots, evergreen recycling, engagement automations "
        "(repost or reply when a post takes off) and RSS auto-posting.",
        "high",
        ("create_posts", "publish_directly", "manage_social_accounts"),
        default_on=False,
    ),
    Capability(
        "upload_media",
        "Content",
        "Media upload",
        "Browse the media library and upload images and video.",
        "medium",
        ("upload_media",),
    ),
    Capability(
        "use_ai_assistant",
        "Content",
        "AI writing assistant",
        "Ask the workspace's AI assistant to write or adapt captions and repurpose articles, PDFs and videos. "
        "Counts toward the AI budget.",
        "medium",
        ("create_posts",),
    ),
    Capability(
        "read_inbox",
        "Inbox",
        "Inbox read",
        "Read comments and direct messages from connected accounts.",
        "low",
        ("use_inbox",),
    ),
    Capability(
        "reply_inbox",
        "Inbox",
        "Inbox reply",
        "Write, edit and send replies to comments and messages. The autonomy policy decides what is sent.",
        "high",
        ("use_inbox", "reply_from_inbox"),
        default_on=False,
    ),
    Capability(
        "read_analytics",
        "Insights",
        "Analytics read",
        "Read analytics, scheduled reports, competitor comparisons and AI visibility.",
        "low",
        ("view_analytics",),
    ),
    Capability(
        "manage_approvals",
        "Review",
        "Approvals",
        "See posts waiting for review, approve, reject or request changes, and comment.",
        "high",
        ("approve_posts",),
        default_on=False,
    ),
    Capability(
        "read_settings",
        "Workspace",
        "Settings read",
        "Read the brand voice, brand kit, autonomy policy, activity log, webhooks and link-in-bio pages.",
        "low",
        # Webhooks are read with an admin permission (``manage_workspace_settings`` or ``manage_social_accounts``),
        # which the issuer grants separately: it is never pre-ticked for a read capability.
        ("create_posts", "view_analytics"),
    ),
    Capability(
        "write_settings",
        "Workspace",
        "Settings write",
        "Change the brand voice, brand kit, autonomy policy, webhooks, link-in-bio pages and tracked competitors, "
        "and run reports (which can email them to their recipients).",
        "high",
        ("manage_workspace_settings", "manage_social_accounts", "view_analytics"),
        default_on=False,
    ),
)

CAPABILITY_KEYS = tuple(c.key for c in CAPABILITIES)
_BY_KEY = {c.key: c for c in CAPABILITIES}

#: Capabilities that carry no workspace secrets and change nothing; safe starting points.
READ_ONLY_KEYS = tuple(c.key for c in CAPABILITIES if c.risk == "low")


def capability(key: str) -> Capability | None:
    return _BY_KEY.get(key)


def label_for(key: str) -> str:
    cap = _BY_KEY.get(key)
    return cap.label if cap else key


def clean(keys) -> list[str]:
    """``keys`` reduced to known capabilities, in catalog order, without duplicates."""
    wanted = set(keys or ())
    return [k for k in CAPABILITY_KEYS if k in wanted]


#: Permissions that widen what a request can do *inside* a route another capability opens (``create_posts`` with
#: ``publish_directly`` schedules instead of drafting; ``pre_approved`` needs ``approve_posts``; ``send`` on a reply
#: needs ``reply_from_inbox``). A token keeps one only while it holds a capability that lists it. Every other
#: permission only matters on routes its own capability already gates, so withdrawing it would just break reads.
GUARDED_PERMISSIONS = frozenset({"publish_directly", "approve_posts", "reply_from_inbox"})


def narrow_permissions(permissions: dict[str, bool], held) -> dict[str, bool]:
    """``permissions`` with the guarded ones withdrawn that no capability the token holds lists.

    A token without ``schedule_posts`` loses ``publish_directly`` and so cannot reach scheduling through a request
    that is otherwise allowed (creating a post with ``action=schedule``, re-timing a post, starting a repeat).
    """
    if held is None:
        return permissions
    held_set = set(held)
    usable = {p for c in CAPABILITIES if c.key in held_set for p in c.permissions}
    return {k: (v and (k not in GUARDED_PERMISSIONS or k in usable)) for k, v in permissions.items()}


def permissions_for(keys) -> list[str]:
    """Workspace permissions that make ``keys`` usable (what a form pre-ticks)."""
    needed: set[str] = set()
    for key in clean(keys):
        needed.update(_BY_KEY[key].permissions)
    return sorted(needed)


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Preset:
    key: str
    label: str
    summary: str
    capabilities: tuple[str, ...]
    #: Forces autonomy to Draft only for the token whatever the workspace allows.
    requires_approval: bool = False
    daily_requests: int | None = None
    daily_ai_calls: int | None = None
    daily_posts: int | None = None


PRESETS: tuple[Preset, ...] = (
    Preset(
        "read_only_analyst",
        "Read-only analyst",
        "Looks at posts, analytics and settings. Cannot write anything.",
        ("read_calendar", "read_analytics", "read_settings"),
        daily_requests=2000,
    ),
    Preset(
        "drafting_assistant",
        "Drafting assistant (needs approval)",
        "Writes drafts and captions. A person approves everything before it goes out.",
        ("read_calendar", "draft_posts", "upload_media", "use_ai_assistant", "read_settings"),
        requires_approval=True,
        daily_requests=2000,
        daily_ai_calls=100,
    ),
    Preset(
        "inbox_helper",
        "Inbox helper (drafts replies)",
        "Reads comments and messages and writes replies as drafts for a person to send.",
        ("read_calendar", "read_inbox", "reply_inbox", "read_settings"),
        requires_approval=True,
        daily_requests=2000,
    ),
    Preset(
        "full_agent",
        "Full agent (guardrails)",
        "Drafts, schedules, replies and reads analytics. Your autonomy policy and the budgets keep it in bounds.",
        (
            "read_calendar",
            "draft_posts",
            "schedule_posts",
            "upload_media",
            "use_ai_assistant",
            "read_inbox",
            "reply_inbox",
            "read_analytics",
            "read_settings",
        ),
        daily_requests=5000,
        daily_ai_calls=200,
        daily_posts=10,
    ),
)

_PRESETS_BY_KEY = {p.key: p for p in PRESETS}


def preset(key: str) -> Preset | None:
    return _PRESETS_BY_KEY.get(key)


# ---------------------------------------------------------------------------
# The registry: every REST route and MCP tool, mapped to one capability
# ---------------------------------------------------------------------------

#: Routes governed per message by the tool registry instead of by one capability.
MCP_ROUTES = frozenset({"POST /mcp", "POST /mcp/"})

_ROUTES: dict[str, tuple[str, ...]] = {
    "read_calendar": (
        "GET /me/",
        "GET /accounts/",
        "GET /posts/",
        "GET /posts/{post_id}",
        "GET /queues/",
        "GET /queues/{queue_id}",
        "GET /slots/",
        "GET /slots/next-free",
        "GET /best-times/",
        "GET /evergreen/",
        "GET /evergreen/{item_id}",
        "GET /ideas/",
        "GET /ideas/{idea_id}",
        "GET /idea-groups/",
        "GET /templates/",
        "GET /templates/{template_id}",
        "GET /tags/",
        "GET /categories/",
        "GET /events/",
        "GET /hashtag-sets/",
        "GET /signatures/",
        "GET /members/",
        "GET /usage/",
        "GET /sets/",
        "GET /sets/{set_id}",
        "GET /plugs/",
        "GET /plugs/{rule_id}",
        "GET /feeds/",
        "GET /feeds/{feed_id}",
    ),
    "draft_posts": (
        "POST /posts/",
        "PATCH /posts/{post_id}",
        "POST /posts/{post_id}/duplicate",
        "POST /ideas/",
        "PATCH /ideas/{idea_id}",
        "DELETE /ideas/{idea_id}",
        "POST /idea-groups/",
        "DELETE /idea-groups/{group_id}",
        "POST /templates/",
        "PATCH /templates/{template_id}",
        "DELETE /templates/{template_id}",
        "POST /tags/",
        "PATCH /tags/{tag_id}",
        "DELETE /tags/{tag_id}",
        "POST /categories/",
        "PATCH /categories/{category_id}",
        "DELETE /categories/{category_id}",
        "POST /events/",
        "DELETE /events/{event_id}",
        "POST /repurpose/jobs/{job_id}/accept",
        "POST /links/",
        "POST /sets/{set_id}/drafts",
    ),
    "schedule_posts": (
        "POST /posts/{post_id}/schedule",
        "POST /posts/{post_id}/cancel",
        "DELETE /posts/{post_id}",
        "POST /posts/{post_id}/resume-thread",
        "POST /queues/{queue_id}/entries",
        "DELETE /queues/{queue_id}/entries/{entry_id}",
        "POST /slots/",
        "PATCH /slots/{slot_id}",
        "DELETE /slots/{slot_id}",
        "POST /best-times/apply",
        "POST /evergreen/",
        "PATCH /evergreen/{item_id}",
        "DELETE /evergreen/{item_id}",
        # Rules and feeds make the workspace post on its own later, so they are scheduling, not drafting.
        "POST /plugs/",
        "PATCH /plugs/{rule_id}",
        "DELETE /plugs/{rule_id}",
        "POST /feeds/",
        "PATCH /feeds/{feed_id}",
        "POST /feeds/{feed_id}/check",
        "DELETE /feeds/{feed_id}/autopost",
    ),
    "upload_media": (
        "POST /media/",
        "GET /media/",
        "GET /media/{media_id}",
    ),
    "use_ai_assistant": (
        "POST /ai/caption",
        "POST /ai/adapt",
        "POST /repurpose/jobs",
        "GET /repurpose/jobs/{job_id}",
        "POST /repurpose/jobs/{job_id}/cancel",
    ),
    "read_inbox": (
        "GET /inbox/",
        "GET /inbox/{message_id}",
    ),
    "reply_inbox": (
        "POST /inbox/{message_id}/replies",
        "PATCH /inbox/replies/{reply_id}",
        "DELETE /inbox/replies/{reply_id}",
        "POST /inbox/replies/{reply_id}/send",
    ),
    "read_analytics": (
        "GET /analytics/overview",
        "GET /analytics/accounts/{account_id}",
        "GET /analytics/posts/{post_id}",
        "GET /reports/",
        "GET /reports/{report_id}/runs/latest",
        "GET /competitors/",
        "GET /competitors/comparison",
        "GET /ai-visibility/",
        "GET /bio/pages/{page_id}/stats",
        "GET /links/",
        "GET /links/stats",
    ),
    "manage_approvals": (
        "GET /approvals/",
        "POST /approvals/{post_id}/approve",
        "POST /approvals/{post_id}/request-changes",
        "POST /approvals/{post_id}/reject",
        "GET /approvals/{post_id}/comments",
        "POST /approvals/{post_id}/comments",
    ),
    "read_settings": (
        "GET /brand-voice/",
        "GET /brand-kit/",
        "GET /autonomy/",
        "GET /autonomy/actions",
        "GET /webhooks/",
        "GET /webhooks/{endpoint_id}",
        "GET /webhooks/{endpoint_id}/deliveries",
        "GET /bio/pages",
        "GET /bio/pages/{page_id}",
    ),
    "write_settings": (
        "PATCH /brand-voice/",
        "PATCH /brand-kit/",
        "PATCH /autonomy/",
        "POST /webhooks/",
        "PATCH /webhooks/{endpoint_id}",
        "DELETE /webhooks/{endpoint_id}",
        "POST /webhooks/{endpoint_id}/rotate-secret",
        "POST /webhooks/{endpoint_id}/test",
        "POST /webhooks/{endpoint_id}/deliveries/{delivery_id}/resend",
        "PATCH /bio/pages/{page_id}",
        "POST /bio/pages/{page_id}/links",
        "PATCH /bio/pages/{page_id}/links/{link_id}",
        "DELETE /bio/pages/{page_id}/links/{link_id}",
        "POST /sets/",
        "PATCH /sets/{set_id}",
        "DELETE /sets/{set_id}",
        "POST /competitors/",
        "DELETE /competitors/{competitor_id}",
        # Running a report can email it to its recipients, so it is not a read.
        "POST /reports/{report_id}/run",
    ),
}

_TOOLS: dict[str, tuple[str, ...]] = {
    "read_calendar": (
        "list_accounts",
        "list_account_sets",
        "get_account_set",
        "list_posts",
        "get_post",
        "list_queues",
        "get_queue",
        "list_posting_slots",
        "find_next_free_slot",
        "get_best_times",
        "list_evergreen_items",
        "list_ideas",
        "get_idea",
        "list_idea_groups",
        "list_templates",
        "get_template",
        "list_tags",
        "list_categories",
        "list_calendar_events",
        "list_hashtag_sets",
        "list_signatures",
        "list_members",
        "get_usage",
        "list_plug_rules",
        "get_plug_rule",
        "list_feeds",
        "get_feed",
    ),
    "draft_posts": (
        "create_draft",
        "update_post",
        "duplicate_post",
        "create_idea",
        "update_idea",
        "delete_idea",
        "create_idea_group",
        "delete_idea_group",
        "create_template",
        "update_template",
        "delete_template",
        "create_tag",
        "rename_tag",
        "delete_tag",
        "create_category",
        "update_category",
        "delete_category",
        "create_calendar_event",
        "delete_calendar_event",
        "accept_repurpose_outputs",
        "create_short_link",
        "create_set_draft",
    ),
    "schedule_posts": (
        "schedule_post",
        "schedule_draft",
        "cancel_post",
        "delete_post",
        "resume_thread",
        "add_post_to_queue",
        "remove_from_queue",
        "create_posting_slot",
        "update_posting_slot",
        "delete_posting_slot",
        "apply_best_times",
        "add_to_evergreen",
        "pause_evergreen_item",
        "remove_from_evergreen",
        "create_plug_rule",
        "update_plug_rule",
        "delete_plug_rule",
        "add_feed",
        "update_feed_autopost",
        "check_feed",
        "stop_feed_autopost",
    ),
    "upload_media": (
        "upload_media",
        "request_media_upload",
        "finalize_media_upload",
        "get_media",
        "search_media",
    ),
    "use_ai_assistant": (
        "generate_caption",
        "adapt_caption",
        "create_repurpose_job",
        "get_repurpose_job",
        "cancel_repurpose_job",
    ),
    "read_inbox": (
        "list_inbox_messages",
        "get_inbox_message",
    ),
    "reply_inbox": (
        "create_reply_draft",
        "update_reply_draft",
        "discard_reply_draft",
        "send_reply",
    ),
    "read_analytics": (
        "get_workspace_analytics",
        "get_account_analytics",
        "get_post_analytics",
        "list_reports",
        "get_latest_report_run",
        "list_competitors",
        "get_competitor_comparison",
        "get_ai_visibility_report",
        "get_bio_stats",
        "list_short_links",
        "get_link_stats",
    ),
    "manage_approvals": (
        "list_approvals",
        "review_post",
        "list_post_comments",
        "add_post_comment",
    ),
    "read_settings": (
        "get_brand_voice",
        "get_brand_kit",
        "get_autonomy_policy",
        "list_agent_actions",
        "list_webhook_endpoints",
        "list_webhook_deliveries",
        "list_bio_pages",
        "get_bio_page",
    ),
    "write_settings": (
        "update_brand_voice",
        "update_brand_kit",
        "update_autonomy_policy",
        "create_webhook_endpoint",
        "update_webhook_endpoint",
        "delete_webhook_endpoint",
        "rotate_webhook_secret",
        "send_webhook_test",
        "resend_webhook_delivery",
        "create_account_set",
        "update_account_set",
        "delete_account_set",
        "update_bio_page",
        "add_bio_link",
        "update_bio_link",
        "delete_bio_link",
        "add_competitor",
        "remove_competitor",
        "run_report",
    ),
}

#: Routes and tools that make exactly one AI request, spent from the token's daily AI budget before they run.
#: A repurposing job makes several and reserves them itself (``apps.api.ops.create_repurpose_job``).
_ONE_AI_CALL_ROUTES = frozenset({"POST /ai/caption", "POST /ai/adapt"})
_ONE_AI_CALL_TOOLS = frozenset({"generate_caption", "adapt_caption"})


def _invert(table: dict[str, tuple[str, ...]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for cap_key, names in table.items():
        for name in names:
            if name in out:
                raise ValueError(f"{name!r} is mapped to two capabilities: {out[name]} and {cap_key}")
            out[name] = cap_key
    return out


_UNKNOWN = set(_ROUTES) | set(_TOOLS)
if _UNKNOWN - set(CAPABILITY_KEYS):
    raise ValueError(f"Registry names unknown capabilities: {sorted(_UNKNOWN - set(CAPABILITY_KEYS))}")

ROUTE_CAPABILITY: dict[str, str] = _invert(_ROUTES)
TOOL_CAPABILITY: dict[str, str] = _invert(_TOOLS)


def tool_capability(name: str) -> str | None:
    return TOOL_CAPABILITY.get(name)


def tool_uses_ai(name: str) -> bool:
    return name in _ONE_AI_CALL_TOOLS


# Route matching. A route is "METHOD /path/{param}"; a request is matched on its method and path.

_PARAM = re.compile(r"\{[^}]+\}")


def _route_pattern(route: str) -> tuple[str, re.Pattern[str]]:
    method, path = route.split(" ", 1)
    pieces = [re.escape(p) for p in _PARAM.split(API_PREFIX + path)]
    regex = "[^/]+".join(pieces).rstrip("/") + "/?"
    return method, re.compile(f"^{regex}$")


_PATTERNS: list[tuple[str, re.Pattern[str], str, str]] | None = None


def _patterns() -> list[tuple[str, re.Pattern[str], str, str]]:
    global _PATTERNS
    if _PATTERNS is None:
        # Literal segments first, so "/slots/next-free" is tried before "/slots/{slot_id}".
        ordered = sorted(ROUTE_CAPABILITY.items(), key=lambda item: item[0].count("{"))
        _PATTERNS = [(*_route_pattern(route), route, cap_key) for route, cap_key in ordered]
    return _PATTERNS


def _match(method: str, path: str) -> tuple[str, str] | None:
    """``(route, capability)`` for the REST route serving ``method path``, or None when it is unmapped."""
    method = (method or "").upper()
    for route_method, pattern, route, cap_key in _patterns():
        if route_method == method and pattern.match(path or ""):
            return route, cap_key
    return None


def capability_for_request(method: str, path: str) -> str | None:
    """The capability the REST route at ``method path`` needs, or None when it is unmapped."""
    found = _match(method, path)
    return found[1] if found else None


def is_mcp_route(method: str, path: str) -> bool:
    return (method or "").upper() == "POST" and (path or "").rstrip("/") == f"{API_PREFIX}/mcp"


def route_uses_ai(method: str, path: str) -> bool:
    found = _match(method, path)
    return found is not None and found[0] in _ONE_AI_CALL_ROUTES


# ---------------------------------------------------------------------------
# What a token may use
# ---------------------------------------------------------------------------


def allows(capabilities, key: str) -> bool:
    """Whether a token holding ``capabilities`` (None = not limited) holds ``key``."""
    return capabilities is None or key in capabilities


def allows_tool(capabilities, tool_name: str) -> bool:
    cap_key = TOOL_CAPABILITY.get(tool_name)
    # An unmapped tool is never shown to a limited token; the registry test keeps this from happening.
    return capabilities is None or (cap_key is not None and cap_key in capabilities)


def intersect(first, second) -> list[str] | None:
    """Capabilities in both sets, where None means not limited."""
    if first is None:
        return None if second is None else clean(second)
    if second is None:
        return clean(first)
    return clean(set(first) & set(second))


def token_capabilities(api_key) -> list[str] | None:
    """The capabilities a key or OAuth stand-in holds; None when it is not limited."""
    raw = getattr(api_key, "capabilities", None)
    return None if raw is None else clean(raw)


# ---------------------------------------------------------------------------
# OAuth scopes
# ---------------------------------------------------------------------------


def scopes_for(keys) -> list[str]:
    return [f"{SCOPE_PREFIX}{k}" for k in clean(keys)]


def from_scope(scope: str | None) -> list[str] | None:
    """Capabilities carried by an OAuth scope string; None when it names none (a grant from before narrowing)."""
    found = [s[len(SCOPE_PREFIX) :] for s in (scope or "").split() if s.startswith(SCOPE_PREFIX)]
    return clean(found) if found else None


def org_scope(organization_id) -> str:
    return f"{ORG_SCOPE_PREFIX}{organization_id}"


def org_from_scope(scope: str | None) -> str | None:
    """The organization id an OAuth scope string is tied to, or None for a grant from before connected apps."""
    for item in (scope or "").split():
        if item.startswith(ORG_SCOPE_PREFIX):
            return item[len(ORG_SCOPE_PREFIX) :]
    return None
