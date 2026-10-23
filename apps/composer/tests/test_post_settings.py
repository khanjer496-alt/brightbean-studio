"""Per-post settings a provider declares: composer form, scheduling checks, API and MCP.

Reddit is the platform that declares them (subreddit, flair, post kind, NSFW,
spoiler); nothing here knows Reddit beyond the account's platform slug.
"""

import json
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.api_keys import services
from apps.composer.models import PlatformPost, Post
from apps.composer.services import clean_post_settings, parse_post_settings, publish_blocker
from apps.members.models import PERMISSION_KEYS, OrgMembership, WorkspaceMembership
from apps.social_accounts.models import SocialAccount


class _SecureClient(Client):
    def generic(self, method, path, *args, **kwargs):
        kwargs["secure"] = True
        return super().generic(method, path, *args, **kwargs)


@pytest.fixture
def workspace(db, organization, user):
    from apps.workspaces.models import Workspace

    workspace = Workspace.objects.create(name="Settings WS", organization=organization)
    OrgMembership.objects.create(user=user, organization=organization, org_role=OrgMembership.OrgRole.OWNER)
    WorkspaceMembership.objects.create(
        user=user, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
    )
    return workspace


@pytest.fixture
def reddit(workspace):
    return SocialAccount.objects.create(
        workspace=workspace,
        platform="reddit",
        account_platform_id="u1",
        account_name="u/alice",
        account_handle="alice",
        connection_status="connected",
        oauth_access_token="at",
    )


@pytest.fixture
def mastodon(workspace):
    return SocialAccount.objects.create(
        workspace=workspace,
        platform="mastodon",
        account_platform_id="m1",
        account_name="mast",
        connection_status="connected",
    )


def form(account, **fields):
    data = {"action": "save_draft", "title": "A title", "caption": "Body", "selected_accounts": str(account.id)}
    data[f"ps_panel_{account.id}"] = "1"
    data.update({f"ps_{key}_{account.id}": value for key, value in fields.items()})
    return data


class TestServices:
    def test_parse_reads_the_declared_fields_only(self):
        values = {
            "ps_subreddit_a1": " python ",
            "ps_post_kind_a1": "link",
            "ps_flair_id_a1": "",
            "ps_nsfw_a1": "true",
            "ps_other_a1": "ignored",
        }
        assert parse_post_settings("reddit", lambda key, default="": values.get(key, default), "a1") == {
            "subreddit": "python",
            "post_kind": "link",
            "nsfw": True,
            "spoiler": False,
        }

    def test_a_platform_without_settings_parses_nothing(self):
        assert parse_post_settings("mastodon", lambda key, default="": "x", "a1") == {}

    def test_clean_refuses_keys_the_platform_does_not_declare(self):
        with pytest.raises(ValueError, match="Unknown post setting.*bogus"):
            clean_post_settings("reddit", {"subreddit": "python", "bogus": 1})
        with pytest.raises(ValueError, match="accepts: none"):
            clean_post_settings("mastodon", {"subreddit": "python"})

    def test_clean_drops_blank_values(self):
        assert clean_post_settings("reddit", {"subreddit": "python", "flair_id": "", "nsfw": None}) == {
            "subreddit": "python"
        }

    def test_publish_blocker_asks_for_the_required_setting(self, reddit):
        assert "Subreddit is required" in publish_blocker(reddit, media_types=[], platform_extra={})
        assert publish_blocker(reddit, media_types=[], platform_extra={"subreddit": "python"}) == ""

    def test_a_platform_that_publishes_no_media_refuses_any(self, reddit, mastodon):
        assert "text or links" in publish_blocker(reddit, media_types=["image"], platform_extra={"subreddit": "python"})
        assert publish_blocker(mastodon, media_types=["image"], platform_extra={}) == ""

    def test_a_platform_without_settings_is_never_blocked_by_them(self, mastodon):
        assert publish_blocker(mastodon, media_types=[], platform_extra={}) == ""


