"""Threads over the Agent API and MCP: ``parts`` on create / PATCH, and resuming.

``parts`` is additive and optional — a client that never sends it creates the
same single post it always did — and every check the composer makes on a
thread applies here too, through the same service layer.
"""

from __future__ import annotations

import json
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.test import Client
from django.utils import timezone

from apps.api_keys import services
from apps.composer.models import PlatformPost, PlatformPostPart, Post, PostPart
from apps.members.models import PERMISSION_KEYS, OrgMembership, WorkspaceMembership

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

    return User.objects.create_user(
        email="threads@example.com", password="testpass123", name="Threads", tos_accepted_at=timezone.now()
    )


@pytest.fixture
def workspace(db, user):
    from apps.organizations.models import Organization
    from apps.workspaces.models import Workspace

    organization = Organization.objects.create(name="Threads Org")
    workspace = Workspace.objects.create(name="Threads WS", organization=organization)
    OrgMembership.objects.create(user=user, organization=organization, org_role=OrgMembership.OrgRole.OWNER)
    WorkspaceMembership.objects.create(
        user=user, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
    )
    return workspace


def _account(workspace, platform):
    from apps.social_accounts.models import SocialAccount

    return SocialAccount.objects.create(
        workspace=workspace,
        platform=platform,
        account_platform_id=f"{platform}-api",
        account_name=f"{platform} account",
        connection_status="connected",
    )


@pytest.fixture
def bluesky(workspace):
    return _account(workspace, "bluesky")


@pytest.fixture
def google(workspace):
    return _account(workspace, "google_business")


@pytest.fixture
def client(user, workspace, bluesky, google):
    key = services.issue_api_key(
        workspace=workspace,
        social_accounts=[bluesky, google],
        issued_by=user,
        name="threads",
        permissions=list(PERMISSION_KEYS),
    )
    return _SecureClient(HTTP_AUTHORIZATION=f"Bearer {key.plaintext_token}")


def _when():
    return (timezone.now() + timedelta(days=1)).isoformat()


def _create(client, body):
    return client.post("/api/v1/posts/", data=json.dumps(body), content_type="application/json")


