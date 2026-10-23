"""Shared inbox fixtures.

The inbox test modules each grew their own workspace/account/message setup;
new modules should take these instead. Existing modules still define their
own — a module-level fixture shadows the one here, so they are unaffected.
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.inbox.models import InboxMessage
from apps.social_accounts.models import SocialAccount


@pytest.fixture
def inbox_workspace(db, organization):
    from apps.workspaces.models import Workspace

    return Workspace.objects.create(name="Inbox WS", organization=organization)


@pytest.fixture
def inbox_account(db, inbox_workspace):
    return SocialAccount.objects.create(
        workspace=inbox_workspace,
        platform="facebook",
        account_platform_id="page-1",
        account_name="Page",
        oauth_access_token="tok",
    )


@pytest.fixture
def inbox_message(db, inbox_workspace, inbox_account):
    return InboxMessage.objects.create(
        workspace=inbox_workspace,
        social_account=inbox_account,
        platform_message_id="pm-1",
        message_type=InboxMessage.MessageType.COMMENT,
        sender_name="Ada",
        body="hi?",
        received_at=timezone.now() - timedelta(hours=2),
    )


@pytest.fixture
def inbox_owner(db, user, org_owner, inbox_workspace):
    from apps.members.models import WorkspaceMembership

    WorkspaceMembership.objects.create(
        user=user, workspace=inbox_workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
    )
    return user


@pytest.fixture
def inbox_client(client, inbox_owner):
    client.force_login(inbox_owner)
    return client


@pytest.fixture
def inbox_viewer_client(db, organization, inbox_workspace):
    from django.test import Client

    from apps.accounts.models import User
    from apps.members.models import OrgMembership, WorkspaceMembership

    viewer = User.objects.create_user(
        email="viewer@example.com", password="testpass123", name="Viewer", tos_accepted_at=timezone.now()
    )
    OrgMembership.objects.filter(user=viewer).exclude(organization=organization).delete()
    OrgMembership.objects.create(user=viewer, organization=organization, org_role=OrgMembership.OrgRole.MEMBER)
    WorkspaceMembership.objects.create(
        user=viewer, workspace=inbox_workspace, workspace_role=WorkspaceMembership.WorkspaceRole.VIEWER
    )
    viewer_client = Client()
    viewer_client.force_login(viewer)
    return viewer_client


@pytest.fixture
def make_messages(inbox_workspace, inbox_account):
    """Create ``count`` messages, newest first by index, one minute apart; extra kwargs apply to all."""

    def make(count, *, workspace=None, account=None, prefix="m", **fields):
        workspace = workspace or inbox_workspace
        account = account or inbox_account
        now = timezone.now()
        rows = [
            InboxMessage(
                workspace=workspace,
                social_account=account,
                platform_message_id=f"{prefix}-{i}",
                **{"sender_name": "Ada", "body": f"message {i}", **fields},
                received_at=now - timedelta(minutes=i + 1),
            )
            for i in range(count)
        ]
        return InboxMessage.objects.bulk_create(rows)

    return make


@pytest.fixture
def other_workspace_message(db, organization):
    """A message in a workspace the test user does not belong to."""
    from apps.workspaces.models import Workspace

    workspace = Workspace.objects.create(name="Other WS", organization=organization)
    account = SocialAccount.objects.create(
        workspace=workspace,
        platform="facebook",
        account_platform_id="other-1",
        account_name="Other",
        oauth_access_token="t",
    )
    return InboxMessage.objects.create(
        workspace=workspace,
        social_account=account,
        platform_message_id="other-msg",
        sender_name="Eve",
        body="secret refund",
        received_at=timezone.now(),
    )
