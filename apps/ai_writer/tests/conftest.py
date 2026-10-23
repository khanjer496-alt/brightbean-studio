"""Fixtures for the AI writing assistant: a workspace with a few accounts and a fake Claude."""

from __future__ import annotations

import pytest
from django.test import Client
from django.utils import timezone

from apps.members.models import OrgMembership, WorkspaceMembership
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

from .fakes import patch_anthropic


@pytest.fixture(autouse=True)
def fake_claude():
    """Every test in this package talks to the fake, never the real API."""
    with patch_anthropic() as fake:
        yield fake


@pytest.fixture
def platform_key(settings):
    settings.ANTHROPIC_API_KEY = "sk-ant-platform-key-for-tests"
    return settings


@pytest.fixture
def workspace(db, organization):
    return Workspace.objects.create(organization=organization, name="Main")


def _account(workspace, platform, name):
    return SocialAccount.objects.create(
        workspace=workspace,
        platform=platform,
        account_platform_id=f"{platform}-1",
        account_name=name,
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )


@pytest.fixture
def bluesky(workspace):
    return _account(workspace, "bluesky", "Sky")  # 300 characters


@pytest.fixture
def linkedin(workspace):
    return _account(workspace, "linkedin_personal", "Link")  # 3,000, with escaping


@pytest.fixture
def facebook(workspace):
    return _account(workspace, "facebook", "Page")


@pytest.fixture
def owner(org_owner, organization, workspace):
    # Creating a user provisions a personal organization; keep only ours.
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
def editor_client(db, organization, workspace):
    from apps.accounts.models import User

    user = User.objects.create_user(
        email="editor@example.com", password="testpass123", name="Editor", tos_accepted_at=timezone.now()
    )
    OrgMembership.objects.filter(user=user).exclude(organization=organization).delete()
    OrgMembership.objects.create(user=user, organization=organization, org_role=OrgMembership.OrgRole.MEMBER)
    WorkspaceMembership.objects.create(
        user=user, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.EDITOR
    )
    client = Client()
    client.force_login(user)
    return client
