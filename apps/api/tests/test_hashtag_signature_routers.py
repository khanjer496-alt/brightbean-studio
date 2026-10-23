"""``/api/v1/hashtag-sets`` and ``/api/v1/signatures`` — read-only, workspace scoped."""

from __future__ import annotations

import pytest
from django.test import Client
from django.utils import timezone

from apps.api_keys import services
from apps.composer.models import HashtagSet, Signature
from apps.members.models import PERMISSION_KEYS, OrgMembership, WorkspaceMembership


class _SecureClient(Client):
    def generic(self, method, path, *args, **kwargs):
        kwargs["secure"] = True
        return super().generic(method, path, *args, **kwargs)


@pytest.fixture
def user(db):
    from apps.accounts.models import User

    return User.objects.create_user(
        email="inserts-agent@example.com", password="testpass123", name="Agent", tos_accepted_at=timezone.now()
    )


def _workspace(name):
    from apps.organizations.models import Organization
    from apps.workspaces.models import Workspace

    return Workspace.objects.create(name=name, organization=Organization.objects.create(name=f"{name} Org"))


@pytest.fixture
def workspace(db, user):
    workspace = _workspace("Inserts WS")
    OrgMembership.objects.create(user=user, organization=workspace.organization, org_role=OrgMembership.OrgRole.OWNER)
    WorkspaceMembership.objects.create(
        user=user, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
    )
    return workspace


@pytest.fixture
def client(user, workspace):
    from apps.social_accounts.models import SocialAccount

    account = SocialAccount.objects.create(
        workspace=workspace,
        platform="bluesky",
        account_platform_id="b-1",
        account_name="bluesky",
        connection_status="connected",
        oauth_access_token="tok",
    )
    key = services.issue_api_key(
        workspace=workspace,
        social_accounts=[account],
        issued_by=user,
        name="inserts",
        permissions=list(PERMISSION_KEYS),
    )
    return _SecureClient(HTTP_AUTHORIZATION=f"Bearer {key.plaintext_token}")


@pytest.mark.django_db
class TestHashtagSets:
    def test_lists_the_workspaces_sets(self, client, workspace):
        coffee = HashtagSet.objects.create(workspace=workspace, name="Coffee", hashtags=["#coffee", "#latte"])
        r = client.get("/api/v1/hashtag-sets/")
        assert r.status_code == 200, r.content
        assert r.json() == {
            "hashtag_sets": [{"id": str(coffee.id), "name": "Coffee", "hashtags": ["#coffee", "#latte"]}]
        }

    def test_other_workspaces_sets_are_invisible(self, client):
        HashtagSet.objects.create(workspace=_workspace("Elsewhere"), name="Theirs", hashtags=["#x"])
        assert client.get("/api/v1/hashtag-sets/").json() == {"hashtag_sets": []}

    def test_requires_a_key(self, db):
        assert _SecureClient().get("/api/v1/hashtag-sets/").status_code == 401

    def test_is_read_only(self, client):
        assert client.post("/api/v1/hashtag-sets/", data={}, content_type="application/json").status_code == 405


@pytest.mark.django_db
class TestSignatures:
    def test_lists_workspace_signatures_only(self, client, workspace, user):
        house = Signature.objects.create(workspace=workspace, name="House", body="Sent from the shop", is_default=True)
        Signature.objects.create(user=user, name="Personal", body="mine")
        Signature.objects.create(workspace=_workspace("Elsewhere"), name="Theirs", body="theirs")

        r = client.get("/api/v1/signatures/")

        assert r.status_code == 200, r.content
        assert r.json() == {
            "signatures": [{"id": str(house.id), "name": "House", "body": "Sent from the shop", "is_default": True}]
        }

    def test_requires_a_key(self, db):
        assert _SecureClient().get("/api/v1/signatures/").status_code == 401

    def test_is_read_only(self, client):
        assert client.post("/api/v1/signatures/", data={}, content_type="application/json").status_code == 405
