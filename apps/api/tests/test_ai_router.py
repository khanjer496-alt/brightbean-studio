"""``/api/v1/ai/*`` — generate and adapt captions with the AI writing assistant."""

from __future__ import annotations

import pytest
from django.test import Client
from django.utils import timezone

from apps.ai_writer.models import AIRequestLog
from apps.ai_writer.tests.fakes import patch_anthropic
from apps.api_keys import services
from apps.members.models import PERMISSION_KEYS, OrgMembership, WorkspaceMembership


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
        email="ai-agent@example.com", password="testpass123", name="AI Agent", tos_accepted_at=timezone.now()
    )


@pytest.fixture
def workspace(db, user):
    from apps.organizations.models import Organization
    from apps.workspaces.models import Workspace

    organization = Organization.objects.create(name="AI Org")
    workspace = Workspace.objects.create(name="AI WS", organization=organization)
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
    key = services.issue_api_key(
        workspace=workspace, social_accounts=accounts, issued_by=user, name="ai", permissions=permissions
    )
    return _SecureClient(HTTP_AUTHORIZATION=f"Bearer {key.plaintext_token}")


@pytest.fixture
def client(user, workspace, bluesky, facebook):
    return _client(user, workspace, [bluesky, facebook], list(PERMISSION_KEYS))


@pytest.mark.django_db
class TestGenerateCaption:
    def test_writes_within_the_accounts_limit(self, platform_key, client, bluesky, fake_claude):
        fake_claude.queue({"text": "New blend, who dis ☕"})
        r = client.post(
            "/api/v1/ai/caption",
            data={"prompt": "new coffee blend", "social_account_ids": [str(bluesky.id)]},
            content_type="application/json",
        )
        assert r.status_code == 200, r.content
        assert r.json() == {"caption": "New blend, who dis ☕"}
        assert "at most 300 characters" in fake_claude.prompts[0]
        assert AIRequestLog.objects.get().source == "api"

    def test_404_when_ai_is_off(self, client, fake_claude):
        r = client.post("/api/v1/ai/caption", data={"prompt": "x"}, content_type="application/json")
        assert r.status_code == 404
        assert r.json()["error"] == "not_found"
        assert fake_claude.calls == []

    def test_account_outside_the_allowlist(self, platform_key, client, outsider):
        r = client.post(
            "/api/v1/ai/caption",
            data={"prompt": "x", "social_account_ids": [str(outsider.id)]},
            content_type="application/json",
        )
        assert r.status_code == 403

    def test_needs_create_posts(self, platform_key, user, workspace, bluesky):
        c = _client(user, workspace, [bluesky], ["view_analytics"])
        r = c.post("/api/v1/ai/caption", data={"prompt": "x"}, content_type="application/json")
        assert r.status_code == 403

    def test_daily_allowance_is_402(self, platform_key, client, workspace, settings):
        from apps.billing.tests.support import set_plan

        settings.BILLING_ENABLED = True
        set_plan(workspace.organization, "free")
        AIRequestLog.objects.bulk_create(
            AIRequestLog(organization=workspace.organization, action="write", provider="anthropic", model="m")
            for _ in range(20)
        )
        r = client.post("/api/v1/ai/caption", data={"prompt": "x"}, content_type="application/json")
        assert r.status_code == 402
        assert r.json()["code"] == "ai_request_limit"

    def test_provider_busy_is_503_not_our_429(self, platform_key, client, fake_claude):
        import anthropic
        import httpx2

        request = httpx2.Request("POST", "https://api.anthropic.com")
        fake_claude.queue(anthropic.RateLimitError("x", response=httpx2.Response(429, request=request), body=None))
        r = client.post("/api/v1/ai/caption", data={"prompt": "x"}, content_type="application/json")
        assert r.status_code == 503


@pytest.mark.django_db
class TestAdaptCaption:
    def test_one_caption_per_account(self, platform_key, client, bluesky, facebook, fake_claude):
        fake_claude.queue(
            {
                "captions": [
                    {"account_id": str(bluesky.id), "caption": "Sky"},
                    {"account_id": str(facebook.id), "caption": "Page"},
                ]
            }
        )
        r = client.post(
            "/api/v1/ai/adapt",
            data={"caption": "Launch", "social_account_ids": [str(bluesky.id), str(facebook.id)]},
            content_type="application/json",
        )
        assert r.status_code == 200, r.content
        rows = r.json()["captions"]
        assert [(row["platform"], row["caption"], row["char_limit"]) for row in rows] == [
            ("bluesky", "Sky", 300),
            ("facebook", "Page", bluesky.PLATFORM_CHAR_LIMITS["facebook"]),
        ]

    def test_requires_accounts(self, platform_key, client):
        r = client.post(
            "/api/v1/ai/adapt", data={"caption": "Launch", "social_account_ids": []}, content_type="application/json"
        )
        assert r.status_code == 422
