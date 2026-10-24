"""Saved views on the calendar / Publish page, and the team-note count on chips and rows."""

import zoneinfo
from datetime import timedelta

from django.db import connection
from django.http import QueryDict
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.approvals.models import PostNote
from apps.calendar import saved_views
from apps.calendar.models import SavedView, SavedViewDefault
from apps.composer.models import PlatformPost, Post
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

NEW_YORK = zoneinfo.ZoneInfo("America/New_York")


class CalendarBase(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Org")
        self.ws = Workspace.objects.create(organization=self.org, name="WS", timezone="America/New_York")
        self.account = SocialAccount.objects.create(
            workspace=self.ws,
            platform="bluesky",
            account_platform_id="bsky-1",
            account_name="Bluesky",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        self.owner = self.member("owner@example.com", "owner")
        self.editor = self.member("editor@example.com", "editor")
        self.client_user = self.member("client@example.com", "client")
        self.url = reverse("calendar:calendar", kwargs={"workspace_id": self.ws.id})
        today = timezone.localtime(timezone.now(), NEW_YORK)
        first_next = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
        self.start = first_next.replace(hour=9, minute=0, second=0, microsecond=0)

    def member(self, email, role):
        user = User.objects.create_user(email=email, password="pw", tos_accepted_at=timezone.now())
        OrgMembership.objects.get_or_create(user=user, organization=self.org, defaults={"org_role": "member"})
        WorkspaceMembership.objects.update_or_create(
            user=user, workspace=self.ws, defaults={"workspace_role": role, "custom_role": None}
        )
        return user

    def post(self, *, day=0, status="scheduled", notes=0, author=None):
        when = self.start + timedelta(days=day)
        post = Post.objects.create(workspace=self.ws, caption=f"post {day}", scheduled_at=when, author=author)
        PlatformPost.objects.create(post=post, social_account=self.account, status=status, scheduled_at=when)
        for n in range(notes):
            PostNote.objects.create(post=post, author=author, body=f"note {n}")
        return post

    def month(self):
        return self.client.get(
            self.url, {"view": "month", "date": self.start.date().isoformat()}, HTTP_HX_REQUEST="true"
        )


class NoteBadgeTests(CalendarBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)

    def test_a_chip_shows_how_many_notes_the_post_has(self):
        self.post(notes=3)
        self.assertIn("3 team notes", self.month().content.decode())

    def test_a_post_without_notes_has_no_badge(self):
        self.post()
        self.assertNotIn("team note", self.month().content.decode())

    def test_deleted_notes_do_not_count(self):
        post = self.post(notes=2)
        PostNote.objects.filter(post=post).first().delete()
        PostNote.objects.filter(post=post).update(deleted_at=timezone.now())
        self.assertNotIn("team note", self.month().content.decode())

    def test_one_note_reads_singular(self):
        self.post(notes=1)
        self.assertIn("1 team note", self.month().content.decode())
        self.assertNotIn("1 team notes", self.month().content.decode())

    def test_the_count_does_not_add_queries_per_chip(self):
        self.post(day=0, notes=1)
        self.month()  # warm caches
        with CaptureQueriesContext(connection) as few:
            self.month()
        for day in range(1, 6):
            self.post(day=day, notes=day)
        with CaptureQueriesContext(connection) as many:
            self.month()
        self.assertEqual(len(few.captured_queries), len(many.captured_queries))
        self.assertIn("5 team notes", self.month().content.decode())

    def test_publish_tabs_show_and_batch_the_count(self):
        for day in range(4):
            self.post(day=day, notes=day + 1, status="scheduled")
        for day in range(4, 8):
            self.post(day=day, notes=1, status="draft")
        queue = reverse("calendar:publish_tab_queue", kwargs={"workspace_id": self.ws.id})
        drafts = reverse("calendar:publish_tab_drafts", kwargs={"workspace_id": self.ws.id})
        self.assertContains(self.client.get(queue, HTTP_HX_REQUEST="true"), "4 team notes")
        self.assertContains(self.client.get(drafts, HTTP_HX_REQUEST="true"), "1 team note")

        def queries(url):
            with CaptureQueriesContext(connection) as ctx:
                self.client.get(url, HTTP_HX_REQUEST="true")
            return len(ctx.captured_queries)

        before = queries(queue)
        # More notes on the same rows: the count rides in the list query, so nothing more is asked.
        for post in Post.objects.filter(workspace=self.ws):
            for n in range(3):
                PostNote.objects.create(post=post, body=f"later {n}")
        self.assertEqual(queries(queue), before)
        self.assertContains(self.client.get(queue, HTTP_HX_REQUEST="true"), "7 team notes")

    def test_clients_see_no_count(self):
        self.post(notes=3)
        WorkspaceMembership.objects.filter(user=self.client_user).update(workspace_role="client")
        self.client.force_login(self.client_user)
        html = self.month().content.decode()
        self.assertNotIn("team note", html)

    def test_viewers_see_no_count(self):
        self.post(notes=2)
        viewer = self.member("viewer@example.com", "viewer")
        self.client.force_login(viewer)
        self.assertNotIn("team note", self.month().content.decode())


class SavedViewServiceTests(CalendarBase):
    def test_clean_filters_keeps_only_the_whitelist(self):
        good = "8a2d3c1e-0000-4000-8000-000000000001"
        params = QueryDict(
            f"mode=calendar&view=week&channel={good}&channel=nope&status=draft&status=scheduled&tag=launch"
            "&evil=1&date=2026-01-01&tz=UTC&view=month"
        )
        cleaned = saved_views.clean_filters(params)
        self.assertEqual(
            cleaned,
            {
                "mode": ["calendar"],
                "view": ["month"],
                "channel": [good],
                "status": ["draft", "scheduled"],
                "tag": ["launch"],
            },
        )

    def test_clean_filters_rejects_unknown_modes_and_views(self):
        self.assertEqual(saved_views.clean_filters(QueryDict("mode=admin&view=year&tab=x&holidays=2")), {})

    def test_view_url_round_trips_through_the_page_parameters(self):
        saved = SavedView.objects.create(
            workspace=self.ws, owner=self.owner, name="V", filters={"mode": ["calendar"], "status": ["draft", "failed"]}
        )
        url = saved_views.view_url(saved)
        self.assertTrue(url.startswith(self.url + "?"))
        self.assertEqual(
            saved_views.clean_filters(QueryDict(url.split("?", 1)[1])),
            {"mode": ["calendar"], "status": ["draft", "failed"]},
        )


class SavedViewHttpTests(CalendarBase):
    def endpoint(self, name, **kwargs):
        return reverse(f"calendar:{name}", kwargs={"workspace_id": self.ws.id, **kwargs})

    def save(self, user, name="Drafts", shared=False, query="mode=list&tab=drafts&tag=launch"):
        self.client.force_login(user)
        data = {"name": name, "query": query}
        if shared:
            data["shared"] = "on"
        return self.client.post(self.endpoint("saved_view_create"), data, HTTP_HX_REQUEST="true")

    def test_saving_keeps_the_filters_and_lists_the_view(self):
        response = self.save(self.editor)
        self.assertContains(response, "Drafts")
        saved = SavedView.objects.get()
        self.assertEqual(saved.filters, {"mode": ["list"], "tab": ["drafts"], "tag": ["launch"]})
        self.assertFalse(saved.is_shared)
        self.assertContains(response, "mode=list")

    def test_a_personal_view_is_private_and_a_shared_one_is_not(self):
        self.save(self.editor, "Mine")
        self.save(self.editor, "Everyone's", shared=True)
        self.client.force_login(self.owner)
        html = self.client.get(self.endpoint("saved_views_menu")).content.decode()
        self.assertIn("Everyone&#x27;s", html)
        self.assertNotIn("Mine", html)
        self.assertIn("Shared by", html)

    def test_clients_cannot_share_views(self):
        response = self.save(self.client_user, "Client view", shared=True)
        self.assertIn("showToast", response.headers["HX-Trigger"])
        self.assertFalse(SavedView.objects.exists())

    def test_a_duplicate_name_and_a_blank_name_are_refused(self):
        self.save(self.editor, "Same")
        self.assertIn("already have", self.save(self.editor, "Same").headers["HX-Trigger"])
        self.assertIn("name", self.save(self.editor, "  ").headers["HX-Trigger"])
        self.assertEqual(SavedView.objects.count(), 1)

    def test_nothing_to_save_is_refused(self):
        response = self.save(self.editor, "Empty", query="evil=1")
        self.assertIn("no filters", response.headers["HX-Trigger"])

    def test_delete_is_for_the_owner_or_an_editor_of_shared_views(self):
        self.save(self.editor, "Personal")
        self.save(self.editor, "Shared", shared=True)
        personal = SavedView.objects.get(name="Personal")
        shared = SavedView.objects.get(name="Shared")
        # Someone else cannot even see a personal view.
        self.client.force_login(self.owner)
        self.assertEqual(self.client.post(self.endpoint("saved_view_delete", view_id=personal.id)).status_code, 404)
        # The workspace owner can remove a shared one.
        self.assertEqual(self.client.post(self.endpoint("saved_view_delete", view_id=shared.id)).status_code, 200)
        self.assertFalse(SavedView.objects.filter(name="Shared").exists())
        # A client viewing a shared view cannot delete it.
        shared_again = SavedView.objects.create(
            workspace=self.ws, owner=self.editor, name="Again", filters={"mode": ["list"]}, is_shared=True
        )
        self.client.force_login(self.client_user)
        response = self.client.post(self.endpoint("saved_view_delete", view_id=shared_again.id))
        self.assertIn("Only its owner", response.headers["HX-Trigger"])
        self.assertTrue(SavedView.objects.filter(pk=shared_again.pk).exists())

    def test_views_are_scoped_to_the_workspace(self):
        other = Workspace.objects.create(organization=self.org, name="Other")
        WorkspaceMembership.objects.create(user=self.editor, workspace=other, workspace_role="editor")
        theirs = SavedView.objects.create(
            workspace=other, owner=self.editor, name="Other WS", filters={"mode": ["list"]}, is_shared=True
        )
        self.client.force_login(self.editor)
        self.assertEqual(self.client.post(self.endpoint("saved_view_delete", view_id=theirs.id)).status_code, 404)
        self.assertNotContains(self.client.get(self.endpoint("saved_views_menu")), "Other WS")

    def test_setting_and_clearing_a_default(self):
        self.save(self.editor, "Mine")
        saved = SavedView.objects.get()
        response = self.client.post(self.endpoint("saved_view_default"), {"view_id": str(saved.id)})
        self.assertContains(response, "Default")
        self.assertEqual(SavedViewDefault.objects.get().view, saved)
        other = SavedView.objects.create(workspace=self.ws, owner=self.editor, name="Other", filters={"mode": ["list"]})
        self.client.post(self.endpoint("saved_view_default"), {"view_id": str(other.id)})
        self.assertEqual(SavedViewDefault.objects.get().view, other)  # one per person
        self.client.post(self.endpoint("saved_view_default"), {})
        self.assertFalse(SavedViewDefault.objects.exists())

    def test_the_default_opens_when_the_page_is_opened_bare(self):
        saved = SavedView.objects.create(
            workspace=self.ws, owner=self.editor, name="Mine", filters={"mode": ["list"], "tab": ["drafts"]}
        )
        saved_views.set_default(self.ws, self.editor, saved)
        self.client.force_login(self.editor)
        response = self.client.get(self.url)
        self.assertRedirects(response, saved_views.view_url(saved), fetch_redirect_response=False)
        # Any explicit filter, and htmx refreshes, are left alone.
        self.assertEqual(self.client.get(self.url, {"mode": "calendar"}).status_code, 200)
        self.assertEqual(self.client.get(self.url, HTTP_HX_REQUEST="true").status_code, 200)

    def test_nobody_else_is_sent_to_someone_elses_default(self):
        saved = SavedView.objects.create(workspace=self.ws, owner=self.editor, name="Mine", filters={"mode": ["list"]})
        saved_views.set_default(self.ws, self.editor, saved)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_a_default_that_stopped_being_shared_no_longer_applies(self):
        shared = SavedView.objects.create(
            workspace=self.ws, owner=self.owner, name="Team", filters={"mode": ["list"]}, is_shared=True
        )
        saved_views.set_default(self.ws, self.editor, shared)
        SavedView.objects.filter(pk=shared.pk).update(is_shared=False)
        self.client.force_login(self.editor)
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_opening_a_saved_view_applies_its_filters_and_the_toolbar_reflects_them(self):
        self.post(day=0, status="draft")
        self.post(day=1, status="scheduled")
        self.client.force_login(self.editor)
        response = self.client.get(
            self.url,
            {"mode": "calendar", "view": "month", "status": "draft", "channel": str(self.account.id),
             "date": self.start.date().isoformat()},
        )  # fmt: skip
        self.assertContains(response, "post 0")
        self.assertNotContains(response, "post 1")
        # The channel picker starts on the saved channel rather than "all".
        self.assertContains(response, f'channels: ["{self.account.id}"]')

    def test_the_toolbar_has_the_views_control_in_both_modes(self):
        self.client.force_login(self.editor)
        self.assertContains(self.client.get(self.url, {"mode": "calendar"}), "Save current filters")
        self.assertContains(self.client.get(self.url, {"mode": "list"}), "Save current filters")

    def test_the_list_picker_starts_on_the_saved_tag_and_channel(self):
        self.client.force_login(self.editor)
        response = self.client.get(
            self.url, {"mode": "list", "tab": "drafts", "tag": "launch", "channel": str(self.account.id)}
        )
        self.assertContains(response, "activeTag: 'launch'")
        self.assertContains(response, f'selectedChannels: ["{self.account.id}"]')
