"""Fixtures for engagement automations: a workspace with accounts, published posts and a fake network."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from apps.autonomy.models import AutonomyPolicy
from apps.composer.models import PlatformPost, Post
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace
from providers.exceptions import ProviderError
from providers.types import CommentResult, PostMetrics, PublishResult


@pytest.fixture
def workspace(db, organization):
    return Workspace.objects.create(organization=organization, name="Main")


def make_account(workspace, platform="bluesky", name="Sky", platform_id=None):
    return SocialAccount.objects.create(
        workspace=workspace,
        platform=platform,
        account_platform_id=platform_id or f"{platform}-{name}",
        account_name=name,
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )


@pytest.fixture
def account(workspace):
    return make_account(workspace)


@pytest.fixture
def booster(workspace):
    return make_account(workspace, name="Booster")


@pytest.fixture
def toot(workspace):
    """A Mastodon account: a network where another account may boost a post (with a person's approval)."""
    return make_account(workspace, "mastodon", "Toot")


@pytest.fixture
def toot_booster(workspace):
    return make_account(workspace, "mastodon", "Toot booster")


@pytest.fixture
def toot_published(toot):
    return publish_post(toot, remote_id="109", caption="Hello fediverse")


def publish_post(account, *, hours_ago=1, remote_id="at://did:plc:me/app.bsky.feed.post/1", caption="Hello"):
    post = Post.objects.create(workspace=account.workspace, caption=caption)
    return PlatformPost.objects.create(
        post=post,
        social_account=account,
        status=PlatformPost.Status.PUBLISHED,
        published_at=timezone.now() - timedelta(hours=hours_ago),
        platform_post_id=remote_id,
        permalink="https://bsky.app/profile/me/post/1",
    )


@pytest.fixture
def published(account):
    return publish_post(account)


def set_policy(workspace, **fields):
    policy, _ = AutonomyPolicy.objects.update_or_create(workspace=workspace, defaults=fields)
    return policy


@pytest.fixture
def full_auto(workspace):
    """Automations may act at once: no undo window."""
    return set_policy(workspace, publish_level="full_auto", reply_level="full_auto", undo_delay_minutes=0)


class FakeProvider:
    """Stands in for the network: counts come from ``counts``, calls are recorded, errors can be queued."""

    def __init__(self):
        self.counts = {"likes": 0}
        self.calls = []
        self.error = None
        self.reads = 0

    def plug_metrics(self, token, post_id):
        self.reads += 1
        if self.error is not None and self.error[0] == "read":
            raise self.error[1]
        return dict(self.counts)

    def _act(self, name, *args):
        self.calls.append((name, *args))
        if self.error is not None and self.error[0] == "act":
            raise self.error[1]

    def repost_post(self, token, target):
        self._act("repost", target)
        return PublishResult(platform_post_id="repost-1")

    def quote_post(self, token, target, text):
        self._act("quote", target, text)
        return PublishResult(platform_post_id="quote-1")

    def publish_comment(self, token, post_id, text):
        self._act("reply", post_id, text)
        return CommentResult(platform_comment_id="reply-1")


@pytest.fixture
def network(monkeypatch):
    fake = FakeProvider()
    monkeypatch.setattr("apps.publisher.engine._provider_and_access_token", lambda account: (fake, "token"))
    return fake


@pytest.fixture
def owner(org_owner, organization, workspace):
    OrgMembership.objects.filter(user=org_owner).exclude(organization=organization).delete()
    WorkspaceMembership.objects.create(
        user=org_owner, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
    )
    return org_owner


@pytest.fixture
def owner_client(owner):
    client = Client()
    client.force_login(owner)
    return client


@pytest.fixture
def viewer_client(db, organization, workspace):
    from apps.accounts.models import User

    user = User.objects.create_user(email="v@example.com", password="x", name="V", tos_accepted_at=timezone.now())
    OrgMembership.objects.filter(user=user).exclude(organization=organization).delete()
    OrgMembership.objects.create(user=user, organization=organization, org_role=OrgMembership.OrgRole.MEMBER)
    WorkspaceMembership.objects.create(
        user=user, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.VIEWER
    )
    client = Client()
    client.force_login(user)
    return client


__all__ = ["PostMetrics", "ProviderError"]
