"""Fixtures for repurposing: a workspace with accounts for every output kind, and a fake Claude."""

from __future__ import annotations

import pytest
from django.test import Client
from django.utils import timezone

from apps.ai_writer.tests.fakes import patch_anthropic
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace


@pytest.fixture(autouse=True)
def fake_claude():
    """Every test here talks to the fake, never the real API."""
    with patch_anthropic() as fake:
        yield fake


@pytest.fixture
def platform_key(settings):
    settings.ANTHROPIC_API_KEY = "sk-ant-platform-key-for-tests"
    return settings


@pytest.fixture
def workspace(db, organization):
    return Workspace.objects.create(organization=organization, name="Main")


def make_account(workspace, platform, name, platform_id=None):
    return SocialAccount.objects.create(
        workspace=workspace,
        platform=platform,
        account_platform_id=platform_id or f"{platform}-1",
        account_name=name,
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )


@pytest.fixture
def bluesky(workspace):
    return make_account(workspace, "bluesky", "Sky")  # 300 characters


@pytest.fixture
def linkedin(workspace):
    return make_account(workspace, "linkedin_personal", "Link")  # 3,000, with escaping


@pytest.fixture
def x_account(workspace):
    return make_account(workspace, "x", "Ex")


@pytest.fixture
def instagram(workspace):
    return make_account(workspace, "instagram", "Gram")


@pytest.fixture
def youtube(workspace):
    return make_account(workspace, "youtube", "Tube", platform_id="UC_owner")


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
