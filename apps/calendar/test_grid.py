"""Instagram grid preview: ordering, swap semantics, approvals and permissions."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.analytics.tests.helpers import make_account, make_member
from apps.approvals.policy import mark_final_approval
from apps.calendar import grid
from apps.calendar.models import Queue, QueueEntry
from apps.composer.models import PlatformPost, Post, PostMedia
from apps.media_library.models import MediaAsset
from apps.members.models import WorkspaceMembership
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db

Role = WorkspaceMembership.WorkspaceRole


@pytest.fixture
def workspace():
    org = Organization.objects.create(name="Agency")
    return Workspace.objects.create(organization=org, name="Acme", timezone="UTC")


@pytest.fixture
def owner(workspace):
    return make_member(workspace, "owner@example.com")


@pytest.fixture
def ig(workspace):
    return make_account(workspace, "instagram", "Acme IG")


@pytest.fixture
def picture(workspace):
    return MediaAsset.objects.create(
        organization=workspace.organization,
        workspace=workspace,
        file="grid/pic.jpg",
        filename="pic.jpg",
        media_type="image",
        mime_type="image/jpeg",
    )


def make_pp(workspace, account, *, status="scheduled", when=None, caption="post", author=None, picture=None, **extra):
    post = Post.objects.create(workspace=workspace, caption=caption, author=author)
    pp = PlatformPost.objects.create(
        post=post,
        social_account=account,
        status=status,
        scheduled_at=when if status != "published" else None,
        published_at=when if status == "published" else None,
        **extra,
    )
    if when and status != "published":
        post.scheduled_at = when
        post.save(update_fields=["scheduled_at"])
    if picture:
        PostMedia.objects.create(post=post, media_asset=picture, position=0)
    return pp


def hours(n):
    return timezone.now() + timedelta(hours=n)


def swap_url(workspace, account):
    return reverse("calendar:grid_swap", kwargs={"workspace_id": workspace.id, "account_id": account.id})


def move_url(workspace, account, pp):
    return reverse(
        "calendar:grid_move", kwargs={"workspace_id": workspace.id, "account_id": account.id, "platform_post_id": pp.id}
    )


def grid_url(workspace, account):
    return reverse("calendar:account_grid", kwargs={"workspace_id": workspace.id, "account_id": account.id})


def membership(user, workspace):
    return WorkspaceMembership.objects.get(user=user, workspace=workspace)


class TestOrdering:
    def test_upcoming_sit_above_published_furthest_first(self, workspace, owner, ig, picture):
        old = make_pp(workspace, ig, status="published", when=hours(-48), caption="old", picture=picture)
        new = make_pp(workspace, ig, status="published", when=hours(-2), caption="new", picture=picture)
        soon = make_pp(workspace, ig, when=hours(5), caption="soon", picture=picture)
        later = make_pp(workspace, ig, when=hours(30), caption="later", picture=picture)
        view = grid.build_grid(ig, owner, membership(owner, workspace))
        assert [t.id for t in view.tiles] == [later.id, soon.id, new.id, old.id]
        assert [t.state for t in view.tiles] == ["scheduled", "scheduled", "published", "published"]
        assert all(t.image_url for t in view.tiles)

    def test_drafts_with_a_suggested_time_take_a_place_and_others_list_below(self, workspace, owner, ig):
        placed = make_pp(workspace, ig, status="draft", caption="placed")
        placed.post.proposed_publish_at = hours(10)
        placed.post.save()
        loose = make_pp(workspace, ig, status="draft", caption="loose")
        sched = make_pp(workspace, ig, when=hours(3))
        view = grid.build_grid(ig, owner, membership(owner, workspace))
        assert [t.id for t in view.tiles] == [placed.id, sched.id]
        assert [t.state for t in view.tiles] == ["draft", "scheduled"]
        assert not view.tiles[0].movable
        assert [t.id for t in view.unscheduled] == [loose.id]

    def test_other_accounts_and_failed_posts_stay_off(self, workspace, owner, ig):
        other = make_account(workspace, "instagram", "Second")
        make_pp(workspace, other, when=hours(2))
        make_pp(workspace, ig, status="failed", when=hours(-1))
        make_pp(workspace, ig, status="rejected", when=hours(4))
        assert grid.build_grid(ig, owner, membership(owner, workspace)).tiles == []

    def test_neighbour_buttons_are_off_at_the_ends(self, workspace, owner, ig):
        a, b, c = (make_pp(workspace, ig, when=hours(h)) for h in (1, 2, 3))
        tiles = {t.id: t for t in grid.build_grid(ig, owner, membership(owner, workspace)).tiles}
        assert (tiles[c.id].can_later, tiles[c.id].can_earlier) == (False, True)
        assert (tiles[b.id].can_later, tiles[b.id].can_earlier) == (True, True)
        assert (tiles[a.id].can_later, tiles[a.id].can_earlier) == (True, False)


class TestSwap:
    def test_trades_times_and_nothing_else(self, workspace, owner, ig):
        t1, t2 = hours(5), hours(9)
        a = make_pp(workspace, ig, when=t1, caption="A")
        b = make_pp(workspace, ig, status="approved", when=t2, caption="B")
        queue = Queue.objects.create(workspace=workspace, name="q", social_account=ig)
        entry_a = QueueEntry.objects.create(queue=queue, post=a.post, assigned_slot_datetime=t1)
        grid.swap_times(ig, a.id, b.id, owner, membership(owner, workspace))
        a.refresh_from_db(), b.refresh_from_db(), a.post.refresh_from_db(), b.post.refresh_from_db()
        assert (a.scheduled_at, b.scheduled_at) == (t2, t1)
        assert (a.post.scheduled_at, b.post.scheduled_at) == (t2, t1)
        assert (a.status, b.status) == ("scheduled", "approved")
        assert (a.post.caption, b.post.caption) == ("A", "B")
        entry_a.refresh_from_db()
        assert entry_a.assigned_slot_datetime == t2

    def test_an_approved_post_keeps_its_approval_when_re_timed(self, workspace, owner, ig):
        workspace.approval_workflow_mode = "required_internal"
        workspace.save()
        a = make_pp(workspace, ig, status="approved", when=hours(5))
        b = make_pp(workspace, ig, status="approved", when=hours(8))
        for pp in (a, b):
            mark_final_approval(pp)
        grid.swap_times(ig, a.id, b.id, owner, membership(owner, workspace))
        a.refresh_from_db()
        from apps.approvals.policy import approval_allows_publish

        assert a.status == "approved" and a.approval_completed_at and approval_allows_publish(a)

    def test_a_post_without_its_own_time_uses_the_posts(self, workspace, owner, ig):
        a = make_pp(workspace, ig, when=hours(5))
        a.scheduled_at = None
        a.save(update_fields=["scheduled_at"])
        b = make_pp(workspace, ig, when=hours(9))
        post_time_of_a, time_of_b = a.post.scheduled_at, b.scheduled_at
        grid.swap_times(ig, a.id, b.id, owner, membership(owner, workspace))
        a.refresh_from_db(), b.refresh_from_db()
        assert (a.scheduled_at, b.scheduled_at) == (time_of_b, post_time_of_a)

    @pytest.mark.parametrize("status", ["draft", "failed", "published", "publishing", "rejected"])
    def test_only_movable_statuses_swap(self, workspace, owner, ig, status):
        a = make_pp(workspace, ig, when=hours(5))
        b = make_pp(workspace, ig, status=status, when=hours(9) if status != "published" else hours(-9))
        with pytest.raises(grid.GridError) as exc:
            grid.swap_times(ig, a.id, b.id, owner, membership(owner, workspace))
        assert exc.value.status in (400, 409)
        a.refresh_from_db()
        assert a.scheduled_at != b.scheduled_at

    def test_a_post_already_due_cannot_move(self, workspace, owner, ig):
        a = make_pp(workspace, ig, when=hours(5))
        b = make_pp(workspace, ig, when=hours(-1))
        with pytest.raises(grid.GridError) as exc:
            grid.swap_times(ig, a.id, b.id, owner, membership(owner, workspace))
        assert exc.value.status == 409

    def test_posts_must_be_on_this_account(self, workspace, owner, ig):
        other = make_account(workspace, "instagram", "Second")
        a = make_pp(workspace, ig, when=hours(5))
        b = make_pp(workspace, other, when=hours(9))
        with pytest.raises(grid.GridError) as exc:
            grid.swap_times(ig, a.id, b.id, owner, membership(owner, workspace))
        assert exc.value.status == 404

    def test_a_post_cannot_swap_with_itself(self, workspace, owner, ig):
        a = make_pp(workspace, ig, when=hours(5))
        with pytest.raises(grid.GridError):
            grid.swap_times(ig, a.id, a.id, owner, membership(owner, workspace))


class TestPermissions:
    def test_authors_move_their_own_but_not_others(self, workspace, owner, ig):
        contributor = make_member(workspace, "c@example.com", role=Role.CONTRIBUTOR)
        mine = make_pp(workspace, ig, when=hours(5), author=contributor)
        mine2 = make_pp(workspace, ig, when=hours(7), author=contributor)
        theirs = make_pp(workspace, ig, when=hours(9), author=owner)
        m = membership(contributor, workspace)
        grid.swap_times(ig, mine.id, mine2.id, contributor, m)
        with pytest.raises(grid.GridError) as exc:
            grid.swap_times(ig, mine.id, theirs.id, contributor, m)
        assert exc.value.status == 403
        theirs.refresh_from_db()
        assert theirs.scheduled_at != mine.scheduled_at

    def test_tiles_show_who_can_move(self, workspace, owner, ig):
        contributor = make_member(workspace, "c@example.com", role=Role.CONTRIBUTOR)
        mine = make_pp(workspace, ig, when=hours(5), author=contributor)
        theirs = make_pp(workspace, ig, when=hours(9), author=owner)
        tiles = {t.id: t for t in grid.build_grid(ig, contributor, membership(contributor, workspace)).tiles}
        assert tiles[mine.id].movable and not tiles[theirs.id].movable
        owner_tiles = grid.build_grid(ig, owner, membership(owner, workspace)).tiles
        assert all(t.movable for t in owner_tiles)

    def test_viewers_cannot_swap_through_the_endpoint(self, client, workspace, owner, ig):
        viewer = make_member(workspace, "v@example.com", role=Role.VIEWER)
        a = make_pp(workspace, ig, when=hours(5), author=owner)
        b = make_pp(workspace, ig, when=hours(9), author=owner)
        client.force_login(viewer)
        assert client.get(grid_url(workspace, ig)).status_code == 200
        r = client.post(swap_url(workspace, ig), {"first": str(a.id), "second": str(b.id)}, follow=True)
        assert b"Permission denied" in r.content
        a.refresh_from_db(), b.refresh_from_db()
        assert a.scheduled_at < b.scheduled_at

    def test_non_members_cannot_see_or_swap(self, client, workspace, ig):
        outsider_ws = Workspace.objects.create(organization=workspace.organization, name="Other")
        outsider = make_member(outsider_ws, "o@example.com")
        client.force_login(outsider)
        assert client.get(grid_url(workspace, ig)).status_code == 403
        assert client.post(swap_url(workspace, ig), {"first": "x", "second": "y"}).status_code == 403

    def test_an_account_of_another_workspace_is_404(self, client, workspace, owner):
        other = Workspace.objects.create(organization=workspace.organization, name="Other")
        foreign = make_account(other, "instagram", "Foreign")
        client.force_login(owner)
        assert client.get(grid_url(workspace, foreign)).status_code == 404

    def test_platforms_without_a_grid_are_404(self, client, workspace, owner):
        li = make_account(workspace, "linkedin_company", "LI")
        client.force_login(owner)
        assert client.get(grid_url(workspace, li)).status_code == 404
        assert client.post(swap_url(workspace, li), {}).status_code == 404

    def test_the_calendar_rule_is_one_function(self, workspace, owner, ig):
        pp = make_pp(workspace, ig, status="published", when=hours(-1), author=owner)
        from apps.calendar.services import reschedule_denial

        assert reschedule_denial(pp, owner, membership(owner, workspace))[0] == 400


class TestMove:
    def test_earlier_and_later_swap_with_the_neighbour(self, workspace, owner, ig):
        a, b, c = (make_pp(workspace, ig, when=hours(h)) for h in (1, 2, 3))
        m = membership(owner, workspace)
        times = {pp.id: pp.scheduled_at for pp in (a, b, c)}
        grid.move(ig, c.id, "earlier", owner, m)
        c.refresh_from_db(), b.refresh_from_db()
        assert (c.scheduled_at, b.scheduled_at) == (times[b.id], times[c.id])
        grid.move(ig, c.id, "later", owner, m)
        c.refresh_from_db()
        assert c.scheduled_at == times[c.id]

    def test_there_is_nothing_beyond_the_ends(self, workspace, owner, ig):
        a, b = (make_pp(workspace, ig, when=hours(h)) for h in (1, 2))
        m = membership(owner, workspace)
        with pytest.raises(grid.GridError) as exc:
            grid.move(ig, a.id, "earlier", owner, m)
        assert exc.value.status == 409
        with pytest.raises(grid.GridError):
            grid.move(ig, b.id, "later", owner, m)
        with pytest.raises(grid.GridError):
            grid.move(ig, a.id, "sideways", owner, m)

    def test_skips_neighbours_the_person_may_not_move(self, workspace, owner, ig):
        contributor = make_member(workspace, "c@example.com", role=Role.CONTRIBUTOR)
        a = make_pp(workspace, ig, when=hours(1), author=contributor)
        make_pp(workspace, ig, when=hours(2), author=owner)
        c = make_pp(workspace, ig, when=hours(3), author=contributor)
        t_a, t_c = a.scheduled_at, c.scheduled_at
        grid.move(ig, c.id, "earlier", contributor, membership(contributor, workspace))
        a.refresh_from_db(), c.refresh_from_db()
        assert (a.scheduled_at, c.scheduled_at) == (t_c, t_a)


class TestViews:
    def test_page_shows_marked_tiles_and_keyboard_buttons(self, client, workspace, owner, ig, picture):
        make_pp(workspace, ig, status="published", when=hours(-3), caption="Out", picture=picture)
        make_pp(workspace, ig, when=hours(4), caption="Soon", picture=picture)
        make_pp(workspace, ig, when=hours(8), caption="Later", picture=picture)
        client.force_login(owner)
        html = client.get(grid_url(workspace, ig)).content.decode()
        assert html.count("border-dashed") >= 2 and "Scheduled &middot;" in html
        assert html.count('draggable="true"') == 2
        assert "Move this post earlier" in html and "Move this post later" in html
        assert 'role="status"' in html and " style=" not in html.split("<main")[-1].split("</main>")[0]

    def test_htmx_swap_returns_the_grid_with_a_message(self, client, workspace, owner, ig):
        a = make_pp(workspace, ig, when=hours(5), caption="First")
        b = make_pp(workspace, ig, when=hours(9), caption="Second")
        client.force_login(owner)
        r = client.post(swap_url(workspace, ig), {"first": str(a.id), "second": str(b.id)}, HTTP_HX_REQUEST="true")
        assert r.status_code == 200 and b"Swapped times" in r.content and b'id="grid-body"' in r.content
        assert b"<html" not in r.content
        a.refresh_from_db(), b.refresh_from_db()
        assert a.scheduled_at > b.scheduled_at

    def test_plain_post_redirects_and_errors_stay_on_the_page(self, client, workspace, owner, ig):
        a = make_pp(workspace, ig, when=hours(5))
        client.force_login(owner)
        r = client.post(move_url(workspace, ig, a), {"direction": "later"})
        assert r.status_code == 302 and r["Location"] == grid_url(workspace, ig)
        r = client.post(move_url(workspace, ig, a), {"direction": "later"}, HTTP_HX_REQUEST="true")
        assert r.status_code == 200 and b'role="alert"' in r.content

    def test_garbage_ids_are_a_message_not_an_error(self, client, workspace, owner, ig):
        client.force_login(owner)
        r = client.post(swap_url(workspace, ig), {"first": "nope", "second": ""}, HTTP_HX_REQUEST="true")
        assert r.status_code == 200 and b"Pick two posts" in r.content

    def test_get_is_not_allowed_on_mutations(self, client, workspace, owner, ig):
        client.force_login(owner)
        assert client.get(swap_url(workspace, ig)).status_code == 405

    def test_index_redirects_when_there_is_one_account(self, client, workspace, owner, ig):
        client.force_login(owner)
        r = client.get(reverse("calendar:grid_index", kwargs={"workspace_id": workspace.id}))
        assert r.status_code == 302 and r["Location"] == grid_url(workspace, ig)
        make_account(workspace, "instagram", "Second")
        r = client.get(reverse("calendar:grid_index", kwargs={"workspace_id": workspace.id}))
        assert r.status_code == 200 and b"Acme IG" in r.content and b"Second" in r.content

    def test_reachable_from_the_calendar_and_the_account_card(self, client, workspace, owner, ig):
        client.force_login(owner)
        calendar = client.get(reverse("calendar:calendar", kwargs={"workspace_id": workspace.id})).content.decode()
        assert "Grid preview" in calendar
        accounts = client.get(reverse("social_accounts:list", kwargs={"workspace_id": workspace.id})).content.decode()
        assert grid_url(workspace, ig) in accounts

    def test_no_entry_points_without_a_grid_platform(self, client, workspace, owner):
        make_account(workspace, "linkedin_company", "LI")
        client.force_login(owner)
        calendar = client.get(reverse("calendar:calendar", kwargs={"workspace_id": workspace.id})).content.decode()
        assert "Grid preview" not in calendar

    def test_render_cost_does_not_grow_with_tiles(
        self, client, workspace, owner, ig, picture, django_assert_max_num_queries
    ):
        for h in range(1, 13):
            make_pp(workspace, ig, when=hours(h), picture=picture, author=owner)
        client.force_login(owner)
        with django_assert_max_num_queries(40):
            assert client.get(grid_url(workspace, ig)).status_code == 200
