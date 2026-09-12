"""Regression checks for live credential revocation and invitation boundaries."""

from datetime import timedelta

import pytest
from django.core.cache import cache
from django.utils import timezone

from apps.accounts.models import User
from apps.api.auth import _resolve_oauth_actor
from apps.api_keys.services import issue_api_key, verify_token
from apps.members.models import Invitation, OrgMembership, WorkspaceMembership
from apps.members.services import accept_invitation, remove_member
from apps.social_accounts.models import SocialAccount

pytestmark = pytest.mark.django_db


@pytest.fixture
def owner():
    return User.objects.create_user(email="launch-owner@example.com", password="test-only")


@pytest.fixture
def workspace(owner):
    return WorkspaceMembership.objects.get(user=owner).workspace


def test_disabled_issuer_loses_cached_api_key_immediately(owner, workspace):
    cache.clear()
    account = SocialAccount.objects.create(
        workspace=workspace,
        platform="linkedin_personal",
        account_platform_id="launch-test",
        account_name="Test",
        connection_status="connected",
    )
    issued = issue_api_key(
        workspace=workspace, social_accounts=[account], issued_by=owner, name="Test", permissions=["create_posts"]
    )
    assert verify_token(issued.plaintext_token) is not None
    User.objects.filter(pk=owner.pk).update(is_active=False)
    assert verify_token(issued.plaintext_token) is None


def test_archived_workspace_loses_cached_api_key_immediately(owner, workspace):
    cache.clear()
    account = SocialAccount.objects.create(
        workspace=workspace,
        platform="linkedin_personal",
        account_platform_id="launch-archive",
        account_name="Test",
        connection_status="connected",
    )
    issued = issue_api_key(
        workspace=workspace, social_accounts=[account], issued_by=owner, name="Test", permissions=["create_posts"]
    )
    assert verify_token(issued.plaintext_token) is not None
    workspace.is_archived = True
    workspace.save(update_fields=["is_archived"])
    assert verify_token(issued.plaintext_token) is None


def test_disabled_user_cannot_use_existing_mcp_oauth_token(owner):
    from oauth2_provider.models import get_access_token_model

    token = "synthetic-launch-oauth-token"
    get_access_token_model().objects.create(
        user=owner, token=token, scope="mcp", expires=timezone.now() + timedelta(hours=1)
    )
    assert _resolve_oauth_actor(token) is not None
    User.objects.filter(pk=owner.pk).update(is_active=False)
    assert _resolve_oauth_actor(token) is None


def test_admin_cannot_remove_owner_even_with_other_owners(owner, workspace):
    org = workspace.organization
    second = User.objects.create_user(email="second-owner@example.com")
    admin = User.objects.create_user(email="launch-admin@example.com")
    OrgMembership.objects.create(user=second, organization=org, org_role="owner")
    OrgMembership.objects.create(user=admin, organization=org, org_role="admin")
    target = OrgMembership.objects.get(user=owner, organization=org)
    with pytest.raises(ValueError, match="higher than your own"):
        remove_member(org, target, admin)
    assert OrgMembership.objects.filter(pk=target.pk).exists()


@pytest.fixture
def invitation(owner, workspace):
    return Invitation.objects.create(
        organization=workspace.organization,
        email="launch-guest@example.com",
        invited_by=owner,
        expires_at=timezone.now() + timedelta(days=1),
        workspace_assignments=[{"workspace_id": str(workspace.pk), "role": "viewer"}],
    )


@pytest.mark.parametrize("change", ["accepted", "expired", "rotated"])
def test_stale_invitation_instance_cannot_bypass_current_state(invitation, change):
    guest = User.objects.create_user(email=invitation.email)
    updates = {
        "accepted": {"accepted_at": timezone.now()},
        "expired": {"expires_at": timezone.now() - timedelta(seconds=1)},
        "rotated": {"token": "synthetic-replacement-token"},
    }[change]
    Invitation.objects.filter(pk=invitation.pk).update(**updates)
    with pytest.raises(ValueError):
        accept_invitation(invitation, guest)
    assert not OrgMembership.objects.filter(user=guest, organization=invitation.organization).exists()


def test_invitation_rechecks_workspace_organization(invitation, workspace):
    guest = User.objects.create_user(email=invitation.email)
    foreign_workspace = WorkspaceMembership.objects.get(user=guest).workspace
    Invitation.objects.filter(pk=invitation.pk).update(
        workspace_assignments=[{"workspace_id": str(foreign_workspace.pk), "role": "owner"}]
    )
    with pytest.raises(ValueError, match="no longer available"):
        accept_invitation(invitation, guest)
    assert not OrgMembership.objects.filter(user=guest, organization=invitation.organization).exists()
