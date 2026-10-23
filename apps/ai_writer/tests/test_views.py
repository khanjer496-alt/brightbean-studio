"""The composer's AI panel, its HTMX endpoints, and the two settings pages."""

from __future__ import annotations

import json
import re

import pytest
from django.urls import reverse

from apps.ai_writer.models import AIRequestLog, OrganizationAIKey
from apps.composer.models import Idea

pytestmark = pytest.mark.django_db


def _url(name, workspace):
    return reverse(f"ai_writer:{name}", kwargs={"workspace_id": workspace.id})


class TestComposerPanel:
    def test_hidden_without_a_key(self, owner_client, workspace):
        response = owner_client.get(reverse("composer:compose", kwargs={"workspace_id": workspace.id}))
        assert response.status_code == 200
        assert b"AI Assist" not in response.content
        assert b'id="ai-panel"' not in response.content

    def test_shown_with_the_platform_key(self, platform_key, owner_client, workspace):
        response = owner_client.get(reverse("composer:compose", kwargs={"workspace_id": workspace.id}))
        assert b'id="ai-panel"' in response.content
        assert _url("write", workspace).encode() in response.content

    def test_shown_with_the_organizations_own_key(self, owner_client, workspace, organization):
        OrganizationAIKey.objects.create(organization=organization, api_key="sk-ant-own-key-000000")
        response = owner_client.get(reverse("composer:compose", kwargs={"workspace_id": workspace.id}))
        assert b'id="ai-panel"' in response.content

    def test_viewers_cannot_spend_ai_requests(self, platform_key, workspace, organization, fake_claude):
        from django.test import Client
        from django.utils import timezone

        from apps.accounts.models import User
        from apps.members.models import OrgMembership, WorkspaceMembership

        viewer = User.objects.create_user(
            email="viewer@example.com", password="testpass123", name="Viewer", tos_accepted_at=timezone.now()
        )
        OrgMembership.objects.filter(user=viewer).exclude(organization=organization).delete()
        OrgMembership.objects.create(user=viewer, organization=organization, org_role=OrgMembership.OrgRole.MEMBER)
        WorkspaceMembership.objects.create(
            user=viewer, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.VIEWER
        )
        client = Client()
        client.force_login(viewer)
        for name in ("write", "improve", "adapt", "hashtags", "link", "link_save", "thread"):
            assert client.post(_url(name, workspace), {"ai_prompt": "x", "caption": "x"}).status_code == 403
        assert fake_claude.calls == []

    def test_actions_need_the_csrf_token(self, platform_key, owner, workspace, fake_claude):
        from django.test import Client

        client = Client(enforce_csrf_checks=True)
        client.force_login(owner)
        assert client.post(_url("write", workspace), {"ai_prompt": "x"}).status_code == 403
        assert fake_claude.calls == []

    def test_endpoints_404_without_a_key(self, owner_client, workspace, fake_claude):
        for name in ("write", "improve", "adapt", "hashtags", "link", "link_save", "thread"):
            assert owner_client.post(_url(name, workspace), {"ai_prompt": "x"}).status_code == 404
        assert fake_claude.calls == []


class TestActions:
    def test_write(self, platform_key, owner_client, workspace, bluesky, fake_claude):
        fake_claude.queue({"text": "Spring is here 🌸"})
        response = owner_client.post(
            _url("write", workspace), {"ai_prompt": "spring sale", "selected_accounts": str(bluesky.id)}
        )
        assert response.status_code == 200
        assert "Spring is here 🌸" in response.content.decode()
        assert "Use this caption" in response.content.decode()
        assert "at most 300 characters" in fake_claude.prompts[0]

    def test_improve(self, platform_key, owner_client, workspace, fake_claude):
        fake_claude.queue({"text": "Better"})
        response = owner_client.post(_url("improve", workspace), {"caption": "good", "ai_mode": "punchier"})
        assert response.status_code == 200 and b"Punchier version" in response.content

    def test_adapt_fills_selected_accounts_only(
        self, platform_key, owner_client, workspace, bluesky, linkedin, fake_claude
    ):
        fake_claude.queue({"captions": [{"account_id": str(bluesky.id), "caption": "Sky version"}]})
        response = owner_client.post(
            _url("adapt", workspace), {"caption": "Launch day", "selected_accounts": str(bluesky.id)}
        )
        body = response.content.decode()
        assert response.status_code == 200
        assert "Sky version" in body and 'id="ai-adapted-captions"' in body
        assert str(linkedin.id) not in body

    def test_hashtags(self, platform_key, owner_client, workspace, fake_claude):
        fake_claude.queue({"items": ["#spring", "#sale"]})
        response = owner_client.post(_url("hashtags", workspace), {"caption": "Spring sale"})
        assert b'data-tag="#spring"' in response.content

    def test_link_save_as_ideas(self, platform_key, owner_client, workspace):
        response = owner_client.post(
            _url("link_save", workspace), {"ai_draft": ["Idea one", "Idea two"], "ai_save_as": "ideas"}
        )
        assert response.status_code == 204
        assert "showToast" in response.headers["HX-Trigger"]
        assert Idea.objects.filter(workspace=workspace).count() == 2

    def test_thread_returns_parts(self, platform_key, owner_client, workspace, bluesky, fake_claude):
        fake_claude.queue({"items": ["Part one", "Part two"]})
        response = owner_client.post(
            _url("thread", workspace), {"text": "word " * 100, "social_account_id": str(bluesky.id)}
        )
        assert response.status_code == 200
        assert response.json() == {"parts": ["Part one 1/2", "Part two 2/2"], "char_limit": 300}

    def test_errors_are_plain_text_for_the_inline_message(self, platform_key, owner_client, workspace, fake_claude):
        response = owner_client.post(_url("improve", workspace), {"caption": "", "ai_mode": "punchier"})
        assert response.status_code == 400
        assert response["Content-Type"].startswith("text/plain")
        assert b"Write a caption first" in response.content

    def test_plan_limit_is_402(self, platform_key, owner_client, workspace, organization, settings):
        from apps.billing.tests.support import set_plan

        settings.BILLING_ENABLED = True
        set_plan(organization, "free")
        AIRequestLog.objects.bulk_create(
            AIRequestLog(organization=organization, action="write", provider="anthropic", model="m") for _ in range(20)
        )
        response = owner_client.post(_url("write", workspace), {"ai_prompt": "hello"})
        assert response.status_code == 402
        assert b"20 AI requests a day" in response.content

    def test_other_workspaces_accounts_are_ignored(
        self, platform_key, owner_client, workspace, organization, fake_claude
    ):
        from apps.social_accounts.models import SocialAccount
        from apps.workspaces.models import Workspace

        other = Workspace.objects.create(organization=organization, name="Other")
        foreign = SocialAccount.objects.create(
            workspace=other,
            platform="bluesky",
            account_platform_id="x",
            account_name="X",
            connection_status="connected",
        )
        response = owner_client.post(_url("adapt", workspace), {"caption": "Hi", "selected_accounts": str(foreign.id)})
        assert response.status_code == 400
        assert fake_claude.calls == []


