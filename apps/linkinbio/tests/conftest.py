"""A workspace with people of each role, a published page, and an image to use as avatar or thumbnail."""

from __future__ import annotations

import pytest

from apps.analytics.tests.helpers import make_account, make_member
from apps.linkinbio import services
from apps.media_library.models import MediaAsset
from apps.members.models import WorkspaceMembership
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace


@pytest.fixture
def workspace(db):
    org = Organization.objects.create(name="Bright Agency")
    return Workspace.objects.create(organization=org, name="Acme Coffee", timezone="UTC")


@pytest.fixture
def owner(workspace):
    return make_member(workspace, "owner@example.com")


@pytest.fixture
def viewer(workspace):
    return make_member(workspace, "viewer@example.com", role=WorkspaceMembership.WorkspaceRole.VIEWER)


@pytest.fixture
def image(workspace):
    return MediaAsset.objects.create(
        organization=workspace.organization,
        workspace=workspace,
        file="test/avatar.jpg",
        filename="avatar.jpg",
        media_type=MediaAsset.MediaType.IMAGE,
        mime_type="image/jpeg",
    )


@pytest.fixture
def page(workspace):
    return services.create_page(workspace, title="Acme", slug="acme", bio="Coffee, roasted daily.")


@pytest.fixture
def instagram(workspace):
    account = make_account(workspace, "instagram", "Acme IG")
    account.account_handle = "acme.coffee"
    account.save(update_fields=["account_handle"])
    return account
