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
