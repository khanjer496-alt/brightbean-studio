"""Channel options on the account card: nickname, picture, profile address and pausing publishing."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.composer.models import PlatformPost, Post
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.publisher.engine import PublishEngine
from apps.social_accounts.models import SocialAccount


@pytest.fixture
def workspace(db, organization):
    from apps.workspaces.models import Workspace

    return Workspace.objects.create(name="Options WS", organization=organization)


@pytest.fixture
def manager(db, user, organization, workspace):
    OrgMembership.objects.create(user=user, organization=organization, org_role="owner")
    WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role="owner")
    return user


@pytest.fixture
def account(workspace):
    return SocialAccount.objects.create(
        workspace=workspace,
        platform="mastodon",
        account_platform_id="m-1",
        account_name="Real Name",
        account_handle="real",
        avatar_url="https://cdn.example/real.png",
        connection_status="connected",
    )


def options_url(workspace, account):
    return reverse("social_accounts:update_options", kwargs={"workspace_id": workspace.id, "account_id": account.id})


def paused_url(workspace, account):
    return reverse("social_accounts:set_paused", kwargs={"workspace_id": workspace.id, "account_id": account.id})


class TestDisplayOverrides:
    def test_defaults_come_from_the_platform(self, account):
        assert account.display_name == "Real Name"
        assert account.display_avatar_url == "https://cdn.example/real.png"
        from providers import PROVIDER_REGISTRY

        assert account.profile_link == PROVIDER_REGISTRY["mastodon"].profile_url(account)

    def test_overrides_win_and_nickname_labels_pickers(self, account):
        account.nickname = "Brand voice"
        account.custom_avatar_url = "https://cdn.example/custom.png"
        account.custom_profile_url = "https://example.com/us"
        assert account.display_name == account.display_label == "Brand voice"
        assert account.display_avatar_url == "https://cdn.example/custom.png"
        assert account.profile_link == "https://example.com/us"

    def test_save_stores_the_overrides_and_the_card_shows_them(self, client, manager, workspace, account):
        client.force_login(manager)
        response = client.post(
            options_url(workspace, account),
            {
                "nickname": " Brand voice ",
                "custom_avatar_url": "https://cdn.example/custom.png",
                "custom_profile_url": "https://example.com/us",
            },
            HTTP_HX_REQUEST="true",
        )
        assert response.status_code == 200
        account.refresh_from_db()
        assert (account.nickname, account.custom_avatar_url, account.custom_profile_url) == (
            "Brand voice",
            "https://cdn.example/custom.png",
            "https://example.com/us",
        )
        body = response.content.decode()
        assert "Brand voice" in body and "https://cdn.example/custom.png" in body
        assert 'href="https://example.com/us"' in body

    def test_an_address_must_be_https_and_the_card_reopens_the_dialog_with_the_error(
        self, client, manager, workspace, account
    ):
        client.force_login(manager)
        response = client.post(
            options_url(workspace, account),
            {"nickname": "x", "custom_avatar_url": "http://insecure.example/a.png"},
            HTTP_HX_REQUEST="true",
        )
        account.refresh_from_db()
        assert account.custom_avatar_url == "" and account.nickname == ""
        body = response.content.decode()
        assert "starts with https://" in body
        assert "optionsOpen: true" in body

    def test_clearing_the_fields_restores_the_platform_values(self, client, manager, workspace, account):
        SocialAccount.objects.filter(pk=account.pk).update(nickname="Old", custom_avatar_url="https://a.example/x.png")
        client.force_login(manager)
        client.post(options_url(workspace, account), {})
        account.refresh_from_db()
        assert account.display_name == "Real Name"
        assert account.display_avatar_url == "https://cdn.example/real.png"

    def test_a_member_without_account_management_cannot_change_them(
        self, client, user, organization, workspace, account
    ):
        OrgMembership.objects.create(user=user, organization=organization, org_role="member")
        WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role="viewer")
        client.force_login(user)
        response = client.post(options_url(workspace, account), {"nickname": "Nope"})
        assert response.status_code in (302, 403)
        account.refresh_from_db()
        assert account.nickname == ""
        response = client.post(paused_url(workspace, account), {"paused": "1"})
        account.refresh_from_db()
        assert not account.is_paused

    def test_another_workspaces_account_is_not_reachable(self, client, manager, organization, account):
        from apps.workspaces.models import Workspace

        other = Workspace.objects.create(name="Other", organization=organization)
        WorkspaceMembership.objects.create(user=manager, workspace=other, workspace_role="owner")
        client.force_login(manager)
        assert client.post(options_url(other, account), {"nickname": "x"}).status_code == 404


class TestPausing:
    def test_pause_and_resume_toggle_the_account_and_the_card_says_so(self, client, manager, workspace, account):
        client.force_login(manager)
        response = client.post(paused_url(workspace, account), {"paused": "1"}, HTTP_HX_REQUEST="true")
        account.refresh_from_db()
        assert account.is_paused and account.paused_by == manager
        assert "Publishing is paused" in response.content.decode()
        response = client.post(paused_url(workspace, account), {"paused": "0"}, HTTP_HX_REQUEST="true")
        account.refresh_from_db()
        assert not account.is_paused and account.paused_by is None
        assert "Publishing is paused" not in response.content.decode()

    def test_the_card_counts_the_held_posts(self, client, manager, workspace, account):
        _scheduled(workspace, account, manager)
        client.force_login(manager)
        response = client.post(paused_url(workspace, account), {"paused": "1"}, HTTP_HX_REQUEST="true")
        assert "1 scheduled post is held" in response.content.decode()

    def test_the_composer_picker_uses_the_nickname(self, client, manager, workspace, account):
        SocialAccount.objects.filter(pk=account.pk).update(nickname="Brand voice")
        client.force_login(manager)
        response = client.get(reverse("composer:compose", kwargs={"workspace_id": workspace.id}))
        assert "Brand voice" in response.content.decode()


def _scheduled(workspace, account, author, *, minutes_ago=5):
    post = Post.objects.create(workspace=workspace, author=author, caption="due")
    return PlatformPost.objects.create(
        post=post,
        social_account=account,
        status=PlatformPost.Status.SCHEDULED,
        scheduled_at=timezone.now() - timedelta(minutes=minutes_ago),
    )


class TestPublisherHoldsPausedAccounts:
    def test_a_due_post_is_not_picked_while_paused_and_is_after_resume(self, workspace, account, manager):
        pp = _scheduled(workspace, account, manager)
        assert [p.id for p in PublishEngine()._get_due_platform_posts()] == [pp.id]
        SocialAccount.objects.filter(pk=account.pk).update(paused_at=timezone.now())
        assert PublishEngine()._get_due_platform_posts() == []
        pp.refresh_from_db()
        assert pp.status == PlatformPost.Status.SCHEDULED  # held, not failed
        SocialAccount.objects.filter(pk=account.pk).update(paused_at=None)
        assert [p.id for p in PublishEngine()._get_due_platform_posts()] == [pp.id]

    def test_a_group_started_before_the_pause_still_holds_the_post(self, workspace, account, manager):
        pp = _scheduled(workspace, account, manager)
        due = PublishEngine()._get_due_platform_posts()
        SocialAccount.objects.filter(pk=account.pk).update(paused_at=timezone.now())
        with patch.object(PublishEngine, "_publish_platform_post") as publish:
            sent = PublishEngine()._publish_post_group(pp.post, due)
        assert sent is False
        publish.assert_not_called()
        pp.refresh_from_db()
        assert pp.status == PlatformPost.Status.SCHEDULED

    def test_a_paused_accounts_retry_waits(self, workspace, account, manager):
        pp = _scheduled(workspace, account, manager)
        PlatformPost.objects.filter(pk=pp.pk).update(retry_count=1, next_retry_at=timezone.now() - timedelta(minutes=1))
        SocialAccount.objects.filter(pk=account.pk).update(paused_at=timezone.now())
        with patch.object(PublishEngine, "_publish_platform_post") as publish:
            PublishEngine()._process_retries()
        publish.assert_not_called()
        pp.refresh_from_db()
        assert pp.status == PlatformPost.Status.SCHEDULED and pp.retry_count == 1

    def test_other_accounts_are_unaffected(self, workspace, account, manager):
        other = SocialAccount.objects.create(
            workspace=workspace,
            platform="bluesky",
            account_platform_id="b-1",
            account_name="B",
            connection_status="connected",
        )
        _scheduled(workspace, account, manager)
        free = _scheduled(workspace, other, manager)
        SocialAccount.objects.filter(pk=account.pk).update(paused_at=timezone.now())
        assert [p.id for p in PublishEngine()._get_due_platform_posts()] == [free.id]


class TestApiShowsPauseAndHold:
    def test_accounts_and_posts_say_why_a_post_is_held(self, user, manager, workspace, account):
        from django.test import Client

        from apps.api_keys import services
        from apps.members.models import PERMISSION_KEYS

        class Secure(Client):
            def generic(self, method, path, *args, **kwargs):
                kwargs["secure"] = True
                return super().generic(method, path, *args, **kwargs)

        key = services.issue_api_key(
            workspace=workspace,
            social_accounts=[account],
            issued_by=user,
            name="pause",
            permissions=list(PERMISSION_KEYS),
        )
        api = Secure(HTTP_AUTHORIZATION=f"Bearer {key.plaintext_token}")
        pp = _scheduled(workspace, account, user)
        assert api.get("/api/v1/accounts/").json()["accounts"][0]["paused"] is False
        SocialAccount.objects.filter(pk=account.pk).update(paused_at=timezone.now())
        assert api.get("/api/v1/accounts/").json()["accounts"][0]["paused"] is True
        body = api.get(f"/api/v1/posts/{pp.post_id}").json()
        assert "paused" in body["platform_posts"][0]["held_reason"]


class TestPauseOverApiAndMcp:
    @pytest.fixture
    def api(self, user, manager, workspace, account):
        from django.core.cache import cache
        from django.test import Client

        from apps.api_keys import services
        from apps.members.models import PERMISSION_KEYS

        cache.delete("agent_api:auth_fail:127.0.0.1")

        class Secure(Client):
            def generic(self, method, path, *args, **kwargs):
                kwargs["secure"] = True
                return super().generic(method, path, *args, **kwargs)

        def client(permissions=None, accounts=(account,)):
            key = services.issue_api_key(
                workspace=workspace,
                social_accounts=list(accounts),
                issued_by=user,
                name="pause",
                permissions=permissions or list(PERMISSION_KEYS),
            )
            return Secure(HTTP_AUTHORIZATION=f"Bearer {key.plaintext_token}")

        return client

    def test_rest_pauses_and_resumes(self, api, account, user):
        client = api()
        body = client.post(f"/api/v1/accounts/{account.id}/pause").json()
        assert body["paused"] is True
        account.refresh_from_db()
        assert account.is_paused and account.paused_by == user
        assert client.post(f"/api/v1/accounts/{account.id}/resume").json()["paused"] is False
        account.refresh_from_db()
        assert not account.is_paused and account.paused_by is None

    def test_rest_needs_account_management_and_the_allowlist(self, api, workspace, account):
        assert api(permissions=["create_posts"]).post(f"/api/v1/accounts/{account.id}/pause").status_code == 403
        other = SocialAccount.objects.create(
            workspace=workspace, platform="bluesky", account_platform_id="b-2", connection_status="connected"
        )
        assert api().post(f"/api/v1/accounts/{other.id}/pause").status_code == 403
        other.refresh_from_db()
        assert not other.is_paused

    def test_mcp_tools(self, api, account):
        import json

        client = api()

        def call(name):
            body = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": {"account_id": str(account.id)}},
            }
            return client.post("/api/v1/mcp/", data=json.dumps(body), content_type="application/json").json()

        assert json.loads(call("pause_account")["result"]["content"][0]["text"])["paused"] is True
        assert json.loads(call("resume_account")["result"]["content"][0]["text"])["paused"] is False

    def test_capability_mapping(self):
        from apps.api_keys import capabilities

        for path in ("/api/v1/accounts/x/pause", "/api/v1/accounts/x/resume"):
            assert capabilities.capability_for_request("POST", path) == "write_settings"
        assert capabilities.tool_capability("pause_account") == "write_settings"
        assert capabilities.tool_capability("resume_account") == "write_settings"


class TestAutomationsSkipPausedAccounts:
    def test_evergreen_does_not_fill_a_paused_account(self, workspace, account, manager):
        from apps.calendar import evergreen

        evergreen._new_item(
            workspace=workspace,
            social_account=account,
            user=manager,
            category=None,
            interval_days=7,
            max_repeats=None,
            vary_with_ai=False,
            pre_approved=False,
            caption="Evergreen",
        ).save()
        account.set_paused(True, by=manager)
        with patch.object(evergreen, "fill_account") as fill:
            evergreen.fill_queues()
        fill.assert_not_called()
        account.set_paused(False)
        with patch.object(evergreen, "fill_account", return_value=evergreen.FillResult()) as fill:
            evergreen.fill_queues()
        fill.assert_called_once()


def _slot_every_day(account):
    from datetime import time

    from apps.calendar.models import PostingSlot

    for day in range(7):
        for at in (time(9, 0), time(15, 0)):
            PostingSlot.objects.create(social_account=account, day_of_week=day, time=at)


def _held(workspace, account, author, count, *, caption="held"):
    """``count`` posts that came due while paused, oldest first, and one still in the future."""
    rows = []
    for index in range(count):
        post = Post.objects.create(workspace=workspace, author=author, caption=f"{caption} {index}")
        rows.append(
            PlatformPost.objects.create(
                post=post,
                social_account=account,
                status=PlatformPost.Status.SCHEDULED,
                scheduled_at=timezone.now() - timedelta(hours=count - index),
            )
        )
    return rows


class TestResumeChoosesWhatHappensToHeldPosts:
    @pytest.fixture
    def held(self, workspace, account, manager):
        _slot_every_day(account)
        rows = _held(workspace, account, manager, 3)
        future = Post.objects.create(workspace=workspace, author=manager, caption="later")
        keep_at = timezone.now() + timedelta(days=2)
        later = PlatformPost.objects.create(
            post=future, social_account=account, status=PlatformPost.Status.SCHEDULED, scheduled_at=keep_at
        )
        account.set_paused(True, by=manager)
        return rows, later, keep_at

    def test_requeue_moves_them_into_the_next_free_slots_in_order(self, held, account, manager):
        from apps.calendar.services import next_available_slots, resume_publishing

        rows, later, keep_at = held
        expected = next_available_slots(account, 4)
        expected.remove(keep_at) if keep_at in expected else None
        outcome = resume_publishing(account, "requeue", by=manager)
        assert outcome == {"requeued": 3, "published_now": 0, "skipped": 0}
        account.refresh_from_db()
        assert not account.is_paused
        times = []
        for pp in rows:
            pp.refresh_from_db()
            assert pp.status == PlatformPost.Status.SCHEDULED and pp.scheduled_at > timezone.now()
            times.append(pp.scheduled_at)
        assert times == sorted(times) and len(set(times)) == 3
        assert times == expected[:3]
        later.refresh_from_db()
        assert later.scheduled_at == keep_at
        assert PublishEngine()._get_due_platform_posts() == []

    def test_requeue_is_the_default(self, held, account, manager):
        from apps.calendar.services import resume_publishing

        assert resume_publishing(account, by=manager)["requeued"] == 3

    def test_requeue_without_slots_leaves_drafts_rather_than_a_burst(self, workspace, account, manager):
        rows = _held(workspace, account, manager, 2)
        account.set_paused(True, by=manager)
        from apps.calendar.services import resume_publishing

        assert resume_publishing(account, "requeue", by=manager) == {"requeued": 0, "published_now": 0, "skipped": 2}
        for pp in rows:
            pp.refresh_from_db()
            assert pp.status == PlatformPost.Status.DRAFT and pp.scheduled_at is None

    def test_publish_now_staggers_a_minute_apart_in_order(self, held, account, manager):
        from apps.calendar.services import resume_publishing

        rows, later, keep_at = held
        before = timezone.now()
        outcome = resume_publishing(account, "publish_now", by=manager)
        assert outcome == {"requeued": 0, "published_now": 3, "skipped": 0}
        times = []
        for pp in rows:
            pp.refresh_from_db()
            assert pp.status == PlatformPost.Status.SCHEDULED
            times.append(pp.scheduled_at)
        assert times[1] - times[0] == timedelta(minutes=1) and times[2] - times[1] == timedelta(minutes=1)
        assert before <= times[0] <= timezone.now()
        later.refresh_from_db()
        assert later.scheduled_at == keep_at
        # Only the first is due this instant; the rest follow a minute apart.
        assert [p.id for p in PublishEngine()._get_due_platform_posts()] == [rows[0].id]

    def test_skip_turns_them_into_drafts_and_deletes_nothing(self, held, account, manager):
        from apps.calendar.services import resume_publishing

        rows, later, keep_at = held
        outcome = resume_publishing(account, "skip", by=manager)
        assert outcome == {"requeued": 0, "published_now": 0, "skipped": 3}
        for pp in rows:
            pp.refresh_from_db()
            assert pp.status == PlatformPost.Status.DRAFT and pp.scheduled_at is None
        assert Post.objects.filter(workspace=account.workspace).count() == 4
        later.refresh_from_db()
        assert later.status == PlatformPost.Status.SCHEDULED and later.scheduled_at == keep_at
        assert PublishEngine()._get_due_platform_posts() == []

    def test_a_queue_entry_follows_its_post(self, held, workspace, account, manager):
        from apps.calendar.models import Queue, QueueEntry
        from apps.calendar.services import resume_publishing

        rows, _later, _keep = held
        queue = Queue.objects.create(workspace=workspace, name="Q", social_account=account)
        entry = QueueEntry.objects.create(queue=queue, post=rows[0].post, position=1)
        resume_publishing(account, "requeue", by=manager)
        rows[0].refresh_from_db()
        entry.refresh_from_db()
        assert entry.assigned_slot_datetime == rows[0].scheduled_at

    def test_a_retry_in_progress_is_not_a_held_post(self, workspace, account, manager):
        from apps.calendar.services import overdue_held_posts

        rows = _held(workspace, account, manager, 2)
        PlatformPost.objects.filter(pk=rows[0].pk).update(retry_count=1)
        assert [pp.id for pp in overdue_held_posts(account)] == [rows[1].id]

    def test_resuming_an_account_that_is_on_changes_nothing(self, workspace, account, manager):
        from apps.calendar.services import resume_publishing

        rows = _held(workspace, account, manager, 1)
        assert resume_publishing(account, "skip", by=manager) == {"requeued": 0, "published_now": 0, "skipped": 0}
        rows[0].refresh_from_db()
        assert rows[0].status == PlatformPost.Status.SCHEDULED

    def test_an_unknown_choice_is_refused(self, account):
        from apps.calendar.services import resume_publishing

        with pytest.raises(ValueError):
            resume_publishing(account, "everything")


class TestResumeDialogOnTheCard:
    def test_no_overdue_posts_resumes_straight_away(self, client, manager, workspace, account):
        account.set_paused(True, by=manager)
        client.force_login(manager)
        html = client.post(paused_url(workspace, account), {"paused": "0"}, HTTP_HX_REQUEST="true").content.decode()
        account.refresh_from_db()
        assert not account.is_paused and "held while paused" not in html

    def test_a_paused_card_with_overdue_posts_offers_the_three_choices_requeue_first(
        self, client, manager, workspace, account
    ):
        _held(workspace, account, manager, 2)
        client.force_login(manager)
        client.post(paused_url(workspace, account), {"paused": "1"}, HTTP_HX_REQUEST="true")
        html = client.get(reverse("social_accounts:list", kwargs={"workspace_id": workspace.id})).content.decode()
        assert "2 posts were held while paused." in html
        assert html.index('value="requeue"') < html.index('value="publish_now"') < html.index('value="skip"')
        assert 'value="requeue" class="mt-1" checked' in html
        assert 'id="resume-btn-' in html

    def test_the_card_has_no_dialog_without_overdue_posts(self, client, manager, workspace, account):
        account.set_paused(True, by=manager)
        client.force_login(manager)
        html = client.get(reverse("social_accounts:list", kwargs={"workspace_id": workspace.id})).content.decode()
        assert "held while paused" not in html and 'value="skip"' not in html

    @pytest.mark.parametrize("choice,status", [("skip", "draft"), ("publish_now", "scheduled"), ("requeue", "draft")])
    def test_posting_a_choice_applies_it(self, client, manager, workspace, account, choice, status):
        rows = _held(workspace, account, manager, 1)
        account.set_paused(True, by=manager)
        client.force_login(manager)
        client.post(paused_url(workspace, account), {"paused": "0", "held_posts": choice}, HTTP_HX_REQUEST="true")
        rows[0].refresh_from_db()
        assert rows[0].status == status  # requeue with no posting slots falls back to a draft

    def test_a_bad_choice_is_a_400_and_the_account_stays_paused(self, client, manager, workspace, account):
        account.set_paused(True, by=manager)
        client.force_login(manager)
        response = client.post(paused_url(workspace, account), {"paused": "0", "held_posts": "all"})
        assert response.status_code == 400
        account.refresh_from_db()
        assert account.is_paused

    def test_the_audit_trail_records_who_chose_what(self, client, manager, workspace, account):
        from apps.autonomy.models import AgentAction

        _slot_every_day(account)
        _held(workspace, account, manager, 2)
        account.set_paused(True, by=manager)
        client.force_login(manager)
        client.post(paused_url(workspace, account), {"paused": "0", "held_posts": "skip"}, HTTP_HX_REQUEST="true")
        action = AgentAction.objects.get(workspace=workspace, operation="account.resume")
        assert action.actor_type == "human" and action.actor_user == manager and action.social_account == account
        assert "Held posts: skip (2 left as drafts)" in action.summary


class TestResumeOverApiAndMcpWithHeldPosts:
    @pytest.fixture
    def api(self, user, manager, workspace, account):
        from django.core.cache import cache
        from django.test import Client

        from apps.api_keys import services
        from apps.members.models import PERMISSION_KEYS

        cache.delete("agent_api:auth_fail:127.0.0.1")

        class Secure(Client):
            def generic(self, method, path, *args, **kwargs):
                kwargs["secure"] = True
                return super().generic(method, path, *args, **kwargs)

        key = services.issue_api_key(
            workspace=workspace,
            social_accounts=[account],
            issued_by=user,
            name="resume",
            permissions=list(PERMISSION_KEYS),
        )
        return Secure(HTTP_AUTHORIZATION=f"Bearer {key.plaintext_token}")

    def _mcp(self, client, name, arguments):
        import json

        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
        return client.post("/api/v1/mcp/", data=json.dumps(body), content_type="application/json").json()

    def test_rest_requeues_by_default_and_counts(self, api, workspace, account, manager):
        _slot_every_day(account)
        rows = _held(workspace, account, manager, 2)
        account.set_paused(True, by=manager)
        body = api.post(f"/api/v1/accounts/{account.id}/resume").json()
        assert body["paused"] is False and (body["requeued"], body["published_now"], body["skipped"]) == (2, 0, 0)
        rows[0].refresh_from_db()
        assert rows[0].scheduled_at > timezone.now()

    @pytest.mark.parametrize("choice,key", [("publish_now", "published_now"), ("skip", "skipped")])
    def test_rest_accepts_the_other_choices(self, api, workspace, account, manager, choice, key):
        _held(workspace, account, manager, 2)
        account.set_paused(True, by=manager)
        body = api.post(f"/api/v1/accounts/{account.id}/resume?held_posts={choice}").json()
        assert body[key] == 2

    def test_rest_refuses_an_unknown_choice_and_stays_paused(self, api, workspace, account, manager):
        account.set_paused(True, by=manager)
        assert api.post(f"/api/v1/accounts/{account.id}/resume?held_posts=all").status_code == 422
        account.refresh_from_db()
        assert account.is_paused

    def test_mcp_resume_takes_held_posts(self, api, workspace, account, manager):
        import json

        rows = _held(workspace, account, manager, 1)
        account.set_paused(True, by=manager)
        envelope = self._mcp(api, "resume_account", {"account_id": str(account.id), "held_posts": "skip"})
        assert json.loads(envelope["result"]["content"][0]["text"])["skipped"] == 1
        rows[0].refresh_from_db()
        assert rows[0].status == PlatformPost.Status.DRAFT

    def test_mcp_refuses_an_unknown_choice_and_lists_it_in_the_schema(self, api, workspace, account, manager):
        account.set_paused(True, by=manager)
        envelope = self._mcp(api, "resume_account", {"account_id": str(account.id), "held_posts": "all"})
        assert "error" in envelope and "held_posts" in envelope["error"]["message"]
        account.refresh_from_db()
        assert account.is_paused
        from apps.mcp.tools import get_tool

        schema = get_tool("resume_account").input_schema["properties"]["held_posts"]
        assert schema["enum"] == ["requeue", "publish_now", "skip"] and schema["default"] == "requeue"

    def test_an_agent_resume_is_audited_with_the_choice(self, api, workspace, account, manager):
        from apps.autonomy.models import AgentAction

        _held(workspace, account, manager, 2)
        account.set_paused(True, by=manager)
        api.post(f"/api/v1/accounts/{account.id}/resume?held_posts=publish_now")
        action = AgentAction.objects.get(workspace=workspace, operation="account.resume")
        assert action.actor_type == "agent" and action.api_key is not None
        assert "Held posts: publish_now (2 published now)" in action.summary

    def test_an_agent_pause_is_audited_once(self, api, workspace, account):
        from apps.autonomy.models import AgentAction

        api.post(f"/api/v1/accounts/{account.id}/pause")
        api.post(f"/api/v1/accounts/{account.id}/pause")
        assert AgentAction.objects.filter(workspace=workspace, operation="account.pause").count() == 1
