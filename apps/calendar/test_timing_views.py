"""The web side of best times and evergreen: heatmap, slot suggestions, chips, the library."""

from __future__ import annotations

import datetime as dt
import re
from datetime import timedelta

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.calendar import services
from apps.calendar.models import EvergreenItem, PostingSlot
from apps.composer.models import PlatformPost, Post
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace


def make_user(email):
    user = User.objects.create_user(email=email, password="testpass123", tos_accepted_at=timezone.now())
    # The signup signal provisions an organization and workspace of its own; start clean.
    auto = list(OrgMembership.objects.filter(user=user).values_list("organization_id", flat=True))
    WorkspaceMembership.objects.filter(user=user).delete()
    OrgMembership.objects.filter(user=user).delete()
    Organization.objects.filter(id__in=auto).delete()
    return user


def join(user, org, workspace, role):
    OrgMembership.objects.create(user=user, organization=org, org_role="owner" if role == "owner" else "member")
    WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role=role)


@pytest.fixture
def world(db):
    org = Organization.objects.create(name="Org")
    ws = Workspace.objects.create(organization=org, name="WS", timezone="Europe/Paris")
    owner, editor, viewer = make_user("o@example.com"), make_user("e@example.com"), make_user("v@example.com")
    join(owner, org, ws, "owner")
    join(editor, org, ws, "editor")
    join(viewer, org, ws, "viewer")
    account = SocialAccount.objects.create(
        workspace=ws, platform="bluesky", account_platform_id="a", account_name="Sky", connection_status="connected"
    )
    other_org = Organization.objects.create(name="Other")
    other_ws = Workspace.objects.create(organization=other_org, name="Theirs")
    stranger = make_user("s@example.com")
    join(stranger, other_org, other_ws, "owner")
    foreign = SocialAccount.objects.create(
        workspace=other_ws, platform="bluesky", account_platform_id="f", connection_status="connected"
    )

    def client_for(user):
        c = Client()
        c.force_login(user)
        return c

    return type(
        "World",
        (),
        {
            "ws": ws,
            "account": account,
            "foreign": foreign,
            "other_ws": other_ws,
            "owner": client_for(owner),
            "editor": client_for(editor),
            "viewer": client_for(viewer),
            "stranger": client_for(stranger),
            "owner_user": owner,
        },
    )


def url(name, world, **kwargs):
    return reverse(f"calendar:{name}", kwargs={"workspace_id": world.ws.id, **kwargs})


def published_pp(world, caption="Tip"):
    post = Post.objects.create(workspace=world.ws, caption=caption, author=world.owner_user)
    return PlatformPost.objects.create(
        post=post,
        social_account=world.account,
        status="published",
        published_at=timezone.now() - timedelta(days=60),
    )


class TestHeatmapPanel:
    def test_renders_the_labelled_heatmap_without_inline_styles(self, world):
        r = world.owner.get(url("best_times_panel", world), {"social_account_id": world.account.id})
        html = r.content.decode()
        assert r.status_code == 200
        assert "Based on general defaults" in html and "Europe/Paris" in html
        assert html.count("<tr>") >= 8 and "Suggest slots" in html and "Top times" in html
        assert "bg-heat-" in html and 'scope="row"' in html and "<caption" in html
        assert not re.search(r"\sstyle=", html)

    def test_viewer_sees_the_heatmap_but_not_the_suggest_button(self, world):
        html = world.viewer.get(
            url("best_times_panel", world), {"social_account_id": world.account.id}
        ).content.decode()
        assert "Based on general defaults" in html and "Suggest slots" not in html

    def test_other_workspaces_account_is_a_404_and_junk_ids_too(self, world):
        assert (
            world.owner.get(url("best_times_panel", world), {"social_account_id": world.foreign.id}).status_code == 404
        )
        assert world.owner.get(url("best_times_panel", world), {"social_account_id": "junk"}).status_code == 404
        assert (
            world.stranger.get(url("best_times_panel", world), {"social_account_id": world.account.id}).status_code
            == 403
        )


