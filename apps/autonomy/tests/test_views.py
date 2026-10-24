"""The Activity page, its tray and actions, the settings page, and what clients see."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from apps.accounts.models import User
from apps.autonomy import service
from apps.autonomy.actors import human, system
from apps.autonomy.models import ActionType, AgentAction, AutonomyPolicy
from apps.composer.models import PlatformPost, Post
from apps.members.models import WorkspaceMembership

from .conftest import schedule_via_rest, set_policy

pytestmark = pytest.mark.django_db


@pytest.fixture
def owner_client(world):
    client = Client()
    client.force_login(world.owner)
    return client


def member_client(world, role):
    user = User.objects.create_user(
        email=f"{role}@example.com", password="x" * 12, name=role.title(), tos_accepted_at=timezone.now()
    )
    WorkspaceMembership.objects.create(user=user, workspace=world.workspace, workspace_role=role)
    client = Client()
    client.force_login(user)
    return client


def url(world, path=""):
    return f"/workspace/{world.workspace.id}/activity/{path}"


class TestActivity:
    def make_entries(self, world):
        schedule_via_rest(world.full, world.account1)  # agent, held
        set_policy(world.workspace, publish_level="full_auto", undo_delay_minutes=0)
        schedule_via_rest(world.full, world.account2)  # agent, executed
        service.record(
            workspace=world.workspace,
            actor=human(world.owner),
            action_type=ActionType.REPLY,
            operation="reply.send",
            decision="executed",
            account=world.account1,
            summary="Sent from the inbox.",
        )
        service.record(
            workspace=world.workspace,
            actor=system("Evergreen recycling"),
            action_type=ActionType.RECYCLE_EVERGREEN,
            operation="evergreen.recycle",
            decision="blocked",
            account=world.account2,
            summary="Blocked because it has a blocked phrase.",
        )

    def test_it_lists_everything_newest_first(self, world, owner_client):
        self.make_entries(world)
        page = owner_client.get(url(world), secure=True)
        assert page.status_code == 200
        html = page.content.decode()
        for needle in ("Evergreen recycling", "Sent from the inbox.", "Held for review", "full ·"):
            assert needle in html

    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("actor=agent", 2),
            ("actor=human", 1),
            ("actor=system", 1),
            ("decision=held", 1),
            ("decision=blocked", 1),
            ("action=reply", 1),
            ("date_from=2000-01-01", 4),
            ("date_to=2000-01-01", 0),
            ("actor=agent&decision=executed", 1),
        ],
    )
    def test_filters(self, world, owner_client, query, expected):
        self.make_entries(world)
        page = owner_client.get(url(world) + "?" + query, secure=True)
        assert len(page.context["page"].object_list) == expected

    def test_the_account_filter(self, world, owner_client):
        self.make_entries(world)
        page = owner_client.get(url(world) + f"?account={world.account1.id}", secure=True)
        assert {a.social_account_id for a in page.context["page"].object_list} == {world.account1.id}

    def test_a_bad_filter_value_is_ignored(self, world, owner_client):
        self.make_entries(world)
        page = owner_client.get(url(world) + "?actor=robot&account=nope&date_from=zzz", secure=True)
        assert page.status_code == 200 and len(page.context["page"].object_list) == 4

    def test_the_detail_shows_the_reasoning_and_the_diff(self, world, owner_client):
        action = service.record(
            workspace=world.workspace,
            actor=system("Evergreen recycling"),
            action_type=ActionType.RECYCLE_EVERGREEN,
            operation="evergreen.recycle",
            decision="held",
            summary="Held for review because it contains a link.",
            source_text="Ten tips for better sleep",
            final_text="Ten fresh tips for better rest",
        )
        AgentAction.objects.filter(pk=action.pk).update(
            guardrail_results=[
                {"name": "links", "label": "Links", "passed": False, "reason": "contains a link", "blocking": False},
                {"name": "daily_cap", "label": "Daily limit", "passed": True, "reason": "", "blocking": False},
            ]
        )
        html = owner_client.get(url(world, f"{action.id}/"), secure=True).content.decode()
        assert "Failed: Links" in html and "contains a link" in html and "Passed: Daily limit" in html
        assert "<ins" in html and "<del" in html  # the word diff

    def test_other_workspaces_entries_are_not_reachable(self, world, owner_client):
        entry = service.record(
            workspace=world.other_workspace,
            actor=system("Evergreen recycling"),
            action_type=ActionType.RECYCLE_EVERGREEN,
            operation="evergreen.recycle",
            decision="executed",
        )
        assert owner_client.get(url(world, f"{entry.id}/"), secure=True).status_code == 404

    def test_strangers_and_clients_are_kept_out(self, world):
        assert Client().get(url(world), secure=True).status_code == 302
        stranger = Client()
        stranger.force_login(world.other_owner)
        assert stranger.get(url(world), secure=True).status_code == 403
        assert member_client(world, "client").get(url(world), secure=True).status_code == 403

    def test_the_tray_shows_pending_items_and_undo_works_from_it(self, world, owner_client):
        set_policy(world.workspace, publish_level="auto_guardrails")
        body = schedule_via_rest(world.full, world.account1).json()
        action = AgentAction.objects.get(post_id=body["id"])
        tray = owner_client.get(url(world, "tray/"), secure=True).content.decode()
        assert "Going out soon" in tray and "A calm update" in tray and "Undo" in tray and "Hold" in tray
        response = owner_client.post(
            url(world, f"{action.id}/undo/"), {"from": "tray"}, secure=True, headers={"HX-Request": "true"}
        )
        assert response.status_code == 200 and "A calm update" not in response.content.decode()
        assert PlatformPost.objects.get(post_id=body["id"]).status == "draft"

    def test_hold_then_release_through_the_page(self, world, owner_client):
        set_policy(world.workspace, publish_level="auto_guardrails")
        body = schedule_via_rest(world.full, world.account1).json()
        action = AgentAction.objects.get(post_id=body["id"])
        owner_client.post(url(world, f"{action.id}/hold/"), secure=True)
        assert AgentAction.objects.get(pk=action.pk).held_at is not None
        assert "On hold" in owner_client.get(url(world, "tray/"), secure=True).content.decode()
        owner_client.post(url(world, f"{action.id}/release/"), secure=True)
        assert AgentAction.objects.get(pk=action.pk).hold_released_at is not None

    def test_review_actions_need_the_right_permission(self, world):
        body = schedule_via_rest(world.full, world.account1).json()
        held = AgentAction.objects.get(post_id=body["id"])
        editor = member_client(world, "editor")  # can edit but not approve or publish
        assert editor.post(url(world, f"{held.id}/approve/"), secure=True).status_code == 403
        assert editor.post(url(world, f"{held.id}/dismiss/"), secure=True).status_code == 403
        assert PlatformPost.objects.get(post_id=body["id"]).status == "draft"

    def test_a_manager_approves_a_held_post_from_the_detail_page(self, world):
        body = schedule_via_rest(world.full, world.account1).json()
        held = AgentAction.objects.get(post_id=body["id"])
        manager = member_client(world, "manager")
        response = manager.post(url(world, f"{held.id}/approve/"), secure=True)
        assert response.status_code == 302
        assert PlatformPost.objects.get(post_id=body["id"]).status == "scheduled"

    def test_an_unknown_verb_is_a_404(self, world, owner_client):
        entry = service.record(
            workspace=world.workspace,
            actor=system("x"),
            action_type=ActionType.REPLY,
            operation="reply.send",
            decision="executed",
        )
        assert owner_client.post(url(world, f"{entry.id}/explode/"), secure=True).status_code == 404

    def test_crisis_pause_from_the_activity_page(self, world, owner_client):
        assert "Pause all autonomous actions" in owner_client.get(url(world), secure=True).content.decode()
        owner_client.post(url(world, "crisis-pause/"), {"crisis_paused": "1"}, secure=True)
        assert AutonomyPolicy.objects.get(workspace=world.workspace).crisis_paused
        assert "Crisis pause is on" in owner_client.get(url(world), secure=True).content.decode()
        owner_client.post(url(world, "crisis-pause/"), {"crisis_paused": "0"}, secure=True)
        assert not AutonomyPolicy.objects.get(workspace=world.workspace).crisis_paused
        assert member_client(world, "editor").post(url(world, "crisis-pause/"), secure=True).status_code == 403


class TestSettings:
    def page(self, world, client):
        return client.get(url(world, "settings/"), secure=True)

    def post(self, world, client, **data):
        return client.post(url(world, "settings/"), data, secure=True, follow=True)

    def test_the_page_explains_each_level_in_plain_language(self, world, owner_client):
        html = self.page(world, owner_client).content.decode()
        for needle in (
            "Draft content",
            "Schedule and publish posts",
            "Reply to comments and DMs",
            "Recycle evergreen posts",
            "Agents cannot schedule or publish",
            "Pause all autonomous actions",
            "Per-account overrides",
            "Blocked words and phrases",
        ):
            assert needle in html

    def test_saving_the_dial(self, world, owner_client):
        response = self.post(
            world,
            owner_client,
            section="levels",
            draft_level="auto_guardrails",
            publish_level="auto_guardrails",
            reply_level="off",
            recycle_level="draft_only",
            undo_delay_minutes="10",
        )
        assert "Autonomy settings saved." in response.content.decode()
        policy = AutonomyPolicy.objects.get(workspace=world.workspace)
        assert (policy.publish_level, policy.reply_level, policy.recycle_level) == (
            "auto_guardrails",
            "off",
            "draft_only",
        )
        assert policy.undo_delay_minutes == 10
        assert AgentAction.objects.get(operation="autonomy.policy_update").actor_user == world.owner

    def test_bad_values_are_refused_with_a_message(self, world, owner_client):
        response = self.post(
            world,
            owner_client,
            section="levels",
            draft_level="anything",
            publish_level="draft_only",
            reply_level="draft_only",
            recycle_level="per_item",
            undo_delay_minutes="5",
        )
        assert "draft_level must be one of" in response.content.decode()
        assert not AutonomyPolicy.objects.filter(workspace=world.workspace).exists()

    def test_per_account_overrides(self, world, owner_client):
        data = {"section": "overrides"}
        for account in (world.account1, world.account2):
            for field in ("draft_level", "publish_level", "reply_level", "recycle_level", "max_posts_per_day"):
                data[f"{account.id}__{field}"] = ""
        data[f"{world.account1.id}__publish_level"] = "full_auto"
        data[f"{world.account1.id}__max_posts_per_day"] = "1"
        self.post(world, owner_client, **data)
        from apps.autonomy.policy import resolve_level

        assert resolve_level(world.workspace, world.account1, ActionType.PUBLISH_POSTS) == ("full_auto", "account")
        assert resolve_level(world.workspace, world.account2, ActionType.PUBLISH_POSTS) == ("draft_only", "workspace")
        data[f"{world.account1.id}__publish_level"] = ""
        data[f"{world.account1.id}__max_posts_per_day"] = ""
        self.post(world, owner_client, **data)
        assert resolve_level(world.workspace, world.account1, ActionType.PUBLISH_POSTS) == ("draft_only", "workspace")
        assert (
            AgentAction.objects.filter(operation="autonomy.account_override").count() == 2
        )  # unchanged saves log nothing

    def test_guardrail_editors(self, world, owner_client):
        self.post(
            world,
            owner_client,
            section="guardrails",
            blocked_phrases="cure\nGuaranteed\ncure",
            review_links="on",
            max_posts_per_day="2",
            max_replies_per_day="9",
            quiet_hours_enabled="on",
            quiet_start="22:00",
            quiet_end="07:30",
            ai_risk_check="on",
            ai_risk_threshold="0.7",
            ai_voice_threshold="0.5",
        )
        policy = AutonomyPolicy.objects.get(workspace=world.workspace)
        assert policy.blocked_phrases == ["cure", "Guaranteed"]
        assert (policy.review_links, policy.review_mentions, policy.review_prices) == (True, False, False)
        assert (policy.max_posts_per_day, policy.max_replies_per_day) == (2, 9)
        assert policy.quiet_hours_enabled and policy.quiet_end.strftime("%H:%M") == "07:30"
        assert policy.ai_risk_check and not policy.ai_voice_check and policy.ai_risk_threshold == 0.7

    def test_quiet_hours_need_both_times(self, world, owner_client):
        response = self.post(
            world,
            owner_client,
            section="guardrails",
            quiet_hours_enabled="on",
            max_posts_per_day="3",
            max_replies_per_day="3",
            ai_risk_threshold="0.5",
            ai_voice_threshold="0.5",
        )
        assert "start and end time" in response.content.decode()

    def test_crisis_pause_with_confirmation_markup_and_toggle(self, world, owner_client):
        page = self.page(world, owner_client).content.decode()
        # the pause asks first, in the shared dialog rather than the browser's confirm()
        assert 'x-pd-dialog="confirmPause"' in page and "Pause every autonomous action?" in page
        assert "confirm(" not in page
        paused_page = self.post(world, owner_client, section="crisis", crisis_paused="1").content.decode()
        policy = AutonomyPolicy.objects.get(workspace=world.workspace)
        assert policy.crisis_paused and policy.crisis_paused_by == world.owner and policy.crisis_paused_at
        assert "Resume autonomous actions" in paused_page
        # the flash message shows once (the layout's list), not again inside the page
        assert paused_page.count("Every autonomous action is stopped and queued ones are held.") == 1
        self.post(world, owner_client, section="crisis", crisis_paused="0")
        policy.refresh_from_db()
        assert not policy.crisis_paused and policy.crisis_paused_by is None

    def test_a_manager_can_pause_but_not_reconfigure(self, world):
        manager = member_client(world, "manager")
        assert self.page(world, manager).status_code == 200
        self.post(world, manager, section="crisis", crisis_paused="1")
        assert AutonomyPolicy.objects.get(workspace=world.workspace).crisis_paused
        assert self.post(world, manager, section="levels", publish_level="full_auto").status_code == 403
        assert AutonomyPolicy.objects.get(workspace=world.workspace).publish_level == "draft_only"

    def test_an_editor_cannot_pause(self, world):
        editor = member_client(world, "editor")
        assert self.post(world, editor, section="crisis", crisis_paused="1").status_code == 403

    def test_non_members_get_nothing(self, world):
        stranger = Client()
        stranger.force_login(world.other_owner)
        assert self.page(world, stranger).status_code == 403


class TestClientVisibility:
    def portal(self, world):
        client = Client()
        client.force_login(world.client_user)
        session = client.session
        session["is_portal_session"] = True
        session["portal_workspace_id"] = str(world.workspace.id)
        session.save()
        return client

    def entries(self, world):
        made = {}
        for name, actor, decision, kwargs in [
            ("agent_ok", system("Zapier bot"), "executed", {}),
            ("agent_held", system("Zapier bot"), "held", {}),
            ("agent_blocked", system("Zapier bot"), "blocked", {}),
            ("undone", system("Zapier bot"), "executed", {"undone": True}),
            ("human", human(world.owner), "executed", {}),
            ("pending", system("Zapier bot"), "executed", {"waiting": True}),
            ("still_queued", system("Zapier bot"), "executed", {"status": "scheduled"}),
            ("taken_down", system("Zapier bot"), "executed", {"deleted": True}),
        ]:
            post = Post.objects.create(workspace=world.workspace, caption=f"Text for {name}", author=world.owner)
            pp = PlatformPost.objects.create(
                post=post, social_account=world.account1, status=kwargs.get("status", "published")
            )
            action = service.record(
                workspace=world.workspace,
                actor=actor,
                action_type=ActionType.PUBLISH_POSTS,
                operation="post.schedule",
                decision=decision,
                final_text=f"Text for {name}",
                account=world.account1,
                post=post,
                platform_post=pp,
            )
            if kwargs.get("deleted"):
                AgentAction.objects.filter(pk=action.pk).update(platform_deleted_at=timezone.now())
            if kwargs.get("undone"):
                AgentAction.objects.filter(pk=action.pk).update(undone_at=timezone.now())
            if kwargs.get("waiting"):
                AgentAction.objects.filter(pk=action.pk).update(release_at=timezone.now() + timedelta(minutes=5))
            made[name] = action
        return made

    def test_nothing_is_shown_until_the_workspace_turns_it_on(self, world):
        self.entries(world)
        assert list(service.client_visible_actions(world.workspace)) == []
        assert "Automated activity" not in self.portal(world).get("/portal/activity/", secure=True).content.decode()

    def test_clients_see_only_what_actually_went_out_automatically(self, world):
        made = self.entries(world)
        set_policy(world.workspace, client_visible=True)
        visible = list(service.client_visible_actions(world.workspace))
        assert [a.pk for a in visible] == [made["agent_ok"].pk]

    def test_the_portal_page_hides_the_internals(self, world):
        self.entries(world)
        set_policy(world.workspace, client_visible=True)
        html = self.portal(world).get("/portal/activity/", secure=True).content.decode()
        assert "Automated activity" in html and "Text for agent_ok" in html
        for private in (
            "Text for agent_held",
            "Text for agent_blocked",
            "Text for human",
            "Text for undone",
            "Zapier bot",
        ):
            assert private not in html

    def test_the_toggle_is_a_setting(self, world, owner_client):
        owner_client.post(url(world, "settings/"), {"section": "clients", "client_visible": "on"}, secure=True)
        assert AutonomyPolicy.objects.get(workspace=world.workspace).client_visible
        owner_client.post(url(world, "settings/"), {"section": "clients"}, secure=True)
        assert not AutonomyPolicy.objects.get(workspace=world.workspace).client_visible
