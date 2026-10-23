"""Fixtures for the welcome flow, checklist, sample content and hints tests."""

import pytest
from django.utils import timezone

from apps.accounts.models import User
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.onboarding.flow import start_flow
from apps.social_accounts.models import SocialAccount


@pytest.fixture
def owner(db):
    """A signed-up user: org + workspace provisioned by the post_save handler."""
    return User.objects.create_user(
        email="owner@example.com", password="pw12345678", name="Olive Owner", tos_accepted_at=timezone.now()
    )


@pytest.fixture
def workspace(owner):
    return WorkspaceMembership.objects.get(user=owner).workspace


@pytest.fixture
def flow(owner, workspace):
    return start_flow(owner, workspace)


@pytest.fixture
def owner_client(client, owner):
    client.force_login(owner)
    return client


@pytest.fixture
def make_member(workspace):
    """Add a user to the owner's workspace with ``role``."""

    def _make(email, role):
        member = User.objects.create_user(email=email, password="pw12345678", tos_accepted_at=timezone.now())
        # post_save gave them their own org; move them into the owner's.
        OrgMembership.objects.filter(user=member).delete()
        OrgMembership.objects.create(
            user=member, organization=workspace.organization, org_role=OrgMembership.OrgRole.MEMBER
        )
        WorkspaceMembership.objects.filter(user=member).delete()
        WorkspaceMembership.objects.create(user=member, workspace=workspace, workspace_role=role)
        return member

    return _make


@pytest.fixture
def account(workspace):
    return SocialAccount.objects.create(
        workspace=workspace,
        platform="bluesky",
        account_platform_id="did:plc:abc",
        account_name="Olive",
        account_handle="olive.example",
        oauth_access_token="token",
    )