@pytest.mark.django_db
class TestComposerForm:
    def save(self, client, workspace, data):
        return client.post(reverse("composer:save_post", kwargs={"workspace_id": workspace.id}), data=data)

    def test_saving_a_draft_stores_the_settings_in_platform_extra(self, client, user, workspace, reddit):
        client.force_login(user)
        response = self.save(client, workspace, form(reddit, subreddit="python", flair_id="f1", spoiler="true"))
        assert response.status_code in (200, 204, 302), response.content
        pp = PlatformPost.objects.get(social_account=reddit)
        assert pp.platform_extra == {
            "subreddit": "python",
            "flair_id": "f1",
            "nsfw": False,
            "spoiler": True,
        }

    def test_a_save_without_the_panel_keeps_what_was_stored(self, client, user, workspace, reddit):
        client.force_login(user)
        self.save(client, workspace, form(reddit, subreddit="python"))
        post = Post.objects.get(workspace=workspace)
        data = form(reddit)
        del data[f"ps_panel_{reddit.id}"]
        client.post(reverse("composer:save_post_edit", kwargs={"workspace_id": workspace.id, "post_id": post.id}), data)
        assert PlatformPost.objects.get(social_account=reddit).platform_extra["subreddit"] == "python"

    def test_scheduling_without_the_required_setting_is_refused(self, client, user, workspace, reddit):
        client.force_login(user)
        data = form(reddit, subreddit="")
        data["action"] = "schedule"
        response = self.save(client, workspace, data)
        assert response.status_code == 400
        assert "Subreddit is required" in response.json()["errors"]["post_settings"]
        assert response.json()["errors"]["post_settings"].startswith("Reddit:")
        assert not Post.objects.exists()

    def test_scheduling_media_to_a_text_only_platform_is_refused_in_the_composer(self, client, user, workspace, reddit):
        from apps.composer.models import PostMedia
        from apps.media_library.models import MediaAsset

        client.force_login(user)
        post = Post.objects.create(workspace=workspace, caption="x", author=user)
        asset = MediaAsset.objects.create(
            organization=workspace.organization,
            workspace=workspace,
            file="test/a.jpg",
            filename="a.jpg",
            media_type=MediaAsset.MediaType.IMAGE,
            mime_type="image/jpeg",
        )
        PostMedia.objects.create(post=post, media_asset=asset, position=0)
        data = form(reddit, subreddit="python")
        data["action"] = "schedule"
        response = client.post(
            reverse("composer:save_post_edit", kwargs={"workspace_id": workspace.id, "post_id": post.id}), data
        )
        assert response.status_code == 400
        assert "Reddit can only publish text or links" in response.json()["errors"]["media"]

    def test_a_draft_may_leave_them_empty(self, client, user, workspace, reddit):
        client.force_login(user)
        assert self.save(client, workspace, form(reddit, subreddit="")).status_code in (200, 204, 302)

    def test_the_composer_page_carries_the_field_schema(self, client, user, workspace, reddit):
        client.force_login(user)
        response = client.get(reverse("composer:compose", kwargs={"workspace_id": workspace.id}))
        assert response.status_code == 200
        limits = response.context["char_limits"][str(reddit.id)]
        assert [s["key"] for s in limits["post_settings"]][:2] == ["subreddit", "post_kind"]
        assert limits["needs_title"] is True
        assert limits["title_max_length"] == 300
        body = response.content.decode()
        assert "providerSettings(" in body
        assert "post-setting-options" in body


@pytest.mark.django_db
class TestOptionsEndpoint:
    def url(self, workspace, account, key="flair_id"):
        return reverse(
            "composer:post_setting_options",
            kwargs={"workspace_id": workspace.id, "account_id": account.id, "key": key},
        )

    def test_returns_the_providers_live_options(self, client, user, workspace, reddit):
        client.force_login(user)
        with patch(
            "providers.reddit.RedditProvider.get_setting_options", return_value=[{"id": "f1", "name": "Question"}]
        ) as options:
            response = client.get(self.url(workspace, reddit), {"depends": "python"})
        assert response.json() == {"options": [{"id": "f1", "name": "Question"}]}
        assert options.call_args.args[1:] == ("flair_id", "python")

    def test_a_setting_that_is_not_live_is_not_found(self, client, user, workspace, reddit):
        client.force_login(user)
        assert client.get(self.url(workspace, reddit, "subreddit")).status_code == 404

    def test_a_platform_without_settings_is_not_found(self, client, user, workspace, mastodon):
        client.force_login(user)
        assert client.get(self.url(workspace, mastodon)).status_code == 404

    def test_a_platform_failure_is_a_502_not_a_crash(self, client, user, workspace, reddit):
        client.force_login(user)
        with patch("providers.reddit.RedditProvider.get_setting_options", side_effect=RuntimeError("down")):
            assert client.get(self.url(workspace, reddit)).status_code == 502

    def test_another_workspaces_account_is_not_reachable(self, client, user, workspace, reddit, organization):
        from apps.workspaces.models import Workspace

        other = Workspace.objects.create(name="Other", organization=organization)
        WorkspaceMembership.objects.create(
            user=user, workspace=other, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
        )
        client.force_login(user)
        assert client.get(self.url(other, reddit)).status_code == 404


@pytest.fixture
def api_client(user, workspace, reddit, mastodon):
    key = services.issue_api_key(
        workspace=workspace,
        social_accounts=[reddit, mastodon],
        issued_by=user,
        name="settings",
        permissions=list(PERMISSION_KEYS),
    )
    return _SecureClient(HTTP_AUTHORIZATION=f"Bearer {key.plaintext_token}")


