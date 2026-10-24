"""Approval request emails: one-tap buttons and the "Review all" digest.

The notification engine calls :func:`approval_email_context` when it renders an
email; an event that is not an approval request gets ``None`` and keeps the
generic notification template. The post email shows what each network will
publish (the same data the composer previews draw, see
``apps.composer.previews``) and two single-use buttons; the digest lists
everything waiting on the recipient behind one "Review all" button, plus the
items an autonomy policy held for them, each with its own Approve / Dismiss
links (decided through the autonomy service, see ``decisions.decide_held``).

Pictures are signed, expiring URLs (``links.media_url``), never attachments.
Nothing internal (team-only comments, notes) is put in these emails.
"""

from __future__ import annotations

from django.conf import settings
from django.urls import reverse

from apps.client_portal.services import issue_portal_link
from apps.composer.models import Post
from apps.composer.previews import post_previews
from apps.members.models import WorkspaceMembership
from apps.notifications.models import EventType

from . import decisions
from .links import issue_held_links, issue_links, media_url
from .models import ApprovalReminder

KIND_DIGEST = "review_digest"
DIGEST_LIMIT = 15
HELD_LIMIT = 10

_POST_EVENTS = frozenset({EventType.POST_SUBMITTED, EventType.CLIENT_APPROVAL_REQUESTED, EventType.APPROVAL_REMINDER})


def _app_url() -> str:
    return getattr(settings, "APP_URL", "http://localhost:8000").rstrip("/")


def _membership(user, workspace):
    return WorkspaceMembership.objects.select_related("custom_role").filter(user=user, workspace=workspace).first()


def _can_approve(membership) -> bool:
    return bool(membership and membership.effective_permissions.get("approve_posts", False))


def _review_all_url(user, workspace, membership) -> str:
    """Where "Review all" goes: the portal's batch page for a client, the approvals tab for the team."""
    if membership.workspace_role == WorkspaceMembership.WorkspaceRole.CLIENT:
        return issue_portal_link(user, workspace, landing="review")
    path = reverse("calendar:calendar", kwargs={"workspace_id": workspace.id})
    return f"{_app_url()}{path}?mode=list&tab=approvals"


def _due_at(post, states):
    return (
        ApprovalReminder.objects.filter(post=post, stage__in=states, due_at__isnull=False)
        .order_by("due_at")
        .values_list("due_at", flat=True)
        .first()
    )


def _email_previews(post, targets):
    data = post_previews(post, targets)
    for item in data["media_items"]:
        item["url"] = media_url(item["id"])
    for preview in data["previews"]:
        for part in (preview["thread"] or {}).get("parts", []):
            for item in part["media"]:
                item["url"] = media_url(item["id"])
    return data


def _is_team(membership) -> bool:
    return bool(membership and membership.workspace_role != WorkspaceMembership.WorkspaceRole.CLIENT)


def held_items(user, workspace, membership, *, limit=HELD_LIMIT) -> list[dict]:
    """Items an autonomy policy held that ``user`` may review, each with its one-tap links.

    Team members only, and only the verbs the Activity page would let them use.
    """
    if not _is_team(membership):
        return []
    verbs = decisions.held_verbs(user, workspace)
    if not verbs:
        return []
    items = []
    for action in decisions.held_waiting(workspace)[:limit]:
        allowed = [v for v in verbs if v != "approve" or decisions.can_approve_from_outside(action)]
        items.append({"action": action, "links": issue_held_links(action, user, verbs=allowed)})
    return items


def _activity_url(workspace) -> str:
    return f"{_app_url()}{reverse('autonomy:activity', kwargs={'workspace_id': workspace.id})}?decision=held"