def _mcp(client, name, arguments):
    response = client.post(
        MCP_URL,
        data=json.dumps(
            {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": name, "arguments": arguments}, "id": 1}
        ),
        content_type="application/json",
    )
    assert response.status_code == 200
    return response.json()


@pytest.mark.django_db
class TestRestParts:
    def test_a_post_without_parts_is_unchanged(self, client, bluesky):
        r = _create(client, {"social_account_id": str(bluesky.id), "caption": "one"})
        assert r.status_code == 201, r.content
        assert r.json()["parts"] == []
        assert r.json()["platform_posts"][0]["thread_status"] == "none"

    def test_create_schedules_a_thread_with_overrides_and_delays(self, client, bluesky):
        r = _create(
            client,
            {
                "social_account_id": str(bluesky.id),
                "caption": "one",
                "action": "schedule",
                "scheduled_at": _when(),
                "parts": [
                    {"caption": "Follow for more", "is_finisher": True},
                    {
                        "caption": "two",
                        "delay_seconds": 120,
                        "platform_overrides": [{"social_account_id": str(bluesky.id), "caption": "bsky two"}],
                    },
                ],
            },
        )
        assert r.status_code == 201, r.content
        parts = r.json()["parts"]
        assert [(p["position"], p["caption"], p["delay_seconds"]) for p in parts] == [
            (2, "two", 120),
            (3, "Follow for more", 0),
        ]
        assert PlatformPostPart.objects.get().platform_specific_caption == "bsky two"

    def test_scheduling_a_thread_the_platform_cant_carry_is_422(self, client, google):
        r = _create(
            client,
            {
                "social_account_id": str(google.id),
                "caption": "one",
                "action": "schedule",
                "scheduled_at": _when(),
                "parts": [{"caption": "two"}],
            },
        )
        assert r.status_code == 422
        assert "can't publish a thread" in r.json()["detail"]
        assert not Post.objects.exists()

    def test_a_part_over_the_limit_is_422(self, client, bluesky):
        r = _create(
            client,
            {
                "social_account_id": str(bluesky.id),
                "caption": "one",
                "action": "schedule",
                "scheduled_at": _when(),
                "parts": [{"caption": "x" * 301}],
            },
        )
        assert r.status_code == 422
        assert "300 per post" in r.json()["detail"]

    def test_an_override_for_another_account_is_422(self, client, bluesky, google):
        r = _create(
            client,
            {
                "social_account_id": str(bluesky.id),
                "caption": "one",
                "parts": [{"caption": "two", "platform_overrides": [{"social_account_id": str(google.id)}]}],
            },
        )
        assert r.status_code == 422

    def test_patch_replaces_and_clears_parts(self, client, bluesky):
        post_id = _create(
            client, {"social_account_id": str(bluesky.id), "caption": "one", "parts": [{"caption": "a"}]}
        ).json()["id"]

        r = client.patch(
            f"/api/v1/posts/{post_id}",
            data=json.dumps({"parts": [{"caption": "b"}, {"caption": "c"}]}),
            content_type="application/json",
        )
        assert r.status_code == 200, r.content
        assert [p["caption"] for p in r.json()["parts"]] == ["b", "c"]

        # Omitting parts leaves them alone.
        r = client.patch(
            f"/api/v1/posts/{post_id}", data=json.dumps({"caption": "new"}), content_type="application/json"
        )
        assert len(r.json()["parts"]) == 2

        r = client.patch(f"/api/v1/posts/{post_id}", data=json.dumps({"parts": []}), content_type="application/json")
        assert r.json()["parts"] == []

    def test_patch_cannot_strand_a_scheduled_thread(self, client, bluesky):
        post_id = _create(
            client,
            {"social_account_id": str(bluesky.id), "caption": "one", "action": "schedule", "scheduled_at": _when()},
        ).json()["id"]
        r = client.patch(
            f"/api/v1/posts/{post_id}",
            data=json.dumps({"parts": [{"caption": "x" * 301}]}),
            content_type="application/json",
        )
        assert r.status_code == 422
        assert not PostPart.objects.exists()

    def test_accounts_report_their_thread_capability(self, client, bluesky, google):
        r = client.get("/api/v1/accounts/")
        assert r.status_code == 200, r.content
        by_platform = {a["platform"]: a for a in r.json()["accounts"]}
        assert by_platform["bluesky"]["thread_mode"] == "reply_chain"
        assert by_platform["bluesky"]["thread_part_max_length"] == 300
        assert by_platform["bluesky"]["thread_media_per_part"] is True
        assert by_platform["google_business"]["thread_mode"] == "none"

    def test_resume_thread_restarts_at_the_stopped_part(self, client, bluesky):
        post_id = _create(
            client, {"social_account_id": str(bluesky.id), "caption": "one", "parts": [{"caption": "two"}]}
        ).json()["id"]
        pp = PlatformPost.objects.get(post_id=post_id)
        part = PostPart.objects.get()
        PlatformPost.objects.filter(pk=pp.pk).update(
            status="published", platform_post_id="at://root", thread_status=PlatformPost.ThreadStatus.PARTIAL
        )
        PlatformPostPart.objects.create(platform_post=pp, part=part, status=PlatformPostPart.Status.FAILED)

        with patch("apps.publisher.engine._publish_thread_part_task") as task:
            r = client.post(f"/api/v1/posts/{post_id}/resume-thread")
        assert r.status_code == 200, r.content
        task.assert_called_once_with(str(pp.id), str(part.id), schedule=0)
        summary = r.json()["platform_posts"][0]
        assert summary["thread_status"] == "pending"
        assert summary["thread_parts"][0]["status"] == "pending"

        # Nothing left stopped: a second resume is a conflict, not a re-send.
        assert client.post(f"/api/v1/posts/{post_id}/resume-thread").status_code == 409

    def test_resume_of_an_unconfirmed_part_needs_confirm_unconfirmed(self, client, bluesky):
        post_id = _create(
            client, {"social_account_id": str(bluesky.id), "caption": "one", "parts": [{"caption": "two"}]}
        ).json()["id"]
        pp = PlatformPost.objects.get(post_id=post_id)
        part = PostPart.objects.get()
        PlatformPost.objects.filter(pk=pp.pk).update(
            status="published", platform_post_id="at://root", thread_status=PlatformPost.ThreadStatus.PARTIAL
        )
        PlatformPostPart.objects.create(platform_post=pp, part=part, status=PlatformPostPart.Status.UNCONFIRMED)

        with patch("apps.publisher.engine._publish_thread_part_task") as task:
            refused = client.post(f"/api/v1/posts/{post_id}/resume-thread")
            assert refused.status_code == 409
            assert "may already be live" in refused.json()["detail"]
            task.assert_not_called()
            r = client.post(f"/api/v1/posts/{post_id}/resume-thread?confirm_unconfirmed=true")
        assert r.status_code == 200, r.content
        task.assert_called_once_with(str(pp.id), str(part.id), schedule=0)


@pytest.mark.django_db
class TestMcpParts:
    def test_create_draft_takes_parts(self, client, bluesky):
        envelope = _mcp(
            client,
            "create_draft",
            {
                "social_account_id": str(bluesky.id),
                "caption": "one",
                "parts": [{"caption": "two", "delay_seconds": 30}],
            },
        )
        assert "error" not in envelope, envelope
        body = json.loads(envelope["result"]["content"][0]["text"])
        assert [(p["caption"], p["delay_seconds"]) for p in body["parts"]] == [("two", 30)]

    def test_schedule_post_refuses_a_thread_the_platform_cant_carry(self, client, google):
        envelope = _mcp(
            client,
            "schedule_post",
            {
                "social_account_id": str(google.id),
                "caption": "one",
                "scheduled_at": _when(),
                "parts": [{"caption": "two"}],
            },
        )
        assert "can't publish a thread" in json.dumps(envelope)
        assert not Post.objects.exists()

    def test_invalid_parts_are_refused(self, client, bluesky):
        envelope = _mcp(
            client,
            "create_draft",
            {"social_account_id": str(bluesky.id), "caption": "one", "parts": [{"delay_seconds": -1}]},
        )
        # Refused against the tool's input schema before the handler runs.
        assert "error" in envelope
        assert "delay_seconds" in envelope["error"]["message"]

    def test_resume_thread_tool(self, client, bluesky):
        envelope = _mcp(
            client,
            "create_draft",
            {"social_account_id": str(bluesky.id), "caption": "one", "parts": [{"caption": "two"}]},
        )
        post_id = json.loads(envelope["result"]["content"][0]["text"])["id"]
        assert "No thread on this post is stopped" in json.dumps(_mcp(client, "resume_thread", {"post_id": post_id}))

        PlatformPost.objects.filter(post_id=post_id).update(
            status="published", platform_post_id="at://root", thread_status=PlatformPost.ThreadStatus.PARTIAL
        )
        with patch("apps.publisher.engine._publish_thread_part_task") as task:
            envelope = _mcp(client, "resume_thread", {"post_id": post_id})
        assert "error" not in envelope, envelope
        task.assert_called_once()

    def test_resume_thread_tool_needs_confirm_unconfirmed_for_an_unconfirmed_part(self, client, bluesky):
        envelope = _mcp(
            client,
            "create_draft",
            {"social_account_id": str(bluesky.id), "caption": "one", "parts": [{"caption": "two"}]},
        )
        post_id = json.loads(envelope["result"]["content"][0]["text"])["id"]
        pp = PlatformPost.objects.get(post_id=post_id)
        PlatformPost.objects.filter(pk=pp.pk).update(
            status="published", platform_post_id="at://root", thread_status=PlatformPost.ThreadStatus.PARTIAL
        )
        PlatformPostPart.objects.create(
            platform_post=pp, part=PostPart.objects.get(), status=PlatformPostPart.Status.UNCONFIRMED
        )
        with patch("apps.publisher.engine._publish_thread_part_task") as task:
            assert "may already be live" in json.dumps(_mcp(client, "resume_thread", {"post_id": post_id}))
            task.assert_not_called()
            envelope = _mcp(client, "resume_thread", {"post_id": post_id, "confirm_unconfirmed": True})
        assert "error" not in envelope, envelope
        task.assert_called_once()
