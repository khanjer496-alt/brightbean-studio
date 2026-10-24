"""``/api/v1/repurpose/jobs/*``: create, read, accept and cancel a repurposing job."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.test import Client
from django.utils import timezone

from apps.ai_writer.models import AIRequestLog
from apps.ai_writer.tests.fakes import patch_anthropic
from apps.api_keys import services as key_services
from apps.composer.models import Post
from apps.members.models import PERMISSION_KEYS, OrgMembership, WorkspaceMembership
from apps.repurpose import services
from apps.repurpose.models import RepurposeJob

pytestmark = pytest.mark.django_db

TEXT = "Slow roasting keeps the sweetness in the bean. We roast every batch for eighteen minutes at lower heat."
BASE = "/api/v1/repurpose/jobs"


class _SecureClient(Client):
    def generic(self, method, path, *args, **kwargs):
        kwargs["secure"] = True
        return super().generic(method, path, *args, **kwargs)


@pytest.fixture(autouse=True)
def fake_claude():
    with patch_anthropic() as fake:
        yield fake


@pytest.fixture
def platform_key(settings):
    settings.ANTHROPIC_API_KEY = "sk-ant-platform-key-for-tests"


@pytest.fixture
def user(db):
    from apps.accounts.models import User

    return User.objects.create_user(
        email="rp-agent@example.com", password="testpass123", name="Agent", tos_accepted_at=timezone.now()
    )


@pytest.fixture
def workspace(db, user):
    from apps.organizations.models import Organization
    from apps.workspaces.models import Workspace

    organization = Organization.objects.create(name="RP Org")
    workspace = Workspace.objects.create(name="RP WS", organization=organization)
    OrgMembership.objects.create(user=user, organization=organization, org_role=OrgMembership.OrgRole.OWNER)
    WorkspaceMembership.objects.create(
        user=user, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
    )
    return workspace


def _account(workspace, platform, platform_id):
    from apps.social_accounts.models import SocialAccount

    return SocialAccount.objects.create(
        workspace=workspace,
        platform=platform,
        account_platform_id=platform_id,
        account_name=platform,
        connection_status="connected",
        oauth_access_token="tok",
    )


@pytest.fixture
def bluesky(workspace):
    return _account(workspace, "bluesky", "b-1")


@pytest.fixture
def facebook(workspace):
    return _account(workspace, "facebook", "f-1")


@pytest.fixture
def outsider(workspace):
    """In the workspace but not in the key's allowlist."""
    return _account(workspace, "threads", "t-1")


def _client(user, workspace, accounts, permissions):
    key = key_services.issue_api_key(
        workspace=workspace, social_accounts=accounts, issued_by=user, name="rp", permissions=permissions
    )
    return _SecureClient(HTTP_AUTHORIZATION=f"Bearer {key.plaintext_token}")


@pytest.fixture
def client(user, workspace, bluesky, facebook):
    return _client(user, workspace, [bluesky, facebook], list(PERMISSION_KEYS))


def _create(client, **body):
    return client.post(BASE, data=body, content_type="application/json")


def test_create_run_read_accept(platform_key, client, bluesky, fake_claude, workspace):
    fake_claude.queue({"parts": ["Hook.", "Second."]})
    r = _create(
        client,
        source_type="text",
        text=TEXT,
        outputs=[{"kind": "thread", "social_account_ids": [str(bluesky.id)]}],
    )
    assert r.status_code == 202, r.content
    body = r.json()
    assert body["status"] == "queued" and body["ai_requests_reserved"] == 1 and body["outputs"] == []
    job = RepurposeJob.objects.get()
    assert job.origin == "api"

    services.execute_job(job.id)
    body = client.get(f"{BASE}/{job.id}").json()
    assert body["status"] == "succeeded" and body["ai_requests_made"] == 1
    out = body["outputs"][0]
    assert out["text"] == "Hook." and out["parts"] == ["Second."] and out["char_limit"] == 300
    assert AIRequestLog.objects.get().source == "api"

    r = client.post(f"{BASE}/{job.id}/accept", data={"output_ids": [out["id"]]}, content_type="application/json")
    assert r.status_code == 200, r.content
    accepted = r.json()["outputs"][0]
    assert accepted["status"] == "accepted" and accepted["post_id"]
    assert Post.objects.get().platform_posts.get().status == "draft"
    assert r.json()["autonomy"]["decision"] == "executed"
    # A retry is a no-op, not a second draft.
    again = client.post(f"{BASE}/{job.id}/accept", data={"output_ids": [out["id"]]}, content_type="application/json")
    assert again.status_code == 200 and again.json()["outputs"][0]["post_id"] == accepted["post_id"]
    assert Post.objects.count() == 1


