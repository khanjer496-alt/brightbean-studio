"""A workspace with people of each role and a handful of accounts to group."""

from __future__ import annotations

import pytest

from apps.analytics.tests.helpers import make_account, make_member
from apps.members.models import WorkspaceMembership
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace


@pytest.fixture
def workspace(db):
    org = Organization.objects.create(name="Bright Agency")
    return Workspace.objects.create(organization=org, name="Client A", timezone="UTC")


@pytest.fixture
def other_workspace(db):
    org = Organization.objects.create(name="Other Agency")
    return Workspace.objects.create(organization=org, name="Client B", timezone="UTC")


@pytest.fixture
def owner(workspace):
    return make_member(workspace, "owner@example.com")


@pytest.fixture
def editor(workspace):
    return make_member(workspace, "editor@example.com", role=WorkspaceMembership.WorkspaceRole.EDITOR)


@pytest.fixture
def viewer(workspace):
    return make_member(workspace, "viewer@example.com", role=WorkspaceMembership.WorkspaceRole.VIEWER)


@pytest.fixture
def linkedin(workspace):
    return make_account(workspace, "linkedin", "A LinkedIn")


@pytest.fixture
def pinterest(workspace):
    return make_account(workspace, "pinterest", "A Pinterest")


@pytest.fixture
def tiktok(workspace):
    return make_account(workspace, "tiktok", "A TikTok")


@pytest.fixture
def x_account(workspace):
    return make_account(workspace, "x", "A X")


@pytest.fixture
def stranger(other_workspace):
    return make_account(other_workspace, "linkedin", "B LinkedIn")
