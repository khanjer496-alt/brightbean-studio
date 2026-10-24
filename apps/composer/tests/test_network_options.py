"""Per-network post options declared by providers: composer fields, API/MCP acceptance, approval fingerprint.

Nothing here is specific to a network beyond the account's platform slug: the composer renders what the
provider declares and the API accepts the same keys through ``post_settings``.
"""

import json

import pytest
from django.test import Client
from django.urls import reverse

from apps.api_keys import services
from apps.approvals.policy import publish_fingerprint
from apps.composer.models import PlatformPost, Post
from apps.members.models import PERMISSION_KEYS, OrgMembership, WorkspaceMembership
from apps.social_accounts.models import SocialAccount


class _SecureClient(Client):
    def generic(self, method, path, *args, **kwargs):
        kwargs["secure"] = True
        return super().generic(method, path, *args, **kwargs)


@pytest.fixture
def workspace(db, organization, user):
    from apps.workspaces.models import Workspace

    workspace = Workspace.objects.create(name="Options WS", organization=organization)
    OrgMembership.objects.create(user=user, organization=organization, org_role=OrgMembership.OrgRole.OWNER)
    WorkspaceMembership.objects.create(
        user=user, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
    )
    return workspace


def make_account(workspace, platform, **extra):
    return SocialAccount.objects.create(
        workspace=workspace,
        platform=platform,
        account_platform_id=f"{platform}-1",
        account_name=platform,
        account_handle=platform,
        connection_status="connected",
        oauth_access_token="at",
        **extra,
    )


@pytest.fixture
def instagram(workspace):
    return make_account(workspace, "instagram")


def form(account, **fields):
    data = {"action": "save_draft", "title": "T", "caption": "Body", "selected_accounts": str(account.id)}
    data[f"ps_panel_{account.id}"] = "1"
    data.update({f"ps_{key}_{account.id}": value for key, value in fields.items()})
    return data


def save(client, workspace, data):
    return client.post(reverse("composer:save_post", kwargs={"workspace_id": workspace.id}), data=data)


def api_client_for(user, workspace, *accounts):
    key = services.issue_api_key(
        workspace=workspace,
        social_accounts=list(accounts),
        issued_by=user,
        name="options",
        permissions=list(PERMISSION_KEYS),
    )
    return _SecureClient(HTTP_AUTHORIZATION=f"Bearer {key.plaintext_token}")


def api_create(client, account, **body):
    payload = {"social_account_id": str(account.id), "caption": "Body", "title": "Title", **body}
    return client.post("/api/v1/posts/", data=json.dumps(payload), content_type="application/json")


@pytest.mark.django_db
class TestInstagramOptions:
    def test_composer_carries_the_declared_fields_with_visibility_rules(self, client, user, workspace, instagram):
        client.force_login(user)
        response = client.get(reverse("composer:compose", kwargs={"workspace_id": workspace.id}))
        fields = {f["key"]: f for f in response.context["char_limits"][str(instagram.id)]["post_settings"]}
        assert list(fields) == ["post_type", "collaborators", "trial_reel", "share_to_feed", "thumb_offset"]
        assert [c["value"] for c in fields["post_type"]["choices"]] == ["story", "reel"]
        assert fields["trial_reel"]["show_when"] == "post_type=|reel"
        # Music and stickers aren't in the API: the UI says so instead of offering them.
        assert "does not let apps add music" in fields["post_type"]["help"]

    def test_form_stores_the_options_and_the_format_hint(self, client, user, workspace, instagram):
        client.force_login(user)
        save(client, workspace, form(instagram, post_type="reel", collaborators="alice", trial_reel="MANUAL"))
        pp = PlatformPost.objects.get(social_account=instagram)
        assert pp.platform_extra == {"post_type": "reel", "collaborators": "alice", "trial_reel": "MANUAL"}

    def test_a_change_to_an_option_changes_the_approval_fingerprint(self, client, user, workspace, instagram):
        client.force_login(user)
        save(client, workspace, form(instagram, post_type="reel", trial_reel="MANUAL"))
        pp = PlatformPost.objects.get(social_account=instagram)
        before = publish_fingerprint(pp)
        pp.platform_extra = {**pp.platform_extra, "trial_reel": "SS_PERFORMANCE"}
        pp.save()
        assert publish_fingerprint(PlatformPost.objects.get(pk=pp.pk)) != before

    def test_api_accepts_and_stores_the_options(self, user, workspace, instagram):
        api = api_client_for(user, workspace, instagram)
        accounts = {a["id"]: a for a in api.get("/api/v1/accounts/").json()["accounts"]}
        assert [s["key"] for s in accounts[str(instagram.id)]["post_settings"]][:2] == ["post_type", "collaborators"]
        response = api_create(api, instagram, action="draft", post_settings={"post_type": "story", "collaborators": ""})
        assert response.status_code == 201, response.content
        assert PlatformPost.objects.get(social_account=instagram).platform_extra == {"post_type": "story"}
        assert Post.objects.filter(workspace=workspace).count() == 1

    def test_api_refuses_unknown_options(self, user, workspace, instagram):
        api = api_client_for(user, workspace, instagram)
        response = api_create(api, instagram, action="draft", post_settings={"subreddit": "python"})
        assert response.status_code in (400, 422)


