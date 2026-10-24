# ruff: noqa: F811 - the shared `world` fixture is imported, then requested by name.
"""The calendar toolbar, grid chips and channel rail: markup contracts and the query budget."""

from __future__ import annotations

from datetime import timedelta

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.account_sets.models import AccountSet
from apps.calendar.test_timing_views import url, world  # noqa: F401 - world is a fixture
from apps.composer.models import PlatformPost, Post
from apps.social_accounts.models import SocialAccount


def make_pp(world, account=None, status="scheduled", when=None, tags=None, error="", caption="Hello"):
    post = Post.objects.create(
        workspace=world.ws, caption=caption, author=world.owner_user, scheduled_at=when, tags=tags or []
    )
    return PlatformPost.objects.create(
        post=post,
        social_account=account or world.account,
        status=status,
        scheduled_at=when,
        publish_error=error,
    )


def page(world, client=None, **params):
    params = {"mode": "calendar", "view": "month", **params}
    return (client or world.owner).get(url("calendar", world), params)


def tomorrow():
    return timezone.now() + timedelta(days=1)


class TestToolbar:
    def test_view_switcher_is_a_labelled_group_of_links_with_the_current_view_marked(self, world):
        html = page(world, view="week").content.decode()
        assert 'aria-label="Calendar view"' in html and 'class="pd-seg"' in html
        for view in ("day", "week", "month", "list"):
            assert f"view={view}&amp;date=" in html
        nav = html[html.index('class="pd-seg"') :]
        nav = nav[: nav.index("</nav>")]
        assert nav.count('aria-current="page"') == 1 and nav.index('aria-current="page"') < nav.index(">Week</a>")
        assert ">Week</a>" in html and ">List</a>" in html

    def test_previous_next_and_today_stay_plain_links(self, world):
        html = page(world, view="month", date="2031-05-04").content.decode()
        assert 'aria-label="Previous month"' in html and 'aria-label="Next month"' in html
        assert "date=2031-04-01" in html and "date=2031-06-01" in html
        assert ">Today</a>" in html

    def test_list_view_has_no_period_arrows(self, world):
        r = page(world, view="list")
        html = r.content.decode()
        assert r.status_code == 200
        assert 'aria-label="Previous' not in html and 'aria-label="Calendar view"' in html


class TestChips:
    def test_chip_is_a_two_part_card_with_the_hooks_drag_and_drop_needs(self, world):
        when = tomorrow()
        pp = make_pp(world, when=when, tags=["Launch"])
        html = page(world, view="week", date=when.date().isoformat()).content.decode()
        assert f'data-platform-post-id="{pp.id}"' in html
        assert 'class="post-chip cal-chip status-scheduled"' in html
        assert 'class="cal-chip-strip"' in html and ">Launch<" in html
        assert 'class="cal-chip-actions" role="group"' in html
        assert 'class="chip-resched cal-act"' in html and 'aria-label="Reschedule:' in html
        assert 'datetime="' in html

    def test_failed_chip_carries_a_visible_badge_and_the_error_for_screen_readers(self, world):
        when = timezone.now() - timedelta(hours=30)
        make_pp(world, status="failed", when=when, error="Rate limit exceeded")
        html = page(world, view="week", date=when.date().isoformat()).content.decode()
        assert "is-failed" in html and 'class="cal-chip-alert"' in html
        assert 'title="Rate limit exceeded"' in html
        assert '<span class="sr-only"> Rate limit exceeded</span>' in html

    def test_actions_follow_what_the_post_and_the_person_may_do(self, world):
        when = timezone.now() - timedelta(hours=30)
        published = make_pp(world, status="published", when=when)
        PlatformPost.objects.filter(pk=published.pk).update(permalink="https://example.com/p/1")
        html = page(world, view="week", date=when.date().isoformat()).content.decode()
        assert "View live post" in html and 'class="cal-act cal-act--danger"' not in html
        assert 'class="chip-resched' not in html
        assert "/clone/" in html  # the owner may duplicate
        viewer_html = page(world, world.viewer, view="week", date=when.date().isoformat()).content.decode()
        assert "/clone/" not in viewer_html

    def test_unsafe_permalink_is_not_linked(self, world):
        when = timezone.now() - timedelta(hours=30)
        pp = make_pp(world, status="published", when=when)
        PlatformPost.objects.filter(pk=pp.pk).update(permalink="javascript:alert(1)")
        html = page(world, view="week", date=when.date().isoformat()).content.decode()
        assert "javascript:alert" not in html

    def test_month_cell_shows_three_chips_then_a_more_button_that_holds_the_rest(self, world):
        when = tomorrow()
        for _ in range(5):
            make_pp(world, when=when)
        html = page(world, view="month", date=when.date().isoformat()).content.decode()
        assert "+2 more" in html and "Show less" in html
        # Two overflow chips and the "Show less" label are hidden until expanded.
        assert html.count('x-show="more" x-cloak') == 3
        assert 'aria-expanded="false"' in html

    def test_past_cells_are_marked_and_future_ones_get_the_add_tile(self, world):
        html = page(world, view="month").content.decode()
        assert "cal-past" in html and 'class="cal-add-btn cal-add-tile"' in html
        week = page(world, view="week").content.decode()
        assert 'class="cal-slot-cell cal-add-cell cal-drop' in week


