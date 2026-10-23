"""Calendar chips and Publish rows mark a thread with its part count."""

import zoneinfo
from datetime import timedelta

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.composer.models import PlatformPost, Post, PostPart
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

NEW_YORK = zoneinfo.ZoneInfo("America/New_York")


class CalendarThreadBadgeTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Thread Org")
        self.ws = Workspace.objects.create(organization=self.org, name="Thread WS", timezone="America/New_York")
        self.account = SocialAccount.objects.create(
            workspace=self.ws,
            platform="bluesky",
            account_platform_id="bsky-cal",
            account_name="Bluesky",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        self.user = User.objects.create_user(email="cal@example.com", password="pw", tos_accepted_at=timezone.now())
        OrgMembership.objects.create(user=self.user, organization=self.org, org_role=OrgMembership.OrgRole.OWNER)
        WorkspaceMembership.objects.create(
            user=self.user, workspace=self.ws, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
        )
        self.client.force_login(self.user)
        today = timezone.localtime(timezone.now(), NEW_YORK)
        first_next = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
        self.start = first_next.replace(hour=9, minute=0, second=0, microsecond=0)
        self.url = reverse("calendar:calendar", kwargs={"workspace_id": self.ws.id})

    def _post(self, parts, *, day=0, status="scheduled"):
        when = self.start + timedelta(days=day)
        post = Post.objects.create(workspace=self.ws, caption="thread", scheduled_at=when)
        PlatformPost.objects.create(post=post, social_account=self.account, status=status, scheduled_at=when)
        for position in range(1, parts):
            PostPart.objects.create(post=post, position=position, caption=f"part {position + 1}")
        return post

    def _month(self):
        return self.client.get(
            self.url, {"view": "month", "date": self.start.date().isoformat()}, HTTP_HX_REQUEST="true"
        )

    def test_a_thread_chip_shows_its_part_count(self):
        self._post(3)
        html = self._month().content.decode()
        # Tooltip and screen-reader text.
        self.assertEqual(html.count("Thread of 3 parts"), 2)

    def test_a_single_post_has_no_badge(self):
        self._post(1)
        self.assertNotIn("Thread of", self._month().content.decode())

    def test_badges_do_not_query_per_post(self):
        self._post(2, day=0)
        self._month()
        with CaptureQueriesContext(connection) as few:
            self._month()
        for day in range(1, 6):
            self._post(3, day=day)
        with CaptureQueriesContext(connection) as many:
            self._month()
        self.assertEqual(len(few.captured_queries), len(many.captured_queries))

    def test_publish_queue_row_marks_the_thread(self):
        self._post(4)
        response = self.client.get(
            reverse("calendar:calendar", kwargs={"workspace_id": self.ws.id}),
            {"tab": "queue"},
            HTTP_HX_REQUEST="true",
        )
        self.assertIn("Thread of 4 parts", response.content.decode())
