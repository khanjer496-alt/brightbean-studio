"""Closed signup must atomically bind a new identity to its current invitation."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from allauth.account.models import EmailAddress
from allauth.socialaccount.models import SocialAccount, SocialLogin
from django.core.exceptions import PermissionDenied
from django.test import RequestFactory, override_settings
from django.utils import timezone

from apps.accounts.adapters import AccountAdapter, SocialAccountAdapter
from apps.accounts.models import User
from apps.accounts.signals import create_organization_on_signup
from apps.members.models import Invitation, OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def closed_signup():
    with override_settings(REGISTRATION_OPEN=False):
        yield


@pytest.fixture
def invitation():
    inviter = User.objects.create_user(email="invited-signup-owner@example.com")
    workspace = WorkspaceMembership.objects.get(user=inviter).workspace
    return Invitation.objects.create(
        organization=workspace.organization,
        invited_by=inviter,
        email="invited-signup-guest@example.com",
        org_role="member",
        workspace_assignments=[{"workspace_id": str(workspace.pk), "role": "viewer"}],
        expires_at=timezone.now() + timedelta(days=1),
    )


def request_for(invitation=None):
    request = RequestFactory().post("/accounts/signup/")
    request.session = {"pending_invite_token": invitation.token} if invitation else {}
    return request


def form_for(email):
    return SimpleNamespace(cleaned_data={"email": email, "password1": "synthetic-signup-password"})


def social_for(email):
    return SocialLogin(
        user=User(email=email),
        account=SocialAccount(provider="google", uid="synthetic-signup-google-id"),
        email_addresses=[EmailAddress(email=email, verified=True, primary=True)],
    )


@pytest.mark.parametrize("social,with_form", [(False, False), (True, False), (True, True)])
def test_valid_invite_creates_only_intended_membership(invitation, social, with_form):
    request = request_for(invitation)
    request.COOKIES["browser_timezone"] = "Asia/Dubai"
    org_count = Organization.objects.count()
    if social:
        user = SocialAccountAdapter().save_user(
            request, social_for(invitation.email), form=form_for(invitation.email) if with_form else None
        )
    else:
        user = AccountAdapter().save_user(request, User(), form_for(invitation.email))
    create_organization_on_signup(User, request, user, sociallogin=social)
    assert Organization.objects.count() == org_count
    invitation.organization.refresh_from_db()
    assert invitation.organization.default_timezone == "UTC"
    assert list(OrgMembership.objects.filter(user=user).values_list("organization_id", flat=True)) == [
        invitation.organization_id
    ]
    membership = WorkspaceMembership.objects.get(user=user)
    assert membership.workspace_role == "viewer"
    invitation.refresh_from_db()
    assert invitation.accepted_at is not None
    assert "pending_invite_token" not in request.session


@pytest.mark.parametrize("social", [False, True])
def test_invite_cannot_be_used_for_another_email(invitation, social):
    org_count, user_count = Organization.objects.count(), User.objects.count()
    request = request_for(invitation)
    with pytest.raises(PermissionDenied, match="email address"):
        if social:
            SocialAccountAdapter().save_user(request, social_for("different@example.com"))
        else:
            AccountAdapter().save_user(request, User(), form_for("different@example.com"))
    assert Organization.objects.count() == org_count
    assert User.objects.count() == user_count
    invitation.refresh_from_db()
    assert invitation.accepted_at is None


@pytest.mark.parametrize("state", ["expired", "accepted", "rotated", "deleted"])
def test_invite_invalidated_after_signup_page_was_loaded_fails_closed(invitation, state):
    request = request_for(invitation)
    assert AccountAdapter().is_open_for_signup(request)
    org_count, user_count = Organization.objects.count(), User.objects.count()
    if state == "deleted":
        Invitation.objects.filter(pk=invitation.pk).delete()
    else:
        updates = {
            "expired": {"expires_at": timezone.now() - timedelta(seconds=1)},
            "accepted": {"accepted_at": timezone.now()},
            "rotated": {"token": "synthetic-new-token"},
        }[state]
        Invitation.objects.filter(pk=invitation.pk).update(**updates)
    with pytest.raises(PermissionDenied):
        AccountAdapter().save_user(request, User(), form_for(invitation.email))
    assert User.objects.count() == user_count
    assert Organization.objects.count() == org_count


def test_membership_failure_rolls_back_new_user_and_default_organization(invitation):
    request = request_for(invitation)
    org_count, user_count = Organization.objects.count(), User.objects.count()
    with (
        patch("apps.members.services.accept_invitation", side_effect=ValueError("invalid assignment")),
        pytest.raises(PermissionDenied),
    ):
        AccountAdapter().save_user(request, User(), form_for(invitation.email))
    assert User.objects.count() == user_count
    assert Organization.objects.count() == org_count
    invitation.refresh_from_db()
    assert invitation.accepted_at is None


def test_uninvited_save_and_unmanaged_signup_signal_do_not_provision():
    request = request_for()
    user = User(email="uninvited@example.com")
    with pytest.raises(PermissionDenied):
        AccountAdapter().save_user(request, user, form_for(user.email))
    with patch("apps.accounts.signals.provision_organization_and_workspace") as provision:
        with pytest.raises(PermissionDenied):
            create_organization_on_signup(User, request, user)
        provision.assert_not_called()


def test_social_signup_gate_rejects_wrong_email(invitation):
    adapter = SocialAccountAdapter()
    assert adapter.is_open_for_signup(request_for(invitation), social_for(invitation.email))
    assert not adapter.is_open_for_signup(request_for(invitation), social_for("different@example.com"))


def test_social_form_cannot_relabel_another_provider_identity(invitation):
    with pytest.raises(PermissionDenied):
        SocialAccountAdapter().save_user(
            request_for(invitation), social_for("different@example.com"), form=form_for(invitation.email)
        )
    assert not User.objects.filter(email=invitation.email).exists()


def test_archived_invited_workspace_rolls_back_saved_user(invitation):
    from apps.workspaces.models import Workspace

    Workspace.objects.filter(pk=invitation.workspace_assignments[0]["workspace_id"]).update(is_archived=True)
    org_count = Organization.objects.count()
    with pytest.raises(PermissionDenied):
        AccountAdapter().save_user(request_for(invitation), User(), form_for(invitation.email))
    assert not User.objects.filter(email=invitation.email).exists()
    assert Organization.objects.count() == org_count
    invitation.refresh_from_db()
    assert invitation.accepted_at is None


@override_settings(REGISTRATION_OPEN=True)
def test_open_signup_still_provisions_exactly_one_owner_organization():
    request = request_for()
    org_count = Organization.objects.count()
    user = AccountAdapter().save_user(request, User(), form_for("public-signup@example.com"))
    create_organization_on_signup(User, request, user)
    assert Organization.objects.count() == org_count + 1
    assert OrgMembership.objects.get(user=user).org_role == "owner"
    assert WorkspaceMembership.objects.get(user=user).workspace_role == "owner"


def test_signup_post_uses_invited_email_despite_tampered_form(client, invitation):
    session = client.session
    session["pending_invite_token"] = invitation.token
    session.save()
    org_count = Organization.objects.count()
    response = client.post(
        "/accounts/signup/", {"email": "tampered@example.com", "password1": "synthetic-Strong-Pass-382!"}
    )
    assert response.status_code == 302
    assert not User.objects.filter(email="tampered@example.com").exists()
    user = User.objects.get(email=invitation.email)
    assert Organization.objects.count() == org_count
    assert OrgMembership.objects.get(user=user).organization_id == invitation.organization_id
    invitation.refresh_from_db()
    assert invitation.accepted_at is not None