@pytest.mark.django_db
class TestXOptions:
    def test_api_stores_community_and_labels_and_fingerprint_follows(self, user, workspace):
        x = make_account(workspace, "x")
        api = api_client_for(user, workspace, x)
        response = api_create(api, x, action="draft", post_settings={"community_id": "123", "made_with_ai": True})
        assert response.status_code == 201, response.content
        pp = PlatformPost.objects.get(social_account=x)
        assert pp.platform_extra == {"community_id": "123", "made_with_ai": True}
        before = publish_fingerprint(pp)
        pp.platform_extra = {**pp.platform_extra, "paid_partnership": True}
        pp.save()
        assert publish_fingerprint(PlatformPost.objects.get(pk=pp.pk)) != before

    def test_a_non_boolean_label_is_refused(self, user, workspace):
        x = make_account(workspace, "x")
        api = api_client_for(user, workspace, x)
        response = api_create(api, x, action="draft", post_settings={"made_with_ai": "yes"})
        assert response.status_code in (400, 422)


@pytest.mark.django_db
class TestGoogleBusinessOptions:
    def test_composer_describes_date_and_time_fields_with_visibility(self, client, user, workspace):
        account = make_account(workspace, "google_business")
        client.force_login(user)
        response = client.get(reverse("composer:compose", kwargs={"workspace_id": workspace.id}))
        fields = {f["key"]: f for f in response.context["char_limits"][str(account.id)]["post_settings"]}
        assert fields["event_start_date"]["kind"] == "date"
        assert fields["event_start_time"]["kind"] == "time"
        assert fields["event_title"]["show_when"] == "topic_type=EVENT|OFFER"
        assert fields["cta_type"]["show_when"] == "topic_type=|EVENT"

    def test_api_stores_a_button_and_validates_an_incomplete_event(self, user, workspace):
        account = make_account(workspace, "google_business")
        api = api_client_for(user, workspace, account)
        ok = api_create(api, account, action="draft", post_settings={"cta_type": "BOOK", "cta_url": "https://e.com/b"})
        assert ok.status_code == 201, ok.content
        assert PlatformPost.objects.get(social_account=account).platform_extra["cta_type"] == "BOOK"

    @pytest.mark.usefixtures("autonomous_agents")
    def test_scheduling_an_event_without_dates_is_refused(self, user, workspace):
        from datetime import timedelta

        from django.utils import timezone

        account = make_account(workspace, "google_business")
        api = api_client_for(user, workspace, account)
        response = api_create(
            api,
            account,
            action="schedule",
            scheduled_at=(timezone.now() + timedelta(hours=2)).isoformat(),
            post_settings={"topic_type": "EVENT", "event_title": "Open day"},
        )
        assert response.status_code == 422
        assert "start and end" in response.json()["detail"]


@pytest.mark.django_db
class TestTikTokOptions:
    def test_declared_fields_and_the_tiktok_panel_save_together(self, client, user, workspace):
        account = make_account(workspace, "tiktok")
        client.force_login(user)
        data = form(account, post_mode="MEDIA_UPLOAD", auto_add_music="")
        data[f"tiktok_privacy_level_{account.id}"] = "SELF_ONLY"
        save(client, workspace, data)
        extra = PlatformPost.objects.get(social_account=account).platform_extra
        assert extra["post_mode"] == "MEDIA_UPLOAD"
        assert extra["privacy_level"] == "SELF_ONLY"
        # A later save from the TikTok panel alone keeps the delivery choice.
        only_panel = {
            "action": "save_draft",
            "title": "T",
            "caption": "Body",
            "selected_accounts": str(account.id),
            f"tiktok_privacy_level_{account.id}": "SELF_ONLY",
        }
        post = Post.objects.get(workspace=workspace)
        url = reverse("composer:save_post_edit", kwargs={"workspace_id": workspace.id, "post_id": post.id})
        client.post(url, data=only_panel)
        assert PlatformPost.objects.get(social_account=account).platform_extra["post_mode"] == "MEDIA_UPLOAD"

    def test_the_api_accepts_the_delivery_and_music_options(self, user, workspace):
        account = make_account(workspace, "tiktok")
        api = api_client_for(user, workspace, account)
        response = api_create(
            api, account, action="draft", post_settings={"post_mode": "MEDIA_UPLOAD", "auto_add_music": False}
        )
        assert response.status_code == 201
        assert PlatformPost.objects.get(social_account=account).platform_extra["post_mode"] == "MEDIA_UPLOAD"
