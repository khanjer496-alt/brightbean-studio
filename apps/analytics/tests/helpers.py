"""Builders shared by the rollup, export, alert and view tests."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from apps.accounts.models import User
from apps.analytics.models import AccountInsightsSnapshot, PostInsightsSnapshot
from apps.composer.models import PlatformPost, Post
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.social_accounts.models import SocialAccount


def make_account(workspace, platform, name=None, followers=0, status="connected"):
    return SocialAccount.objects.create(
        workspace=workspace,
        platform=platform,
        account_platform_id=f"{platform}-{name or 'x'}",
        account_name=name or platform,
        follower_count=followers,
        oauth_access_token="token",
        connection_status=status,
    )


def add_daily(account, metric, values: dict[date, float]):
    AccountInsightsSnapshot.objects.bulk_create(
        AccountInsightsSnapshot(social_account=account, metric_key=metric, date=day, value=value)
        for day, value in values.items()
    )


def fill_days(account, metric, start: date, end: date, value: float):
    days = {start + timedelta(days=i): value for i in range((end - start).days + 1)}
    add_daily(account, metric, days)


def make_post(workspace, account, published_at: datetime, caption="A post", snapshots=None, author=None):
    """A published post; ``snapshots`` is ``{metric: [(date, value, captured_at)]}``."""
    post = Post.objects.create(workspace=workspace, caption=caption, author=author)
    platform_post = PlatformPost.objects.create(
        post=post,
        social_account=account,
        status=PlatformPost.Status.PUBLISHED,
        published_at=published_at,
        platform_post_id=f"pp-{post.id}",
    )
    for metric, rows in (snapshots or {}).items():
        for day, value, captured_at in rows:
            row = PostInsightsSnapshot.objects.create(
                platform_post=platform_post, metric_key=metric, date=day, value=value
            )
            PostInsightsSnapshot.objects.filter(pk=row.pk).update(captured_at=captured_at)
    return platform_post


def make_member(workspace, email, role=WorkspaceMembership.WorkspaceRole.OWNER):
    user = User.objects.create_user(
        email=email, password="pw-12345-x", tos_accepted_at=datetime(2026, 1, 1).astimezone()
    )
    OrgMembership.objects.create(user=user, organization=workspace.organization, org_role=OrgMembership.OrgRole.OWNER)
    WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role=role)
    return user