class TestChannelRail:
    def test_rail_lists_every_channel_and_marks_the_ones_needing_a_reconnect(self, world):
        broken = SocialAccount.objects.create(
            workspace=world.ws,
            platform="mastodon",
            account_platform_id="m",
            account_name="Moss",
            connection_status="error",
        )
        html = page(world).content.decode()
        assert 'class="cal-rail"' in html and 'aria-label="Channels"' in html
        assert "Sky" in html and "Moss" in html
        assert f'data-account-id="{broken.id}"' in html and "Needs reconnecting" in html
        assert f"/{broken.id}/reconnect/" in html and 'aria-label="Reconnect Moss"' in html
        assert "channelRailCollapsed" in html and "Connect channel" in html and "Create post" in html

    def test_rows_are_toggle_buttons_for_the_channel_filter(self, world):
        html = page(world, channel=str(world.account.id)).content.decode()
        assert f"toggleChannel('{world.account.id}')" in html
        assert 'aria-pressed="true"' in html  # the filtered channel starts pressed

    def test_viewer_gets_neither_reconnect_nor_connect_nor_create(self, world):
        SocialAccount.objects.create(
            workspace=world.ws,
            platform="mastodon",
            account_platform_id="m",
            account_name="Moss",
            connection_status="error",
        )
        html = page(world, world.viewer).content.decode()
        rail = html[html.index('<aside class="cal-rail"') :]
        rail = rail[: rail.index("</aside>")]
        assert "Moss" in rail
        assert 'aria-label="Reconnect' not in rail
        assert 'title="Connect channel"' not in rail and "cal-rail-btn--primary" not in rail

    def test_channels_group_under_their_account_set(self, world):
        other = SocialAccount.objects.create(
            workspace=world.ws,
            platform="mastodon",
            account_platform_id="m2",
            account_name="Moss",
            connection_status="connected",
        )
        client_set = AccountSet.objects.create(workspace=world.ws, name="Client A")
        client_set.accounts.add(world.account)
        html = page(world).content.decode()
        rail = html[html.index('class="cal-rail"') :]
        assert "Client A" in rail and "Other channels" in rail
        assert rail.index("Client A") < rail.index("Sky") < rail.index("Other channels") < rail.index("Moss")
        assert other.account_name in html

    def test_no_channels_shows_the_shared_empty_state(self, world):
        SocialAccount.objects.filter(workspace=world.ws).delete()
        html = world.owner.get(url("calendar", world), {"mode": "calendar", "view": "month"}).content.decode()
        assert 'data-empty-state="calendar-rail"' in html and "No channels yet" in html


class TestQueryBudget:
    """Rendering must not issue queries per post, per channel or per account set."""

    def count(self, world, **params):
        page(world, **params)  # warm-up: first-visit work (onboarding, caches) is not what is measured
        with CaptureQueriesContext(connection) as ctx:
            r = page(world, **params)
        assert r.status_code == 200
        # Savepoints come from per-request atomic blocks, not from the page's own data access.
        return len([q for q in ctx if not q["sql"].startswith(("SAVEPOINT", "RELEASE SAVEPOINT"))])

    def test_month_queries_do_not_grow_with_posts_channels_or_sets(self, world):
        when = tomorrow()
        make_pp(world, when=when, tags=["a"])
        AccountSet.objects.create(workspace=world.ws, name="First set").accounts.add(world.account)
        baseline = self.count(world, view="month", date=when.date().isoformat())
        for i in range(6):
            account = SocialAccount.objects.create(
                workspace=world.ws,
                platform="mastodon",
                account_platform_id=f"x{i}",
                account_name=f"Chan {i}",
                connection_status="connected" if i % 2 else "error",
            )
            for _ in range(3):
                make_pp(world, account=account, when=when + timedelta(hours=i), tags=["t"])
            AccountSet.objects.create(workspace=world.ws, name=f"Set {i}").accounts.add(account)
        assert self.count(world, view="month", date=when.date().isoformat()) == baseline

    def test_week_and_day_queries_do_not_grow_with_posts(self, world):
        when = tomorrow()
        make_pp(world, when=when)
        base_week = self.count(world, view="week", date=when.date().isoformat())
        base_day = self.count(world, view="day", date=when.date().isoformat())
        for i in range(8):
            make_pp(world, when=when + timedelta(hours=i), status="failed" if i % 2 else "scheduled", error="boom")
        assert self.count(world, view="week", date=when.date().isoformat()) == base_week
        assert self.count(world, view="day", date=when.date().isoformat()) == base_day
