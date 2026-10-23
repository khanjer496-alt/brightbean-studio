import ipaddress
import socket

import pytest
from django.utils import timezone

from apps.accounts.models import User
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.social_accounts.models import SocialAccount
from apps.webhooks import services
from apps.workspaces.models import Workspace

PUBLIC_URL = "https://hooks.example.com/postdelegate"


def make_user(email):
    user = User.objects.create_user(email=email, password="testpass123", name=email, tos_accepted_at=timezone.now())
    # Signup provisions a default org and workspace; the tests build their own.
    org_ids = list(OrgMembership.objects.filter(user=user).values_list("organization_id", flat=True))
    WorkspaceMembership.objects.filter(user=user).delete()
    OrgMembership.objects.filter(user=user).delete()
    Organization.objects.filter(id__in=org_ids).delete()
    return user


def add_member(user, organization, workspace, role):
    OrgMembership.objects.get_or_create(
        user=user, organization=organization, defaults={"org_role": OrgMembership.OrgRole.MEMBER}
    )
    return WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role=role)


@pytest.fixture(autouse=True)
def public_urls(monkeypatch):
    """Resolve names without DNS: ``localhost`` is loopback, any other name is public, IP literals are themselves."""

    def resolve(host, port, *args, **kwargs):
        try:
            address = str(ipaddress.ip_address(host.strip("[]")))
        except ValueError:
            address = "127.0.0.1" if host == "localhost" else "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    monkeypatch.setattr("apps.common.validators.socket.getaddrinfo", resolve)


@pytest.fixture
def org(db):
    return Organization.objects.create(name="Org")


@pytest.fixture
def workspace(org):
    return Workspace.objects.create(organization=org, name="Acme")


@pytest.fixture
def owner(org, workspace):
    user = make_user("owner@example.com")
    OrgMembership.objects.create(user=user, organization=org, org_role=OrgMembership.OrgRole.OWNER)
    add_member(user, org, workspace, WorkspaceMembership.WorkspaceRole.OWNER)
    return user


@pytest.fixture
def manager(org, workspace):
    user = make_user("manager@example.com")
    add_member(user, org, workspace, WorkspaceMembership.WorkspaceRole.MANAGER)
    return user


@pytest.fixture
def account(workspace):
    return SocialAccount.objects.create(
        workspace=workspace,
        platform="bluesky",
        account_platform_id="bsky-1",
        account_name="Sky",
        account_handle="sky.example",
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        oauth_access_token="tok",
    )


@pytest.fixture
def endpoint(workspace, owner):
    endpoint, secret = services.create_endpoint(
        workspace, url=PUBLIC_URL, event_types=services.SUBSCRIBABLE_EVENT_TYPES, created_by=owner
    )
    endpoint.plain_secret = secret
    return endpoint