class TestAutonomy:
    """Agent-created runs and accepted outputs go through the policy for drafting, and leave a trail."""

    def _thread_job(self, client, bluesky, fake_claude, text=TEXT):
        fake_claude.queue({"parts": [text[:200], "Second."]})
        r = _create(
            client, source_type="text", text=TEXT, outputs=[{"kind": "thread", "social_account_ids": [str(bluesky.id)]}]
        )
        assert r.status_code == 202, r.content
        job = RepurposeJob.objects.get()
        services.execute_job(job.id)
        return job, r.json()

    def test_drafting_off_refuses_the_run_before_any_ai_request(self, platform_key, client, bluesky, workspace):
        from apps.autonomy.models import AgentAction, AutonomyPolicy

        AutonomyPolicy.objects.update_or_create(workspace=workspace, defaults={"draft_level": "off"})
        r = _create(
            client, source_type="text", text=TEXT, outputs=[{"kind": "thread", "social_account_ids": [str(bluesky.id)]}]
        )
        assert r.status_code == 403 and "Blocked because" in r.json()["detail"]
        assert RepurposeJob.objects.count() == 0 and AIRequestLog.objects.count() == 0
        action = AgentAction.objects.get()
        assert action.decision == "blocked" and action.operation == "repurpose.create"

    def test_create_and_accept_are_logged_with_the_draft_linked(
        self, platform_key, client, bluesky, fake_claude, workspace
    ):
        from apps.autonomy.models import AgentAction

        job, created = self._thread_job(client, bluesky, fake_claude)
        assert created["autonomy"]["decision"] == "executed"
        out_id = str(job.outputs.get().id)
        r = client.post(f"{BASE}/{job.id}/accept", data={"output_ids": [out_id]}, content_type="application/json")
        assert r.status_code == 200
        accept = AgentAction.objects.get(operation="repurpose.accept")
        assert accept.action_type == "draft_content" and accept.post_id == Post.objects.get().id
        assert "Hook" not in accept.final_text and TEXT[:40] in accept.final_text

    def test_crisis_pause_blocks_accept_and_the_refusal_survives_the_rollback(
        self, platform_key, client, bluesky, fake_claude, workspace
    ):
        from apps.autonomy.models import AgentAction, AutonomyPolicy

        job, _ = self._thread_job(client, bluesky, fake_claude)
        AutonomyPolicy.objects.update_or_create(workspace=workspace, defaults={"crisis_paused": True})
        out_id = str(job.outputs.get().id)
        r = client.post(f"{BASE}/{job.id}/accept", data={"output_ids": [out_id]}, content_type="application/json")
        assert r.status_code == 403 and "crisis pause" in r.json()["detail"]
        assert Post.objects.count() == 0 and job.outputs.get().status == "generated"
        assert AgentAction.objects.filter(operation="repurpose.accept", decision="blocked").count() == 1

    def test_a_blocked_phrase_is_flagged_for_review_under_draft_only(
        self, platform_key, client, bluesky, fake_claude, workspace
    ):
        from apps.autonomy.models import AutonomyPolicy

        AutonomyPolicy.objects.update_or_create(
            workspace=workspace, defaults={"draft_level": "draft_only", "blocked_phrases": ["eighteen minutes"]}
        )
        job, _ = self._thread_job(client, bluesky, fake_claude)
        out_id = str(job.outputs.get().id)
        r = client.post(f"{BASE}/{job.id}/accept", data={"output_ids": [out_id]}, content_type="application/json")
        assert r.status_code == 200
        notice = r.json()["autonomy"]
        assert notice["decision"] == "executed" and any("flagged" in reason for reason in notice["reasons"])
        assert Post.objects.get().platform_posts.get().status == "draft"


