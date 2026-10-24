import datetime as dt

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.billing.models import Subscription
from apps.members.models import OrgMembership
from apps.support.models import Announcement, AnnouncementDismissal

from .conftest import make_user

PAGE = "organizations:settings"


def post(message="Maintenance on Friday", **kwargs):
    return Announcement.objects.create(message=message, **kwargs)


@pytest.fixture
def logged_in(client, customer):
    client.force_login(customer)
    return client


@pytest.mark.django_db
class TestShowing:
    def test_live_announcement_is_shown(self, logged_in):
        post(link_url="https://status.example.com", link_label="Status page")

        content = logged_in.get(reverse(PAGE)).content

        assert b"Maintenance on Friday" in content
        assert b"https://status.example.com" in content
        assert b"Status page" in content

    def test_warning_and_info_look_different(self, logged_in):
        post("Heads up", severity=Announcement.Severity.WARNING)

        assert b"bg-warn-soft" in logged_in.get(reverse(PAGE)).content

    def test_link_label_defaults(self, logged_in):
        post(link_url="https://example.com/news")

        assert b"Learn more" in logged_in.get(reverse(PAGE)).content

    def test_not_before_it_starts(self, logged_in):
        post(starts_at=timezone.now() + dt.timedelta(hours=1))

        assert b"Maintenance on Friday" not in logged_in.get(reverse(PAGE)).content

    def test_not_after_it_ends(self, logged_in):
        post(starts_at=timezone.now() - dt.timedelta(days=2), ends_at=timezone.now() - dt.timedelta(minutes=1))

        assert b"Maintenance on Friday" not in logged_in.get(reverse(PAGE)).content

    def test_not_when_withdrawn(self, logged_in):
        post(is_active=False)

        assert b"Maintenance on Friday" not in logged_in.get(reverse(PAGE)).content

    def test_not_shown_to_signed_out_visitors(self, client):
        post()

        assert b"Maintenance on Friday" not in client.get(reverse("account_login")).content

    def test_not_shown_while_support_is_viewing_as_the_user(self, staff_client, customer):
        from apps.support.tests.test_impersonation import start

        post()
        start(staff_client, customer)

        assert b"Maintenance on Friday" not in staff_client.get(reverse(PAGE)).content


@pytest.mark.django_db
class TestAudience:
    @pytest.fixture(autouse=True)
    def billing(self, settings):
        settings.BILLING_ENABLED = True

    def subscribe(self, customer, plan):
        org = OrgMembership.objects.get(user=customer).organization
        Subscription.objects.update_or_create(
            organization=org, defaults={"plan": plan, "status": Subscription.Status.ACTIVE}
        )

    def test_plan_targeted_announcement_reaches_that_plan(self, logged_in, customer):
        self.subscribe(customer, "team")
        post(plan_slugs=["team", "pro"])

        assert b"Maintenance on Friday" in logged_in.get(reverse(PAGE)).content

    def test_plan_targeted_announcement_skips_other_plans(self, logged_in, customer):
        self.subscribe(customer, "starter")
        post(plan_slugs=["team", "pro"])

        assert b"Maintenance on Friday" not in logged_in.get(reverse(PAGE)).content

    def test_everyone_announcement_reaches_every_plan(self, logged_in, customer):
        self.subscribe(customer, "free")
        post(plan_slugs=[])

        assert b"Maintenance on Friday" in logged_in.get(reverse(PAGE)).content


@pytest.mark.django_db
class TestDismissing:
    def test_dismissal_is_remembered_server_side(self, logged_in, customer, client):
        announcement = post()

        response = logged_in.post(reverse("support:dismiss_announcement", kwargs={"announcement_id": announcement.id}))

        assert response.status_code == 204
        assert AnnouncementDismissal.objects.filter(announcement=announcement, user=customer).count() == 1
        assert b"Maintenance on Friday" not in logged_in.get(reverse(PAGE)).content
        # A fresh browser for the same user, with no cookies of the first one.
        client.logout()
        client.force_login(customer)
        assert b"Maintenance on Friday" not in client.get(reverse(PAGE)).content

    def test_dismissing_twice_is_harmless(self, logged_in, customer):
        announcement = post()
        url = reverse("support:dismiss_announcement", kwargs={"announcement_id": announcement.id})

        logged_in.post(url)
        logged_in.post(url)

        assert AnnouncementDismissal.objects.count() == 1

    def test_dismissal_is_per_user(self, logged_in, client):
        announcement = post()
        logged_in.post(reverse("support:dismiss_announcement", kwargs={"announcement_id": announcement.id}))
        client.logout()
        client.force_login(make_user("colleague@example.com"))

        assert b"Maintenance on Friday" in client.get(reverse(PAGE)).content

    def test_dismissing_requires_post_and_login(self, client, logged_in):
        announcement = post()
        url = reverse("support:dismiss_announcement", kwargs={"announcement_id": announcement.id})

        assert logged_in.get(url).status_code == 405
        client.logout()
        assert client.post(url).status_code == 302
        assert not AnnouncementDismissal.objects.exists()

    def test_unknown_announcement_is_ignored(self, logged_in):
        url = reverse(
            "support:dismiss_announcement", kwargs={"announcement_id": "00000000-0000-0000-0000-000000000000"}
        )

        assert logged_in.post(url).status_code == 204
        assert not AnnouncementDismissal.objects.exists()


@pytest.mark.django_db
class TestPendingCache:
    def test_no_query_for_announcements_when_none_exist(self, customer, django_assert_num_queries):
        from apps.support.services import announcements_for

        assert announcements_for(customer, None) == []  # primes the cache
        with django_assert_num_queries(0):
            assert announcements_for(customer, None) == []

    def test_saving_and_withdrawing_show_up_immediately(self, logged_in):
        assert b"Maintenance on Friday" not in logged_in.get(reverse(PAGE)).content
        announcement = post()
        assert b"Maintenance on Friday" in logged_in.get(reverse(PAGE)).content
        announcement.delete()
        assert b"Maintenance on Friday" not in logged_in.get(reverse(PAGE)).content
