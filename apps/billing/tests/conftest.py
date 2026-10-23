"""Shared fixtures for the billing tests.

``billing`` turns billing on with fake Stripe credentials. Nothing here
reaches Stripe: tests that exercise a Stripe call patch ``stripe`` in
``apps.billing.services`` (or the command module) and assert on the call.
"""

from __future__ import annotations

import pytest
from django.test import Client
from django.utils import timezone

from apps.members.models import OrgMembership, WorkspaceMembership
from apps.workspaces.models import Workspace

from .support import PRICE_IDS, WEBHOOK_SECRET


@pytest.fixture(autouse=True)
def _no_stripe_network(monkeypatch):
    """Fail loudly if anything here would reach the real Stripe API."""

    def refuse(*args, **kwargs):
        raise AssertionError("A billing test tried to call the real Stripe API; mock apps.billing.services.stripe.")

    monkeypatch.setattr("stripe._api_requestor._APIRequestor.request", refuse)


@pytest.fixture
def billing(settings):
    settings.BILLING_ENABLED = True
    settings.STRIPE_SECRET_KEY = "sk_test_fake"
    settings.STRIPE_WEBHOOK_SECRET = WEBHOOK_SECRET
    settings.STRIPE_PRICE_IDS = PRICE_IDS
    settings.APP_URL = "https://studio.example.com"
    return settings


@pytest.fixture
def workspace(db, organization):
    return Workspace.objects.create(organization=organization, name="Main")


def _only_member_of(user, organization):
    # Creating a user provisions a personal organization for them; drop it so
    # the request resolves to the organization under test.
    OrgMembership.objects.filter(user=user).exclude(organization=organization).delete()


@pytest.fixture
def owner(org_owner, organization, workspace):
    _only_member_of(org_owner, organization)
    WorkspaceMembership.objects.create(
        user=org_owner, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
    )
    org_owner.last_workspace_id = workspace.id
    org_owner.save(update_fields=["last_workspace_id"])
    return org_owner


@pytest.fixture
def owner_client(owner):
    client = Client()
    client.force_login(owner)
    return client


@pytest.fixture
def member_client(db, organization, workspace):
    from apps.accounts.models import User

    user = User.objects.create_user(
        email="member@example.com", password="testpass123", name="Member", tos_accepted_at=timezone.now()
    )
    _only_member_of(user, organization)
    OrgMembership.objects.create(user=user, organization=organization, org_role=OrgMembership.OrgRole.MEMBER)
    WorkspaceMembership.objects.create(
        user=user, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.EDITOR
    )
    client = Client()
    client.force_login(user)
    return client
