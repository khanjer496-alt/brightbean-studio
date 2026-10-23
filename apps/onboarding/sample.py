"""The example workspace: a calendar full of sample drafts, safe to explore.

"Show me an example" builds a separate workspace in the user's organization
with one placeholder channel and a handful of drafts spread over the coming
days, so a new user sees a working calendar before connecting anything.
Nothing in it can reach a network:

* the placeholder channel is disconnected, so the composer never offers it;
* every post is a draft, and scheduling or publishing one is refused
  (``is_sample_workspace`` is checked wherever a post becomes schedulable);
* the publisher leaves these workspaces alone;
* billing does not count the workspace or its channel.

Removing the example deletes the workspace and everything in it.
"""

from __future__ import annotations

import datetime as dt
import zoneinfo

from django.db import transaction
from django.utils import timezone

from apps.onboarding.models import SampleWorkspace

SAMPLE_WORKSPACE_NAME = "Example workspace (sample)"
SAMPLE_TAG = "sample"
SAMPLE_REFUSAL = "This is an example workspace. Sample posts are never published."
SAMPLE_CONNECT_REFUSAL = "This is an example workspace. Connect channels in your real workspace instead."

# (days from today, local hour, minute, title, caption)
_SAMPLE_POSTS = [
    (1, 9, 0, "Welcome post", "Hello, world! This is where your first post would go. Swap this for your own words."),
    (
        2,
        12,
        30,
        "Behind the scenes",
        "A peek behind the scenes: how we plan a week of content in about ten minutes. What would you like to see?",
    ),
    (
        3,
        8,
        45,
        "Quick tip",
        "Quick tip: batch your posts on one day, then let the schedule do the rest. Future you says thanks.",
    ),
    (
        5,
        17,
        15,
        "Question for the audience",
        "Question for you: what is the one thing you wish you had more time for? Tell us below.",
    ),
    (
        6,
        10,
        0,
        "Weekly roundup",
        "This week in review: what we shipped, what we learned, and what is coming next. Details inside.",
    ),
    (
        8,
        14,
        0,
        "Customer story",
        "A customer story we love: how a small team doubled their reach by showing up consistently.",
    ),
]


def is_sample_workspace(workspace_id) -> bool:
    return SampleWorkspace.objects.filter(workspace_id=workspace_id).exists()


def sample_workspace_for(organization_id):
    """The organization's example workspace, if it has one."""
    from apps.workspaces.models import Workspace

    return Workspace.objects.filter(organization_id=organization_id, sample_marker__isnull=False).first()


@transaction.atomic
def create_sample_workspace(*, user, source_workspace):
    """Build (or return) the example workspace for ``source_workspace``'s organization."""
    from apps.composer.models import PlatformPost, Post
    from apps.members.models import WorkspaceMembership
    from apps.social_accounts.models import SocialAccount
    from apps.workspaces.models import Workspace

    existing = sample_workspace_for(source_workspace.organization_id)
    if existing is not None:
        return existing

    workspace = Workspace.objects.create(
        organization=source_workspace.organization,
        name=SAMPLE_WORKSPACE_NAME,
        description="Sample drafts to explore. Nothing here is ever published.",
        timezone=source_workspace.timezone,
    )
    SampleWorkspace.objects.create(workspace=workspace, created_by=user)
    WorkspaceMembership.objects.create(
        user=user, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
    )

    account = SocialAccount(
        workspace=workspace,
        platform="bluesky",
        account_platform_id="sample-channel",
        account_name="Sample channel",
        account_handle="sample.example",
        connection_status=SocialAccount.ConnectionStatus.DISCONNECTED,
    )
    account._skip_analytics_backfill = True
    account.save()

    tz = zoneinfo.ZoneInfo(workspace.effective_timezone or "UTC")
    today = timezone.now().astimezone(tz).date()
    for days, hour, minute, title, caption in _SAMPLE_POSTS:
        when = dt.datetime.combine(today + dt.timedelta(days=days), dt.time(hour, minute), tzinfo=tz)
        post = Post.objects.create(
            workspace=workspace,
            author=user,
            title=f"Sample: {title}",
            caption=caption,
            tags=[SAMPLE_TAG],
            scheduled_at=when,
        )
        PlatformPost.objects.create(post=post, social_account=account, status=PlatformPost.Status.DRAFT)
    return workspace


@transaction.atomic
def remove_sample_workspace(*, user, organization_id):
    """Delete the example workspace and point ``user`` back at a real one.

    Returns the workspace the user should land on, or ``None`` when there was
    no example to remove.
    """
    from apps.workspaces.models import Workspace

    workspace = sample_workspace_for(organization_id)
    if workspace is None:
        return None
    workspace_id = workspace.id
    workspace.delete()

    target = (
        Workspace.objects.filter(
            memberships__user=user,
            is_archived=False,
            sample_marker__isnull=True,
        )
        .order_by("created_at")
        .first()
    )
    if user.last_workspace_id == workspace_id:
        user.last_workspace_id = target.id if target else None
        user.save(update_fields=["last_workspace_id"])
    return target