class TestSuggestSlots:
    def test_preview_shows_changes_and_writes_nothing(self, world):
        PostingSlot.objects.create(social_account=world.account, day_of_week=6, time=dt.time(3, 7))
        r = world.owner.get(url("suggest_slots_preview", world), {"social_account_id": world.account.id, "count": 4})
        html = r.content.decode()
        assert r.status_code == 200 and "Replace slots" in html and "Sun 3:07 AM" in html
        assert html.count('name="slots"') == 4 and not re.search(r"\sstyle=", html)
        assert PostingSlot.objects.filter(social_account=world.account).count() == 1

    def test_add_mode_preview_keeps_existing_slots(self, world):
        PostingSlot.objects.create(social_account=world.account, day_of_week=6, time=dt.time(3, 7))
        html = world.owner.get(
            url("suggest_slots_preview", world), {"social_account_id": world.account.id, "mode": "add"}
        ).content.decode()
        assert "Add slots" in html and '<dd class="inline">nothing</dd>' in html

    def test_apply_replaces_and_triggers_a_grid_refresh(self, world):
        PostingSlot.objects.create(social_account=world.account, day_of_week=6, time=dt.time(3, 7))
        r = world.owner.post(
            url("suggest_slots_apply", world),
            {"social_account_id": world.account.id, "mode": "replace", "slots": ["1-9", "3-14", "bad", "9-99"]},
            HTTP_HX_REQUEST="true",
        )
        assert r.status_code == 204 and "slotsUpdated" in r["HX-Trigger"]
        assert sorted(PostingSlot.objects.filter(social_account=world.account).values_list("day_of_week", "time")) == [
            (1, dt.time(9, 0)),
            (3, dt.time(14, 0)),
        ]

    def test_apply_with_nothing_valid_is_a_400_and_changes_nothing(self, world):
        PostingSlot.objects.create(social_account=world.account, day_of_week=6, time=dt.time(3, 7))
        r = world.owner.post(url("suggest_slots_apply", world), {"social_account_id": world.account.id, "slots": ["x"]})
        assert r.status_code == 400
        assert PostingSlot.objects.filter(social_account=world.account).count() == 1

    def test_editor_without_manage_social_accounts_is_refused(self, world):
        for name, method in (("suggest_slots_preview", "get"), ("suggest_slots_apply", "post")):
            r = getattr(world.editor, method)(
                url(name, world), {"social_account_id": world.account.id, "slots": ["1-9"]}
            )
            assert r.status_code == 403, name
        assert not PostingSlot.objects.filter(social_account=world.account).exists()

    def test_cannot_touch_another_workspaces_account(self, world):
        r = world.owner.post(
            url("suggest_slots_apply", world), {"social_account_id": world.foreign.id, "slots": ["1-9"]}
        )
        assert r.status_code == 404
        assert not PostingSlot.objects.filter(social_account=world.foreign).exists()


class TestServiceSlotPlanning:
    def test_replace_keeps_slots_that_are_already_suggested(self, world):
        best, suggestions = services.suggest_slots(world.account, 3)
        first = suggestions[0]
        PostingSlot.objects.create(social_account=world.account, day_of_week=first.weekday, time=dt.time(first.hour, 0))
        PostingSlot.objects.create(social_account=world.account, day_of_week=6, time=dt.time(4, 4))
        to_add, to_remove, kept = services.plan_slot_changes(world.account, suggestions, "replace")
        assert len(to_add) == 2 and [s.time for s in to_remove] == [dt.time(4, 4)] and len(kept) == 1
        assert services.apply_suggested_slots(world.account, suggestions, "replace") == (2, 1)
        assert services.apply_suggested_slots(world.account, suggestions, "replace") == (0, 0)

    def test_count_is_clamped_and_empty_suggestions_refused(self, world):
        _best, suggestions = services.suggest_slots(world.account, 500)
        assert len(suggestions) <= services.SUGGESTED_SLOTS_MAX
        with pytest.raises(ValueError):
            services.apply_suggested_slots(world.account, [])
        with pytest.raises(ValueError):
            services.plan_slot_changes(world.account, suggestions, "nuke")


class TestChips:
    def test_chips_for_selected_accounts_in_the_workspace_zone(self, world):
        r = world.editor.get(
            url("best_time_chips", world), {"selected_accounts": f"{world.account.id},junk,{world.foreign.id}"}
        )
        html = r.content.decode()
        assert r.status_code == 200 and "data-best-time-chip" in html and "general defaults" in html
        assert len(re.findall(r"data-best-time-chip", html)) == 3
        assert not re.search(r"\sstyle=", html)
        assert str(world.foreign.id) not in html

    def test_nothing_selected_renders_nothing(self, world):
        assert world.editor.get(url("best_time_chips", world), {"selected_accounts": ""}).content.strip() == b""

    def test_non_members_are_refused(self, world):
        assert world.stranger.get(url("best_time_chips", world)).status_code == 403


