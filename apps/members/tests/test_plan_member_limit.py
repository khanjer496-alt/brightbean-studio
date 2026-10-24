"""Free and Starter include the owner and one other member; Team and up are unlimited.

Client-role users count. Pending invitations hold a seat. A downgrade never
removes anyone: it only stops new invitations until the organization is back
under the limit.
"""

from __future__ import annotations

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.billing.entitlements import PlanLimitError, usage_summary
from apps.billing.tests.support import set_plan
from apps.members.models import Invitation, OrgMembership, WorkspaceMembership
from apps.members.services import accept_invitation, create_invitation
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace


@pytest.fixture
def billing(settings):
    settings.BILLING_ENABLED = True
    return settings


@pytest.fixture
def org(db):
    return Organization.objects.create(name="Test Org")


@pytest.fixture
def workspace(org):
    return Workspace.objects.create(organization=org, name="Main")


@pytest.fixture
def owner(db, org):
    user = User.objects.create_user(
        email="owner@example.com", password="x", name="Owner", tos_accepted_at=timezone.now()
    )
    OrgMembership.objects.filter(user=user).delete()
    OrgMembership.objects.create(user=user, organization=org, org_role=OrgMembership.OrgRole.OWNER)
    return user


def _invite(org, owner, email, assignments=()):
    return create_invitation(
        org=org,
        email=email,
        org_role=OrgMembership.OrgRole.MEMBER,
        workspace_assignments=list(assignments),
        invited_by=owner,
        inviter=owner,
    )


def _member(org, email):
    user = User.objects.create_user(email=email, password="x", name=email, tos_accepted_at=timezone.now())
    OrgMembership.objects.create(user=user, organization=org, org_role=OrgMembership.OrgRole.MEMBER)
    return user


@pytest.mark.django_db
@pytest.mark.parametrize("plan", ["free", "starter"])
def test_owner_plus_one_then_an_upgrade_message(billing, org, owner, plan):
    set_plan(org, plan)
    _invite(org, owner, "approver@example.com")
    with pytest.raises(PlanLimitError) as exc:
        _invite(org, owner, "third@example.com")
    assert exc.value.code == "member_limit"
    assert "owner and 1 other member" in exc.value.message and "Upgrade to Team or higher" in exc.value.message
    assert Invitation.objects.filter(organization=org).count() == 1


@pytest.mark.django_db
def test_client_role_users_count(billing, org, owner, workspace):
    set_plan(org, "starter")
    _invite(org, owner, "client@example.com", [{"workspace_id": str(workspace.id), "role": "client"}])
    with pytest.raises(PlanLimitError):
        _invite(org, owner, "another-client@example.com", [{"workspace_id": str(workspace.id), "role": "client"}])


@pytest.mark.django_db
@pytest.mark.parametrize("plan", ["team", "pro", "ultimate"])
def test_team_and_up_are_unlimited(billing, org, owner, plan):
    set_plan(org, plan)
    for i in range(5):
        _invite(org, owner, f"person{i}@example.com")
    assert Invitation.objects.filter(organization=org).count() == 5


@pytest.mark.django_db
def test_no_limit_when_billing_is_off(org, owner):
    for i in range(4):
        _invite(org, owner, f"person{i}@example.com")


@pytest.mark.django_db
def test_downgrade_keeps_existing_members_but_stops_new_invitations(billing, org, owner):
    _member(org, "a@example.com")
    _member(org, "b@example.com")
    set_plan(org, "free")
    with pytest.raises(PlanLimitError):
        _invite(org, owner, "c@example.com")
    assert OrgMembership.objects.filter(organization=org).count() == 3
    row = next(r for r in usage_summary(org) if r["key"] == "members")
    assert (row["used"], row["limit"], row["over_by"]) == (3, 2, 1)


@pytest.mark.django_db
def test_accepting_after_a_downgrade_is_refused_when_full(billing, org, owner, workspace):
    set_plan(org, "team")
    invitation = _invite(org, owner, "late@example.com", [{"workspace_id": str(workspace.id), "role": "editor"}])
    _member(org, "joined@example.com")
    set_plan(org, "free")
    late = User.objects.create_user(email="late@example.com", password="x", name="Late", tos_accepted_at=timezone.now())
    with pytest.raises(PlanLimitError):
        accept_invitation(invitation, late)
    assert not OrgMembership.objects.filter(user=late, organization=org).exists()
    assert not WorkspaceMembership.objects.filter(user=late, workspace=workspace).exists()


@pytest.mark.django_db
def test_accepting_the_seat_held_by_its_invitation(billing, org, owner):
    set_plan(org, "starter")
    invitation = _invite(org, owner, "approver@example.com")
    approver = User.objects.create_user(
        email="approver@example.com", password="x", name="A", tos_accepted_at=timezone.now()
    )
    accept_invitation(invitation, approver)
    assert OrgMembership.objects.filter(organization=org).count() == 2


@pytest.mark.django_db
def test_invite_form_shows_the_upgrade_message(billing, org, owner):
    set_plan(org, "free")
    _member(org, "a@example.com")
    client = Client()
    client.force_login(owner)
    response = client.post(reverse("members:invite"), {"email": "b@example.com"}, HTTP_HX_REQUEST="true")
    assert response.status_code == 422
    assert "Upgrade to Team or higher" in response.content.decode()
