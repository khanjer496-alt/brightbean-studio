"""A small multi-key, multi-workspace world shared by the REST and MCP coverage tests.

Workspace A has an owner, two connected accounts and four keys: ``full`` (every
permission, both accounts), ``narrow`` (every permission, account 1 only),
``bare`` (no permissions, both accounts) and ``client`` (issued to a client
member, so it carries only that role's permissions). Workspace B belongs to
someone else and exists to prove nothing leaks across workspaces.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from django.test import Client
from django.utils import timezone

from apps.api_keys import services
from apps.composer.models import PlatformPost, Post
from apps.members.models import PERMISSION_KEYS, OrgMembership, WorkspaceMembership

MCP_URL = "/api/v1/mcp/"


class SecureClient(Client):
    def generic(self, method, path, *args, **kwargs):
        kwargs["secure"] = True
        return super().generic(method, path, *args, **kwargs)


def bearer(token: str) -> SecureClient:
    return SecureClient(HTTP_AUTHORIZATION=f"Bearer {token}")


@dataclass
class World:
    workspace: Any
    owner: Any
    account1: Any
    account2: Any
    other_workspace: Any
    other_owner: Any
    other_account: Any
    clients: dict[str, SecureClient] = field(default_factory=dict)
    client_user: Any = None

    @property
    def full(self) -> SecureClient:
        return self.clients["full"]

    @property
    def narrow(self) -> SecureClient:
        return self.clients["narrow"]

    @property
    def bare(self) -> SecureClient:
        return self.clients["bare"]

    @property
    def client_role(self) -> SecureClient:
        return self.clients["client"]

    @property
    def other(self) -> SecureClient:
        return self.clients["other"]

    @property
    def anonymous(self) -> SecureClient:
        return SecureClient()


def _make_workspace(name: str, email: str):
    from apps.accounts.models import User
    from apps.organizations.models import Organization
    from apps.workspaces.models import Workspace

    user = User.objects.create_user(email=email, password="testpass123", name=name, tos_accepted_at=timezone.now())
    org = Organization.objects.create(name=f"{name} Org")
    workspace = Workspace.objects.create(name=name, organization=org)
    OrgMembership.objects.update_or_create(
        user=user, organization=org, defaults={"org_role": OrgMembership.OrgRole.OWNER}
    )
    WorkspaceMembership.objects.create(
        user=user, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
    )
    return user, workspace


def _account(workspace, handle: str, platform: str = "bluesky"):
    from apps.social_accounts.models import SocialAccount

    return SocialAccount.objects.create(
        workspace=workspace,
        platform=platform,
        account_platform_id=handle,
        account_name=handle,
        connection_status="connected",
        oauth_access_token="tok",
    )


def build_world() -> World:
    from apps.accounts.models import User

    owner, workspace = _make_workspace("Alpha", f"alpha-{uuid.uuid4().hex[:6]}@example.com")
    account1 = _account(workspace, "alpha-1")
    account2 = _account(workspace, "alpha-2")
    other_owner, other_workspace = _make_workspace("Bravo", f"bravo-{uuid.uuid4().hex[:6]}@example.com")
    other_account = _account(other_workspace, "bravo-1")

    def issue(who, ws, accounts, perms, name):
        return services.issue_api_key(
            workspace=ws, social_accounts=accounts, issued_by=who, name=name, permissions=perms
        )

    full = issue(owner, workspace, [account1, account2], list(PERMISSION_KEYS), "full")
    narrow = issue(owner, workspace, [account1], list(PERMISSION_KEYS), "narrow")
    bare = issue(owner, workspace, [account1, account2], [], "bare")
    client_key = issue(owner, workspace, [account1, account2], list(PERMISSION_KEYS), "client")
    other = issue(other_owner, other_workspace, [other_account], list(PERMISSION_KEYS), "other")

    # The client key is handed to a client member: its effective permissions are the
    # intersection with that person's role, as in production.
    client_user = User.objects.create_user(
        email=f"client-{uuid.uuid4().hex[:6]}@example.com",
        password="x",
        name="Cleo Client",
        tos_accepted_at=timezone.now(),
    )
    WorkspaceMembership.objects.create(
        user=client_user, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.CLIENT
    )
    from apps.api_keys.models import ApiKey

    ApiKey.objects.filter(pk=client_key.api_key.pk).update(issued_by=client_user)

    return World(
        workspace=workspace,
        owner=owner,
        account1=account1,
        account2=account2,
        other_workspace=other_workspace,
        other_owner=other_owner,
        other_account=other_account,
        client_user=client_user,
        clients={
            "full": bearer(full.plaintext_token),
            "narrow": bearer(narrow.plaintext_token),
            "bare": bearer(bare.plaintext_token),
            "client": bearer(client_key.plaintext_token),
            "other": bearer(other.plaintext_token),
        },
    )


def make_post(
    workspace,
    author,
    accounts,
    *,
    status: str = "draft",
    caption: str = "caption",
    scheduled_at=None,
    tags=None,
    title: str = "",
) -> Post:
    """A post with one platform target per account, all in ``status``."""
    if status in {"scheduled", "published", "publishing"} and scheduled_at is None:
        scheduled_at = timezone.now() + timedelta(days=1)
    post = Post.objects.create(
        workspace=workspace,
        author=author,
        caption=caption,
        title=title,
        scheduled_at=scheduled_at,
        tags=list(tags or []),
    )
    for account in accounts:
        PlatformPost.objects.create(
            post=post,
            social_account=account,
            status=status,
            scheduled_at=scheduled_at if status in {"scheduled", "published", "publishing"} else None,
        )
    return post


def call_tool(client: Client, name: str, arguments: dict | None = None) -> dict:
    """``tools/call`` over the MCP endpoint; returns the JSON-RPC envelope."""
    response = client.post(
        MCP_URL,
        data=json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments or {}}}
        ),
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    return response.json()


def tool_result(client: Client, name: str, arguments: dict | None = None) -> Any:
    """A tool's decoded JSON result; fails the test if the call errored."""
    envelope = call_tool(client, name, arguments)
    assert "error" not in envelope, envelope
    return json.loads(envelope["result"]["content"][0]["text"])


def tool_error(client: Client, name: str, arguments: dict | None = None) -> str:
    envelope = call_tool(client, name, arguments)
    assert "error" in envelope, envelope
    return envelope["error"]["message"]