class TestEvergreenLibrary:
    def add(self, client, world, pp, **extra):
        return client.post(
            url("evergreen_add", world), {"platform_post_id": pp.id, "interval_days": 45, "wording": "ai", **extra}
        )

    def test_add_form_and_add_flow(self, world):
        pp = published_pp(world)
        form = world.editor.get(url("evergreen_add_form", world), {"platform_post_id": pp.id})
        assert form.status_code == 200 and b"Add to evergreen" in form.content
        assert b"pre_approved" not in form.content  # editors can't approve
        assert not re.search(r"\sstyle=", form.content.decode())
        r = self.add(world.editor, world, pp, new_category="Evergreen: Tips")
        assert r.status_code == 200 and b"Added to evergreen" in r.content
        item = EvergreenItem.objects.get()
        assert (item.interval_days, item.vary_with_ai, item.category.name) == (45, True, "Evergreen: Tips")

    def test_adding_twice_shows_the_error_in_the_dialog(self, world):
        pp = published_pp(world)
        self.add(world.editor, world, pp)
        r = self.add(world.editor, world, pp)
        assert r.status_code == 200 and b"already in the evergreen library" in r.content
        assert EvergreenItem.objects.count() == 1

    def test_viewer_cannot_add_and_editor_cannot_pre_approve(self, world):
        pp = published_pp(world)
        assert self.add(world.viewer, world, pp).status_code == 403
        assert world.viewer.get(url("evergreen_add_form", world), {"platform_post_id": pp.id}).status_code == 403
        assert self.add(world.editor, world, pp, pre_approved="on").status_code == 403
        assert not EvergreenItem.objects.exists()
        assert self.add(world.owner, world, pp, pre_approved="on").status_code == 200
        assert EvergreenItem.objects.get().pre_approved_by == world.owner_user

    def test_unpublished_posts_are_refused_and_foreign_posts_404(self, world):
        draft = PlatformPost.objects.create(
            post=Post.objects.create(workspace=world.ws, caption="d"), social_account=world.account, status="draft"
        )
        assert b"published" in self.add(world.editor, world, draft).content
        foreign_post = Post.objects.create(workspace=world.other_ws, caption="x")
        foreign = PlatformPost.objects.create(post=foreign_post, social_account=world.foreign, status="published")
        assert self.add(world.editor, world, foreign).status_code == 404

    def test_list_filters_and_scopes_to_the_workspace(self, world):
        EvergreenItem.objects.create(workspace=world.ws, social_account=world.account, caption="my tip")
        EvergreenItem.objects.create(
            workspace=world.ws, social_account=world.account, caption="paused tip", is_paused=True
        )
        EvergreenItem.objects.create(workspace=world.other_ws, social_account=world.foreign, caption="their tip")
        html = world.viewer.get(url("evergreen_list", world)).content.decode()
        assert "my tip" in html and "paused tip" in html and "their tip" not in html
        active = world.viewer.get(url("evergreen_list", world), {"status": "active"}).content.decode()
        assert "my tip" in active and "paused tip" not in active
        paused = world.viewer.get(url("evergreen_list", world), {"status": "paused"}).content.decode()
        assert "paused tip" in paused and "my tip" not in paused
        assert world.viewer.get(url("evergreen_list", world), {"category": "junk"}).status_code == 200
        assert world.stranger.get(url("evergreen_list", world)).status_code == 403

    def test_detail_shows_history_and_hides_foreign_items(self, world):
        item = EvergreenItem.objects.create(workspace=world.ws, social_account=world.account, caption="tip")
        foreign = EvergreenItem.objects.create(workspace=world.other_ws, social_account=world.foreign, caption="x")
        assert b"Not recycled yet" in world.viewer.get(url("evergreen_detail", world, item_id=item.id)).content
        assert world.viewer.get(url("evergreen_detail", world, item_id=foreign.id)).status_code == 404

    def test_pause_resume_update_remove_need_create_posts_and_stay_in_scope(self, world):
        item = EvergreenItem.objects.create(workspace=world.ws, social_account=world.account, caption="tip")
        foreign = EvergreenItem.objects.create(workspace=world.other_ws, social_account=world.foreign, caption="x")
        assert world.viewer.post(url("evergreen_pause", world, item_id=item.id)).status_code == 403
        assert world.editor.post(url("evergreen_pause", world, item_id=foreign.id)).status_code == 404
        world.editor.post(url("evergreen_pause", world, item_id=item.id))
        item.refresh_from_db()
        assert item.is_paused
        world.editor.post(url("evergreen_pause", world, item_id=item.id), {"paused": "0"})
        item.refresh_from_db()
        assert not item.is_paused

        r = world.editor.post(url("evergreen_update", world, item_id=item.id), {"interval_days": 0})
        assert r.status_code == 400
        r = world.editor.post(
            url("evergreen_update", world, item_id=item.id), {"interval_days": 60, "max_repeats": 4, "wording": "ai"}
        )
        item.refresh_from_db()
        assert r.status_code == 302 and (item.interval_days, item.max_repeats, item.vary_with_ai) == (60, 4, True)
        assert (
            world.editor.post(
                url("evergreen_update", world, item_id=item.id), {"interval_days": 5, "pre_approved": "on"}
            ).status_code
            == 403
        )

        assert world.viewer.post(url("evergreen_remove", world, item_id=item.id)).status_code == 403
        assert world.editor.post(url("evergreen_remove", world, item_id=foreign.id)).status_code == 404
        world.editor.post(url("evergreen_remove", world, item_id=item.id))
        assert not EvergreenItem.objects.filter(pk=item.pk).exists()
        assert EvergreenItem.objects.filter(pk=foreign.pk).exists()

    def test_sent_tab_offers_add_to_evergreen_to_people_who_can_create_posts(self, world):
        published_pp(world)
        sent = url("publish_tab_sent", world)
        assert b"Add to evergreen" in world.editor.get(sent).content
        assert b"Add to evergreen" not in world.viewer.get(sent).content
