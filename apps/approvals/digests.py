"""The "Review all (N)" digest: everything waiting on one person, in one email."""

from __future__ import annotations

import zoneinfo

from django.utils import timezone
from django.utils.formats import date_format

from apps.members.models import WorkspaceMembership
from apps.notifications.engine import notify
from apps.notifications.models import EventType

from .emails import KIND_DIGEST, waiting_posts


def send_review_digest(workspace, user, *, posts=None):
    """Email ``user`` one digest of what waits on them in ``workspace``; None when nothing does."""
    posts = waiting_posts(user, workspace) if posts is None else posts
    if not posts:
        return None
    count = len(posts)
    due = [p.waiting_due_at for p in posts if getattr(p, "waiting_due_at", None)]
    title = f"{count} post{'s' if count != 1 else ''} need your approval"
    if due:
        local = timezone.localtime(min(due), zoneinfo.ZoneInfo(workspace.effective_timezone or "UTC"))
        title += f" by {date_format(local, 'D g:i A')}"
    return notify(
        user=user,
        event_type=EventType.APPROVAL_DIGEST,
        title=title,
        body=f"{count} in {workspace.name} {'is' if count == 1 else 'are'} waiting for your decision.",
        data={"kind": KIND_DIGEST, "workspace_id": str(workspace.id)},
    )


def send_review_digests(workspace) -> int:
    """Send the digest to everyone in ``workspace`` who has something waiting; returns how many got one."""
    sent = 0
    memberships = WorkspaceMembership.objects.filter(workspace=workspace).select_related("user", "custom_role")
    for membership in memberships:
        if not membership.effective_permissions.get("approve_posts", False):
            continue
        if send_review_digest(workspace, membership.user):
            sent += 1
    return sent