def _post_context(notification):
    data = notification.data
    user = notification.user
    post = (
        Post.objects.select_related("workspace", "author")
        .filter(pk=data["post_id"], workspace_id=data["workspace_id"])
        .first()
    )
    if post is None:
        return None
    workspace = post.workspace
    membership = _membership(user, workspace)
    if not _can_approve(membership):
        return None
    states = decisions.review_states(user, workspace)
    targets = list(
        post.platform_posts.filter(status__in=states)
        .select_related("social_account")
        .order_by("social_account__platform")
    )
    fingerprint = decisions.current_fingerprint(post, user, workspace)
    if not targets or not fingerprint:
        return None
    links = issue_links(post, user, workspace, fingerprint)
    return {
        "html": "approvals/email/post_request.html",
        "text": "approvals/email/post_request.txt",
        "context": {
            "post": post,
            "workspace": workspace,
            "approve_url": links["approve"],
            "changes_url": links["request_changes"],
            "review_all_url": _review_all_url(user, workspace, membership),
            "due_at": _due_at(post, states),
            "timezone": workspace.effective_timezone or "UTC",
            "ttl_hours": settings.APPROVAL_LINK_TTL_HOURS,
            **_email_previews(post, targets),
        },
    }


def waiting_posts(user, workspace):
    """The posts waiting on ``user`` in ``workspace``, soonest deadline first."""
    states = decisions.review_states(user, workspace)
    if not states:
        return []
    posts = list(
        Post.objects.filter(workspace=workspace, platform_posts__status__in=states)
        .distinct()
        .select_related("author")
        .prefetch_related("platform_posts__social_account")
    )
    due = {
        reminder.post_id: reminder.due_at
        for reminder in ApprovalReminder.objects.filter(post__in=posts, stage__in=states, due_at__isnull=False)
    }
    for post in posts:
        post.waiting_due_at = due.get(post.pk)
    far = max((d for d in due.values()), default=None)
    return sorted(posts, key=lambda p: (p.waiting_due_at is None, p.waiting_due_at or far, p.created_at))


def _digest_context(notification):
    from apps.workspaces.models import Workspace

    user = notification.user
    workspace = Workspace.objects.filter(pk=notification.data.get("workspace_id")).first()
    membership = _membership(user, workspace) if workspace else None
    if not _can_approve(membership):
        return None
    posts = waiting_posts(user, workspace)
    held = held_items(user, workspace, membership)
    if not posts and not held:
        return None
    due = [p.waiting_due_at for p in posts if p.waiting_due_at]
    return {
        "html": "approvals/email/review_digest.html",
        "text": "approvals/email/review_digest.txt",
        "context": {
            "workspace": workspace,
            "posts": posts[:DIGEST_LIMIT],
            "count": len(posts),
            "hidden_count": max(len(posts) - DIGEST_LIMIT, 0),
            "earliest_due_at": min(due) if due else None,
            "timezone": workspace.effective_timezone or "UTC",
            "review_all_url": _review_all_url(user, workspace, membership),
            "held": held,
            "activity_url": _activity_url(workspace),
            "ttl_hours": settings.APPROVAL_LINK_TTL_HOURS,
        },
    }


def _held_context(notification):
    """The "held for review" email to a team member, with Approve / Dismiss links when they may use them."""
    from apps.autonomy.models import AgentAction

    data = notification.data
    action = (
        AgentAction.objects.select_related("workspace", "social_account", "platform_post__social_account")
        .filter(pk=data["action_id"], workspace_id=data["workspace_id"])
        .first()
    )
    if action is None or action.decision != "held" or action.review_outcome:
        return None
    workspace = action.workspace
    membership = _membership(notification.user, workspace)
    if not _is_team(membership):
        return None
    verbs = [
        v
        for v in decisions.held_verbs(notification.user, workspace)
        if v != "approve" or decisions.can_approve_from_outside(action)
    ]
    if not verbs:
        return None
    return {
        "html": "approvals/email/held_action.html",
        "text": "approvals/email/held_action.txt",
        "context": {
            "workspace": workspace,
            "item": {"action": action, "links": issue_held_links(action, notification.user, verbs=verbs)},
            "activity_url": _activity_url(workspace),
            "ttl_hours": settings.APPROVAL_LINK_TTL_HOURS,
        },
    }


def approval_email_context(notification) -> dict | None:
    """Templates and context for an approval email, or None for any other notification."""
    data = notification.data or {}
    if notification.event_type == EventType.APPROVAL_DIGEST and data.get("kind") == KIND_DIGEST:
        return _digest_context(notification)
    if notification.event_type in _POST_EVENTS and data.get("post_id") and data.get("workspace_id"):
        return _post_context(notification)
    if notification.event_type == EventType.AGENT_ACTION_HELD and data.get("action_id") and data.get("workspace_id"):
        return _held_context(notification)
    return None