def _when():
    return (timezone.now() + timedelta(hours=2)).isoformat()


def _mcp(client, tool, arguments):
    rpc = {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": tool, "arguments": arguments}, "id": 1}
    return client.post("/api/v1/mcp/", data=json.dumps(rpc), content_type="application/json").json()


@pytest.mark.django_db
class TestApiAndMcp:
    def test_accounts_describe_their_settings(self, api_client, reddit, mastodon):
        accounts = {a["id"]: a for a in api_client.get("/api/v1/accounts/").json()["accounts"]}
        settings = {s["key"]: s for s in accounts[str(reddit.id)]["post_settings"]}
        assert settings["subreddit"]["required"] is True
        assert settings["flair_id"]["options_from_provider"] is True
        assert settings["post_kind"]["choices"] == [
            {"value": "self", "label": "Text post"},
            {"value": "link", "label": "Link post"},
        ]
        assert accounts[str(reddit.id)]["needs_title"] is True
        assert accounts[str(reddit.id)]["thread_mode"] == "comments"
        assert accounts[str(reddit.id)]["char_limit"] == 40000
        assert accounts[str(mastodon.id)]["post_settings"] == []

    def test_mcp_list_accounts_shares_the_same_schema(self, api_client, reddit):
        accounts = json.loads(_mcp(api_client, "list_accounts", {})["result"]["content"][0]["text"])
        row = next(a for a in accounts["accounts"] if a["id"] == str(reddit.id))
        assert row["post_settings"][0]["key"] == "subreddit"

    def create(self, client, account, **body):
        payload = {"social_account_id": str(account.id), "caption": "Body", "title": "Title", **body}
        return client.post("/api/v1/posts/", data=json.dumps(payload), content_type="application/json")

    def test_settings_sent_with_the_post_are_stored(self, api_client, reddit):
        response = self.create(
            api_client,
            reddit,
            action="schedule",
            scheduled_at=_when(),
            post_settings={"subreddit": "python", "nsfw": True},
        )
        assert response.status_code == 201, response.content
        assert PlatformPost.objects.get(social_account=reddit).platform_extra == {"subreddit": "python", "nsfw": True}

    def test_scheduling_without_them_is_refused(self, api_client, reddit):
        response = self.create(api_client, reddit, action="schedule", scheduled_at=_when())
        assert response.status_code == 422
        assert "Subreddit is required" in response.json()["detail"]

    def test_scheduling_with_an_invalid_value_is_refused(self, api_client, reddit):
        response = self.create(
            api_client, reddit, action="schedule", scheduled_at=_when(), post_settings={"subreddit": "bad name!"}
        )
        assert response.status_code == 422
        assert "Subreddit must be" in response.json()["detail"]

    def test_an_unknown_setting_is_refused_even_for_a_draft(self, api_client, reddit):
        response = self.create(api_client, reddit, post_settings={"board_id": "b1"})
        assert response.status_code == 422
        assert "Unknown post setting" in response.json()["detail"]

    def test_a_draft_can_be_scheduled_once_it_has_them(self, api_client, reddit):
        draft = self.create(api_client, reddit, post_settings={"subreddit": "python"})
        assert draft.status_code == 201
        response = api_client.post(
            f"/api/v1/posts/{draft.json()['id']}/schedule",
            data=json.dumps({"scheduled_at": _when()}),
            content_type="application/json",
        )
        assert response.status_code == 200, response.content

    def test_mcp_schedule_post_takes_post_settings(self, api_client, reddit):
        result = _mcp(
            api_client,
            "schedule_post",
            {
                "social_account_id": str(reddit.id),
                "caption": "Body",
                "title": "T",
                "scheduled_at": _when(),
                "post_settings": {"subreddit": "python"},
            },
        )
        assert "error" not in result, result
        assert PlatformPost.objects.get(social_account=reddit).platform_extra == {"subreddit": "python"}

    def test_mcp_refuses_without_them(self, api_client, reddit):
        result = _mcp(
            api_client,
            "schedule_post",
            {"social_account_id": str(reddit.id), "caption": "Body", "title": "T", "scheduled_at": _when()},
        )
        assert "Subreddit is required" in result["error"]["message"]

    def test_mcp_create_draft_takes_post_settings(self, api_client, reddit):
        result = _mcp(
            api_client,
            "create_draft",
            {"social_account_id": str(reddit.id), "caption": "Body", "post_settings": {"subreddit": "python"}},
        )
        assert "error" not in result, result
        assert PlatformPost.objects.get(social_account=reddit).platform_extra == {"subreddit": "python"}