class TestBrandVoiceSettings:
    def test_owner_saves(self, owner_client, workspace):
        url = reverse("workspaces:brand_voice_settings", kwargs={"workspace_id": workspace.id})
        assert owner_client.get(url).status_code == 200
        response = owner_client.post(
            url,
            {
                "brand_voice_tone": "Calm and clear",
                "brand_voice_words_to_use": "simple",
                "brand_voice_words_to_avoid": "disrupt",
                "brand_voice_examples": ["One", "", "Two"],
            },
        )
        assert response.status_code == 302
        workspace.refresh_from_db()
        assert workspace.brand_voice_tone == "Calm and clear"
        assert workspace.brand_voice_examples == ["One", "Two"]

    def test_too_many_or_too_long(self, owner_client, workspace):
        url = reverse("workspaces:brand_voice_settings", kwargs={"workspace_id": workspace.id})
        owner_client.post(url, {"brand_voice_tone": "x" * 1001})
        owner_client.post(url, {"brand_voice_examples": ["a", "b", "c", "d"]})
        workspace.refresh_from_db()
        assert workspace.brand_voice_tone == "" and workspace.brand_voice_examples == []

    def test_editor_cannot_change_it(self, editor_client, workspace):
        url = reverse("workspaces:brand_voice_settings", kwargs={"workspace_id": workspace.id})
        assert editor_client.get(url).status_code == 200
        assert editor_client.post(url, {"brand_voice_tone": "Loud"}).status_code == 404


class TestOrganizationKeySettings:
    def test_save_replace_and_remove(self, owner_client, organization):
        url = reverse("ai_writer_org:settings")
        assert owner_client.get(url).status_code == 200
        owner_client.post(url, {"provider": "anthropic", "api_key": "sk-ant-api03-first-key-AAAA"})
        key = OrganizationAIKey.objects.get(organization=organization)
        assert (key.api_key, key.key_hint) == ("sk-ant-api03-first-key-AAAA", "AAAA")
        page = owner_client.get(url).content.decode()
        assert "sk-ant-api03-first-key-AAAA" not in page and "…AAAA" in page
        owner_client.post(url, {"action": "remove_key"})
        assert not OrganizationAIKey.objects.exists()

    def test_rejects_junk(self, owner_client):
        owner_client.post(reverse("ai_writer_org:settings"), {"provider": "anthropic", "api_key": "short"})
        owner_client.post(reverse("ai_writer_org:settings"), {"provider": "other", "api_key": "x" * 40})
        assert not OrganizationAIKey.objects.exists()

    def test_page_works_when_the_key_cannot_be_decrypted(self, owner_client, organization):
        from unittest.mock import patch

        OrganizationAIKey.objects.create(
            organization=organization, api_key="sk-ant-api03-first-key-AAAA", key_hint="AAAA"
        )
        with patch("apps.common.encryption.decrypt_value", side_effect=ValueError("bad tag")):
            page = owner_client.get(reverse("ai_writer_org:settings"))
            assert page.status_code == 200 and "…AAAA" in page.content.decode()
            owner_client.post(reverse("ai_writer_org:settings"), {"action": "remove_key"})
        assert not OrganizationAIKey.objects.exists()

    def test_members_cannot_open_it(self, editor_client):
        assert editor_client.get(reverse("ai_writer_org:settings")).status_code == 403


def test_adapted_json_is_valid(platform_key, owner_client, workspace, bluesky, fake_claude):
    fake_claude.queue({"captions": [{"account_id": str(bluesky.id), "caption": 'Quote "this" </script>'}]})
    body = owner_client.post(
        _url("adapt", workspace), {"caption": "x", "selected_accounts": str(bluesky.id)}
    ).content.decode()
    match = re.search(r'<script id="ai-adapted-captions"[^>]*>(.*?)</script>', body, re.DOTALL)
    payload = json.loads(match.group(1))
    assert payload == {str(bluesky.id): 'Quote "this" </script>'}
