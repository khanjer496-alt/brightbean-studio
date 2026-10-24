# ruff: noqa: F811 - the shared `world` fixture is imported, then requested by name.
"""Keyboard and phone alternatives to dragging: the Reschedule dialog, queue Move up / down, the week agenda."""

from __future__ import annotations

from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from apps.calendar.models import Queue, QueueEntry
from apps.calendar.test_timing_views import join, make_user, url, world  # noqa: F401 - world is a fixture
from apps.composer.models import PlatformPost, Post


def make_pp(world, status="draft", scheduled_at=None, author=None):
    post = Post.objects.create(workspace=world.ws, caption="Hello", author=author or world.owner_user)
    return PlatformPost.objects.create(
        post=post, social_account=world.account, status=status, scheduled_at=scheduled_at
    )


def dialog_url(world, pp):
    return url("reschedule_dialog", world, platform_post_id=pp.id)


class TestRescheduleDialog:
    def test_author_gets_the_form_that_posts_to_the_reschedule_endpoint(self, world):
        pp = make_pp(world, "scheduled", timezone.now() + timedelta(days=2))
        r = world.owner.get(dialog_url(world, pp))
        html = r.content.decode()
        assert r.status_code == 200
        assert 'type="datetime-local"' in html and 'for="reschedule-datetime"' in html
        assert url("reschedule", world) in html and str(pp.id) in html
        assert "Europe/Paris" in html and 'aria-label="Close"' in html

    def test_draft_says_saving_schedules_it(self, world):
        html = world.owner.get(dialog_url(world, make_pp(world, "draft"))).content.decode()
        assert "schedules this draft post" in html

    def test_approved_post_keeps_its_status(self, world):
        pp = make_pp(world, "approved", timezone.now() + timedelta(days=1))
        assert "keeps its approved status" in world.owner.get(dialog_url(world, pp)).content.decode()

    def test_viewer_is_refused_in_the_dialog(self, world):
        pp = make_pp(world, "scheduled", timezone.now() + timedelta(days=2))
        r = world.viewer.get(dialog_url(world, pp))
        assert r.status_code == 403
        assert "Permission denied" in r.content.decode() and "datetime-local" not in r.content.decode()

    def test_published_post_cannot_be_rescheduled(self, world):
        pp = make_pp(world, "published", timezone.now() - timedelta(days=1))
        r = world.owner.get(dialog_url(world, pp))
        assert r.status_code == 400 and "cannot be rescheduled" in r.content.decode()

    def test_another_workspaces_post_is_a_404(self, world):
        foreign_post = Post.objects.create(workspace=world.other_ws, caption="x")
        foreign = PlatformPost.objects.create(post=foreign_post, social_account=world.foreign, status="draft")
        assert world.owner.get(dialog_url(world, foreign)).status_code == 404

    def test_anonymous_is_redirected_to_login(self, world):
        from django.test import Client

        pp = make_pp(world, "draft")
        assert Client().get(dialog_url(world, pp)).status_code == 302

    def test_submitting_the_form_moves_the_post_via_the_existing_endpoint(self, world):
        pp = make_pp(world, "scheduled", timezone.now() + timedelta(days=2))
        r = world.owner.post(url("reschedule", world), {"platform_post_id": pp.id, "new_datetime": "2031-05-04T10:30"})
        pp.refresh_from_db()
        assert r.status_code == 204
        assert pp.scheduled_at.astimezone(timezone.get_fixed_timezone(120)).strftime("%H:%M") == "10:30"


