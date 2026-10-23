"""REST and MCP: repeating posts, and the platform media rules.

Both surfaces schedule through the composer services, so a post whose media
breaks a blocking platform rule is refused on each, and a ``recurrence``
object starts, changes or stops a series the same way the composer's Repeat
control does.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from apps.api_keys import services
from apps.calendar.models import RecurrenceRule
from apps.composer.models import Post
from apps.mcp.protocol import INVALID_PARAMS
from apps.media_library.models import MediaAsset
from apps.members.models import PERMISSION_KEYS, OrgMembership, WorkspaceMembership
from apps.social_accounts.models import SocialAccount

# These tests drive an agent the workspace trusts to publish and reply on its own.
pytestmark = pytest.mark.usefixtures("autonomous_agents")

MCP_URL = "/api/v1/mcp/"


class _SecureClient(Client):
    def generic(self, method, path, *args, **kwargs):
        kwargs["secure"] = True
        return super().generic(method, path, *args, **kwargs)


@pytest.fixture
def user(db):
    from apps.accounts.models import User

    return User.objects.create_user(email="series@example.com", password="pw", tos_accepted_at=timezone.now())


@pytest.fixture
def workspace(db, user):
    from apps.organizations.models import Organization
    from apps.workspaces.models import Workspace

    org = Organization.objects.create(name="Series API Org")
    workspace = Workspace.objects.create(name="Series API WS", organization=org)
    OrgMembership.objects.create(user=user, organization=org, org_role=OrgMembership.OrgRole.OWNER)
    WorkspaceMembership.objects.create(
        user=user, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
    )
    return workspace


def _account(workspace, platform):
    return SocialAccount.objects.create(
        workspace=workspace,
        platform=platform,
        account_platform_id=f"{platform}-api",
        account_name=platform,
        connection_status="connected",
    )


@pytest.fixture
def linkedin(workspace):
    return _account(workspace, "linkedin_personal")


@pytest.fixture
def instagram(workspace):
    return _account(workspace, "instagram")


def _client(user, workspace, accounts, permissions=None):
    key = services.issue_api_key(
        workspace=workspace,
        social_accounts=accounts,
        issued_by=user,
        name="series",
        permissions=permissions or list(PERMISSION_KEYS),
    )
    return _SecureClient(HTTP_AUTHORIZATION=f"Bearer {key.plaintext_token}")


@pytest.fixture
def client(user, workspace, linkedin, instagram):
    return _client(user, workspace, [linkedin, instagram])


def _image(workspace, *, width=1080, height=1080):
    return MediaAsset.objects.create(
        organization=workspace.organization,
        workspace=workspace,
        file="test/image",
        filename="photo.jpg",
        media_type=MediaAsset.MediaType.IMAGE,
        mime_type="image/jpeg",
        width=width,
        height=height,
    )


def _when(days=1):
    return (timezone.now() + timedelta(days=days)).replace(microsecond=0).isoformat()


def _create(client, account, **body):
    payload = {"social_account_id": str(account.id), "caption": "hello", "action": "schedule", "scheduled_at": _when()}
    payload.update(body)
    return client.post("/api/v1/posts/", data=json.dumps(payload), content_type="application/json")


def _patch(client, post_id, body):
    return client.patch(f"/api/v1/posts/{post_id}", data=json.dumps(body), content_type="application/json")


def _mcp(client, tool, arguments):
    rpc = {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": tool, "arguments": arguments}, "id": 1}
    return client.post(MCP_URL, data=json.dumps(rpc), content_type="application/json").json()


def _copies(post_id):
    return Post.objects.filter(recurrence_source_id=post_id)


@pytest.mark.django_db
class TestRestRecurrence:
    def test_create_with_a_weekly_repeat(self, client, linkedin):
        response = _create(client, linkedin, recurrence={"frequency": "weekly", "weekdays": [1, 3], "count": 5})

        assert response.status_code == 201, response.content
        body = response.json()
        assert body["recurrence"]["weekdays"] == [1, 3]
        assert body["recurrence"]["summary"] == "Every Tuesday and Thursday, 5 times"
        assert body["recurrence_source_id"] is None
        assert _copies(body["id"]).count() == 4

    def test_copies_point_back_to_the_series(self, client, linkedin):
        post_id = _create(client, linkedin, recurrence={"frequency": "daily", "count": 2}).json()["id"]
        copy = _copies(post_id).get()

        body = client.get(f"/api/v1/posts/{copy.id}").json()

        assert body["recurrence"] is None
        assert body["recurrence_source_id"] == post_id

    def test_a_draft_cannot_repeat(self, client, linkedin):
        response = _create(client, linkedin, action="draft", recurrence={"frequency": "daily"})

        assert response.status_code == 422
        assert not Post.objects.exists()

    def test_an_invalid_repeat_is_refused_before_the_post_is_created(self, client, linkedin):
        response = _create(client, linkedin, recurrence={"frequency": "daily", "count": 3, "end_date": "2099-01-01"})

        assert response.status_code == 422
        assert "not both" in response.json()["detail"]
        assert not Post.objects.exists()

    def test_schema_bounds(self, client, linkedin):
        assert _create(client, linkedin, recurrence={"frequency": "hourly"}).status_code == 422
        assert _create(client, linkedin, recurrence={"frequency": "daily", "interval": 0}).status_code == 422

    def test_patch_changes_then_stops_the_series(self, client, linkedin):
        post_id = _create(client, linkedin, recurrence={"frequency": "daily", "count": 3}).json()["id"]

        changed = _patch(client, post_id, {"recurrence": {"frequency": "daily", "interval": 2, "count": 4}})
        assert changed.status_code == 200, changed.content
        assert changed.json()["recurrence"]["summary"] == "Every 2 days, 4 times"
        assert _copies(post_id).count() == 3

        stopped = _patch(client, post_id, {"recurrence": {"frequency": "never"}})
        assert stopped.status_code == 200, stopped.content
        assert stopped.json()["recurrence"] is None
        assert not _copies(post_id).exists()
        assert RecurrenceRule.objects.get(post_id=post_id).is_active is False

    def test_patch_cannot_turn_a_copy_into_its_own_series(self, client, linkedin):
        post_id = _create(client, linkedin, recurrence={"frequency": "daily", "count": 2}).json()["id"]
        copy = _copies(post_id).get()

        response = _patch(client, copy.id, {"recurrence": {"frequency": "weekly"}})

        assert response.status_code == 422
        assert post_id in response.json()["detail"]

    def test_starting_a_series_needs_publish_directly(self, user, workspace, linkedin):
        post_id = _create(_client(user, workspace, [linkedin]), linkedin).json()["id"]
        limited = _client(user, workspace, [linkedin], permissions=["create_posts"])

        response = _patch(limited, post_id, {"recurrence": {"frequency": "daily"}})

        assert response.status_code == 403

    def test_schedule_route_accepts_a_repeat(self, client, linkedin):
        draft_id = _create(client, linkedin, action="draft").json()["id"]

        response = client.post(
            f"/api/v1/posts/{draft_id}/schedule",
            data=json.dumps({"scheduled_at": _when(), "recurrence": {"frequency": "daily", "count": 3}}),
            content_type="application/json",
        )

        assert response.status_code == 200, response.content
        assert response.json()["recurrence"]["count"] == 3
        assert _copies(draft_id).count() == 2


@pytest.mark.django_db
class TestRestMediaRules:
    def test_scheduling_media_that_breaks_a_rule_is_refused(self, client, workspace, instagram):
        tall = _image(workspace, width=1080, height=1920)

        response = _create(client, instagram, media_asset_ids=[str(tall.id)])

        assert response.status_code == 422, response.content
        assert "between 4:5 and 1.91:1" in response.json()["detail"]
        assert not Post.objects.exists()

    def test_a_draft_is_accepted_and_refused_at_schedule(self, client, workspace, instagram):
        tall = _image(workspace, width=1080, height=1920)
        draft = _create(client, instagram, action="draft", media_asset_ids=[str(tall.id)])
        assert draft.status_code == 201, draft.content

        response = client.post(
            f"/api/v1/posts/{draft.json()['id']}/schedule",
            data=json.dumps({"scheduled_at": _when()}),
            content_type="application/json",
        )

        assert response.status_code == 422
        assert "between 4:5 and 1.91:1" in response.json()["detail"]

    def test_patching_a_scheduled_post_to_bad_media_is_refused(self, client, workspace, instagram):
        post_id = _create(client, instagram, media_asset_ids=[str(_image(workspace).id)]).json()["id"]

        response = _patch(client, post_id, {"media_asset_ids": [str(_image(workspace, width=1080, height=1920).id)]})

        assert response.status_code == 422
        assert "between 4:5 and 1.91:1" in response.json()["detail"]

    def test_media_within_the_rules_is_accepted(self, client, workspace, instagram):
        portrait = _image(workspace, width=1080, height=1350)
        assert _create(client, instagram, media_asset_ids=[str(portrait.id)]).status_code == 201


@pytest.mark.django_db
class TestMcp:
    def test_schedule_post_with_a_repeat(self, client, linkedin):
        result = _mcp(
            client,
            "schedule_post",
            {
                "social_account_id": str(linkedin.id),
                "caption": "hi",
                "scheduled_at": _when(),
                "recurrence": {"frequency": "weekly", "count": 3},
            },
        )

        assert "error" not in result, result
        body = json.loads(result["result"]["content"][0]["text"])
        assert body["recurrence"]["count"] == 3
        assert _copies(body["id"]).count() == 2

    def test_schedule_post_refuses_an_invalid_repeat(self, client, linkedin):
        result = _mcp(
            client,
            "schedule_post",
            {
                "social_account_id": str(linkedin.id),
                "caption": "hi",
                "scheduled_at": _when(),
                "recurrence": {"frequency": "daily", "count": 1},
            },
        )

        assert result["error"]["code"] == INVALID_PARAMS
        assert not Post.objects.exists()

    def test_schedule_draft_with_a_repeat(self, client, linkedin):
        draft_id = _create(client, linkedin, action="draft").json()["id"]

        result = _mcp(
            client,
            "schedule_draft",
            {"post_id": draft_id, "scheduled_at": _when(), "recurrence": {"frequency": "daily", "count": 2}},
        )

        assert "error" not in result, result
        assert _copies(draft_id).count() == 1

    def test_schedule_post_refuses_media_that_breaks_a_rule(self, client, workspace, instagram):
        tall = _image(workspace, width=1080, height=1920)

        result = _mcp(
            client,
            "schedule_post",
            {
                "social_account_id": str(instagram.id),
                "caption": "hi",
                "scheduled_at": _when(),
                "media_asset_ids": [str(tall.id)],
            },
        )

        assert result["error"]["code"] == INVALID_PARAMS
        assert "between 4:5 and 1.91:1" in result["error"]["message"]

    def test_tools_advertise_the_recurrence_argument(self, client):
        rpc = {"jsonrpc": "2.0", "method": "tools/list", "id": 1}
        tools = {
            t["name"]: t
            for t in client.post(MCP_URL, data=json.dumps(rpc), content_type="application/json").json()["result"][
                "tools"
            ]
        }
        for name in ("schedule_post", "schedule_draft"):
            assert "recurrence" in tools[name]["inputSchema"]["properties"]