def test_cancel(platform_key, client, bluesky, fake_claude):
    r = _create(client, source_type="text", text=TEXT, outputs=[{"kind": "newsletter"}])
    job_id = r.json()["id"]
    r = client.post(f"{BASE}/{job_id}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert client.post(f"{BASE}/{job_id}/cancel").status_code == 409
    assert fake_claude.calls == []


def test_idempotent_create(platform_key, client):
    kwargs = {"HTTP_IDEMPOTENCY_KEY": "k-1"}
    body = {"source_type": "text", "text": TEXT, "outputs": [{"kind": "newsletter"}]}
    a = client.post(BASE, data=body, content_type="application/json", **kwargs)
    b = client.post(BASE, data=body, content_type="application/json", **kwargs)
    assert a.json()["id"] == b.json()["id"] and RepurposeJob.objects.count() == 1


def test_account_outside_the_allowlist(platform_key, client, outsider):
    r = _create(
        client, source_type="text", text=TEXT, outputs=[{"kind": "post", "social_account_ids": [str(outsider.id)]}]
    )
    assert r.status_code == 403
    assert RepurposeJob.objects.count() == 0
    assert RepurposeJob.objects.count() == 0


def test_job_is_invisible_to_a_key_that_lacks_one_of_its_accounts(
    platform_key, client, user, workspace, bluesky, facebook, fake_claude
):
    r = _create(
        client,
        source_type="text",
        text=TEXT,
        outputs=[{"kind": "post", "social_account_ids": [str(bluesky.id), str(facebook.id)]}],
    )
    job_id = r.json()["id"]
    narrow = _client(user, workspace, [bluesky], list(PERMISSION_KEYS))
    assert narrow.get(f"{BASE}/{job_id}").status_code == 404
    assert narrow.post(f"{BASE}/{job_id}/cancel").status_code == 404
    assert client.get(f"{BASE}/{job_id}").status_code == 200


def test_another_workspaces_job_is_a_404(platform_key, client, organization_other_job):
    assert client.get(f"{BASE}/{organization_other_job.id}").status_code == 404


@pytest.fixture
def organization_other_job(db, workspace):
    from apps.workspaces.models import Workspace

    other = Workspace.objects.create(organization=workspace.organization, name="Other")
    return RepurposeJob.objects.create(workspace=other, source_type="text", source_text=TEXT)


def test_needs_create_posts(platform_key, user, workspace, bluesky):
    c = _client(user, workspace, [bluesky], ["view_analytics"])
    r = _create(c, source_type="text", text=TEXT, outputs=[{"kind": "newsletter"}])
    assert r.status_code == 403


def test_ai_off_is_404_and_hosted_mode_ignores_the_platform_key(client, workspace, settings, platform_key):
    settings.ANTHROPIC_API_KEY = ""
    assert _create(client, source_type="text", text=TEXT, outputs=[{"kind": "newsletter"}]).status_code == 404
    settings.ANTHROPIC_API_KEY = "sk-ant-platform-key-for-tests"
    settings.AI_PLATFORM_KEY_FOR_CUSTOMERS = False
    r = _create(client, source_type="text", text=TEXT, outputs=[{"kind": "newsletter"}])
    assert r.status_code == 404 and RepurposeJob.objects.count() == 0


def test_video_without_transcript_says_so(platform_key, client):
    r = _create(client, source_type="video", url="https://youtu.be/dQw4w9WgXcQ", outputs=[{"kind": "newsletter"}])
    assert r.status_code == 422 and "transcript_needed" in str(r.json())


def test_pdf_source_comes_from_the_media_library(platform_key, client, workspace, user, fake_claude):
    from django.core.files.uploadedfile import SimpleUploadedFile
    from fpdf import FPDF

    from apps.media_library.services import create_asset

    pdf = FPDF()
    pdf.set_font("Helvetica", size=12)
    pdf.add_page()
    pdf.multi_cell(0, 8, TEXT)
    asset = create_asset(
        workspace.organization, workspace, SimpleUploadedFile("g.pdf", bytes(pdf.output()), "application/pdf"), user
    )
    r = _create(client, source_type="pdf", media_asset_id=str(asset.id), outputs=[{"kind": "newsletter"}])
    assert r.status_code == 202, r.content
    assert RepurposeJob.objects.get().source_label == "g.pdf"
    missing = _create(
        client,
        source_type="pdf",
        media_asset_id="00000000-0000-0000-0000-000000000000",
        outputs=[{"kind": "newsletter"}],
    )
    assert missing.status_code == 404


def test_post_source_must_be_visible_and_published(platform_key, client, workspace, bluesky):
    from apps.composer.models import PlatformPost
    from apps.composer.services import create_post

    post = create_post(workspace=workspace, social_account=bluesky, caption=TEXT)
    r = _create(client, source_type="post", post_id=str(post.id), outputs=[{"kind": "newsletter"}])
    assert r.status_code == 422  # a draft can't be remixed
    PlatformPost.objects.filter(post=post).update(status="published")
    assert (
        _create(client, source_type="post", post_id=str(post.id), outputs=[{"kind": "newsletter"}]).status_code == 202
    )


def test_url_source_goes_through_the_safe_fetcher(platform_key, client):
    with patch("apps.composer.views.httpx.stream") as stream:
        r = _create(client, source_type="url", url="http://127.0.0.1/admin", outputs=[{"kind": "newsletter"}])
    assert r.status_code == 422
    stream.assert_not_called()