class TestChipControls:
    def test_week_view_has_reschedule_button_agenda_and_labelled_controls(self, world):
        when = timezone.now() + timedelta(days=1)
        pp = make_pp(world, "scheduled", when)
        r = world.owner.get(
            url("calendar", world), {"mode": "calendar", "view": "week", "date": when.date().isoformat()}
        )
        html = r.content.decode()
        assert r.status_code == 200
        assert "md:hidden" in html and 'aria-labelledby="agenda-' in html
        assert (
            html.count(
                reverse("calendar:reschedule_dialog", kwargs={"workspace_id": world.ws.id, "platform_post_id": pp.id})
            )
            >= 2
        )
        assert 'aria-label="Reschedule:' in html and 'id="reschedule-dialog"' in html
        assert 'role="checkbox"' in html

    def test_month_view_has_a_compact_phone_layout_with_every_post_in_the_day_agenda(self, world):
        when = timezone.now() + timedelta(days=1)
        for _ in range(5):
            make_pp(world, "scheduled", when)
        r = world.owner.get(
            url("calendar", world), {"mode": "calendar", "view": "month", "date": when.date().isoformat()}
        )
        html = r.content.decode()
        assert 'class="cal-compact-month md:hidden tab-slide-in"' in html and "cal-compact-day" in html
        assert 'aria-pressed="false"' in html and 'aria-labelledby="month-agenda-' in html
        # The desktop grid caps a day at three chips ("+2 more"); the phone agenda lists all five.
        assert "+2 more" in html
        assert html.count('class="post-chip ') >= 3 + 5
        # It opens on a day of the shown month (today when that is in it).
        assert f"selected: '{r.context['month_agenda_default']}'" in html

    def test_published_chip_has_no_reschedule_button(self, world):
        when = timezone.now() - timedelta(hours=30)
        make_pp(world, "published", when)
        html = world.owner.get(
            url("calendar", world), {"mode": "calendar", "view": "week", "date": when.date().isoformat()}
        ).content.decode()
        assert "Published:" in html
        assert 'aria-label="Reschedule:' not in html


class TestQueueMove:
    def make_queue(self, world, count=3):
        queue = Queue.objects.create(workspace=world.ws, name="Q", social_account=world.account)
        base = timezone.now() + timedelta(days=1)
        entries = []
        for i in range(count):
            pp = make_pp(world, "scheduled", base + timedelta(hours=i))
            entries.append(
                QueueEntry.objects.create(queue=queue, post=pp.post, position=i, assigned_slot_datetime=pp.scheduled_at)
            )
        return queue, entries

    def move(self, client, world, queue, entry, direction):
        return client.post(url("queue_entry_move", world, queue_id=queue.id, entry_id=entry.id, direction=direction))

    def order(self, queue):
        return [e.id for e in queue.entries.order_by("assigned_slot_datetime")]

    def test_move_down_swaps_with_the_next_entry(self, world):
        queue, (a, b, c) = self.make_queue(world)
        r = self.move(world.owner, world, queue, a, "down")
        assert r.status_code == 200 and r.json()["position"] == 2
        assert self.order(queue) == [b.id, a.id, c.id]

    def test_move_up_swaps_with_the_previous_entry(self, world):
        queue, (a, b, c) = self.make_queue(world)
        self.move(world.owner, world, queue, c, "up")
        assert self.order(queue) == [a.id, c.id, b.id]

    def test_htmx_response_announces_the_new_place(self, world):
        queue, (a, b, c) = self.make_queue(world)
        r = world.owner.post(
            url("queue_entry_move", world, queue_id=queue.id, entry_id=b.id, direction="down"), HTTP_HX_REQUEST="true"
        )
        assert r.status_code == 204 and '"position": 3' in r["HX-Trigger"] and "queueReordered" in r["HX-Trigger"]

    def test_moving_past_either_end_changes_nothing(self, world):
        queue, (a, b, c) = self.make_queue(world)
        self.move(world.owner, world, queue, a, "up")
        self.move(world.owner, world, queue, c, "down")
        assert self.order(queue) == [a.id, b.id, c.id]

    def test_bad_direction_is_400_and_foreign_queue_404(self, world):
        queue, (a, *_rest) = self.make_queue(world)
        assert self.move(world.owner, world, queue, a, "sideways").status_code == 400
        assert self.move(world.stranger, world, queue, a, "down").status_code in (403, 404)

    def test_get_is_not_allowed(self, world):
        queue, (a, *_rest) = self.make_queue(world)
        assert (
            world.owner.get(
                url("queue_entry_move", world, queue_id=queue.id, entry_id=a.id, direction="up")
            ).status_code
            == 405
        )

    def test_detail_page_renders_move_buttons_with_names(self, world):
        queue, _ = self.make_queue(world)
        html = world.owner.get(url("queue_detail", world, queue_id=queue.id)).content.decode()
        assert (
            html.count('data-move="up"') == 3
            and 'aria-label="Move earlier:' in html
            and 'aria-label="Move later:' in html
        )
        assert html.count("disabled") >= 2
