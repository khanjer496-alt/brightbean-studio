import datetime as dt

import pytest
from django.contrib.sessions.backends.db import SessionStore
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.members.models import OrgMembership
from apps.support.models import ImpersonationSession
from apps.support.services import SESSION_KEY

from .conftest import make_user, record_authentication

REASON = {"reason": "Ticket 4821"}


def start_url(user):
    return reverse("support:impersonate", kwargs={"user_id": user.pk})


def start(client, target, **extra):
    return client.post(start_url(target), REASON, **extra)


@pytest.mark.django_db
class TestStarting:
    def test_superuser_starts_and_is_audited(self, staff_client, superuser, customer):
        response = start(staff_client, customer, REMOTE_ADDR="203.0.113.9")

        assert response.status_code == 302
        record = ImpersonationSession.objects.get()
        assert record.staff_user == superuser
        assert record.target_user == customer
        assert record.target_organization == OrgMembership.objects.get(user=customer).organization
        assert record.reason == "Ticket 4821"
        assert record.ip_address == "203.0.113.9"
        assert record.ended_at is None
        assert record.expires_at - record.started_at == dt.timedelta(minutes=30)

    def test_confirmation_page_asks_for_a_reason(self, staff_client, customer):
        response = staff_client.get(start_url(customer))

        assert response.status_code == 200
        assert b"customer@example.com" in response.content
        assert b'name="reason"' in response.content
        assert not ImpersonationSession.objects.exists()

    def test_reason_is_required(self, staff_client, customer):
        response = staff_client.post(start_url(customer), {"reason": ""})

        assert response.status_code == 200
        assert not ImpersonationSession.objects.exists()

    def test_anonymous_is_sent_to_login(self, client, customer):
        assert start(client, customer).status_code == 302
        assert not ImpersonationSession.objects.exists()

    def test_ordinary_user_cannot_start(self, client, customer):
        other = make_user("other@example.com")
        client.force_login(other)
        record_authentication(client)

        assert start(client, customer).status_code == 403
        assert client.get(start_url(customer)).status_code == 403
        assert not ImpersonationSession.objects.exists()

    def test_staff_without_superuser_cannot_start(self, client, customer):
        client.force_login(make_user("helper@example.com", is_staff=True))
        record_authentication(client)

        assert start(client, customer).status_code == 403
        assert not ImpersonationSession.objects.exists()

    @pytest.mark.parametrize("flags", [{"is_staff": True}, {"is_staff": True, "is_superuser": True}])
    def test_cannot_view_as_staff(self, staff_client, flags):
        target = make_user("colleague@example.com", **flags)

        assert start(staff_client, target).status_code == 403
        assert not ImpersonationSession.objects.exists()

    def test_cannot_view_as_yourself(self, staff_client, superuser):
        assert start(staff_client, superuser).status_code == 403

    def test_cannot_view_as_deactivated_user(self, staff_client, customer):
        User.objects.filter(pk=customer.pk).update(is_active=False)

        assert start(staff_client, customer).status_code == 403

    def test_stale_sign_in_is_sent_to_reauthenticate(self, client, superuser, customer):
        client.force_login(superuser)
        record_authentication(client, seconds_ago=11 * 60)

        response = start(client, customer)

        assert response.status_code == 302
        assert reverse("account_reauthenticate") in response["Location"]
        assert not ImpersonationSession.objects.exists()

    def test_no_sign_in_record_is_sent_to_reauthenticate(self, client, superuser, customer):
        client.force_login(superuser)

        assert start(client, customer).status_code == 302
        assert not ImpersonationSession.objects.exists()

    def test_recent_sign_in_is_enough(self, client, superuser, customer):
        client.force_login(superuser)
        record_authentication(client, seconds_ago=9 * 60, reauthenticated=False)

        assert start(client, customer).status_code == 302
        assert ImpersonationSession.objects.exists()

    def test_cannot_start_while_already_viewing_as_someone(self, staff_client, customer):
        start(staff_client, customer)
        other = make_user("second@example.com")

        assert start(staff_client, other).status_code == 403
        assert ImpersonationSession.objects.count() == 1


@pytest.mark.django_db
class TestWhileViewing:
    @pytest.fixture(autouse=True)
    def viewing(self, staff_client, customer):
        start(staff_client, customer)
        self.client = staff_client
        self.customer = customer

    def test_pages_render_as_the_customer(self):
        response = self.client.get(reverse("organizations:settings"))

        assert response.status_code == 200
        assert response.wsgi_request.user == self.customer
        assert response.wsgi_request.impersonator.email == "support@example.com"

    def test_banner_on_every_page_with_stop(self):
        for url in (reverse("organizations:settings"), reverse("accounts:settings"), reverse("members:list")):
            response = self.client.get(url)
            assert response.status_code == 200
            assert b"You are viewing as customer@example.com" in response.content
            assert reverse("support:impersonation_stop").encode() in response.content

    def test_no_banner_for_ordinary_sessions(self, client):
        client.force_login(self.customer)

        assert b"You are viewing as" not in client.get(reverse("organizations:settings")).content

    @pytest.mark.parametrize(
        "url_name",
        [
            "api_keys:issue",
            "members:invite",
            "organizations:settings",
            "accounts:settings",
            "accounts:delete_account",
            "billing:checkout",
            "billing:portal",
            "mfa_index",
            "account_change_password",
        ],
    )
    def test_writes_are_blocked(self, url_name):
        assert self.client.post(reverse(url_name), {"name": "x"}).status_code == 403

    def test_writes_are_blocked_whatever_the_method(self):
        url = reverse("organizations:settings")

        for method in ("put", "patch", "delete"):
            assert getattr(self.client, method)(url).status_code == 403

    def test_unknown_write_urls_are_blocked_too(self):
        assert self.client.post("/no/such/route/").status_code == 403

    @pytest.mark.parametrize(
        "path",
        [
            "/social-accounts/11111111-1111-1111-1111-111111111111/connect/",
            "/social-accounts/callback/anything/",
            "/organizations/billing/checkout/",
            "/organizations/billing/portal/",
            "/oauth/authorize/",
            "/accounts/2fa/",
            "/accounts/password/change/",
            "/members/invite/",
        ],
    )
    def test_outward_reads_are_blocked(self, path):
        assert self.client.get(path).status_code == 403

    def test_stop_ends_the_session_and_closes_the_audit_row(self):
        response = self.client.post(reverse("support:impersonation_stop"))

        assert response.status_code == 302
        record = ImpersonationSession.objects.get()
        assert record.ended_at is not None
        assert record.end_reason == ImpersonationSession.EndReason.STOPPED
        page = self.client.get(reverse("organizations:settings"))
        assert page.wsgi_request.user.email == "support@example.com"
        assert b"You are viewing as" not in page.content

    def test_logout_ends_it(self):
        assert self.client.post(reverse("accounts:logout")).status_code == 302

        record = ImpersonationSession.objects.get()
        assert record.end_reason == ImpersonationSession.EndReason.LOGOUT
        assert record.ended_at is not None
        assert self.client.get(reverse("organizations:settings")).status_code == 302  # signed out

    def test_expires_after_thirty_minutes(self):
        ImpersonationSession.objects.update(expires_at=timezone.now() - dt.timedelta(seconds=1))

        response = self.client.get(reverse("accounts:settings"))

        record = ImpersonationSession.objects.get()
        assert record.end_reason == ImpersonationSession.EndReason.EXPIRED
        assert b"You are viewing as" not in response.content
        assert response.wsgi_request.user.email == "support@example.com"

    def test_ends_if_staff_lose_superuser(self, superuser):
        User.objects.filter(pk=superuser.pk).update(is_superuser=False)

        self.client.get(reverse("accounts:settings"))

        assert ImpersonationSession.objects.get().end_reason == ImpersonationSession.EndReason.INVALID

    def test_ends_if_target_is_promoted_to_staff(self):
        User.objects.filter(pk=self.customer.pk).update(is_staff=True)

        self.client.get(reverse("accounts:settings"))

        assert ImpersonationSession.objects.get().end_reason == ImpersonationSession.EndReason.INVALID

    def test_viewing_does_not_touch_the_customers_last_workspace(self):
        from apps.members.models import WorkspaceMembership

        ws = WorkspaceMembership.objects.filter(user=self.customer).first().workspace
        User.objects.filter(pk=self.customer.pk).update(last_workspace_id=None)

        self.client.get(reverse("calendar:calendar", kwargs={"workspace_id": ws.id}))

        self.customer.refresh_from_db()
        assert self.customer.last_workspace_id is None

    def test_a_forged_session_marker_does_nothing(self, client, customer):
        client.force_login(customer)
        session = client.session
        session[SESSION_KEY] = {"record_id": "00000000-0000-0000-0000-000000000000", "target_id": str(customer.pk)}
        session.save()

        response = client.get(reverse("organizations:settings"))

        assert response.status_code == 200
        assert response.wsgi_request.impersonator is None
        assert SESSION_KEY not in client.session

    def test_marker_from_another_staff_members_record_is_rejected(self, client, customer):
        other_staff = make_user("other-staff@example.com", is_staff=True, is_superuser=True)
        record = ImpersonationSession.objects.get()
        client.force_login(other_staff)
        session = client.session
        session[SESSION_KEY] = {"record_id": str(record.id), "target_id": str(customer.pk)}
        session.save()

        response = client.get(reverse("accounts:settings"))

        assert response.wsgi_request.impersonator is None
        assert response.wsgi_request.user == other_staff


@pytest.mark.django_db
def test_org_owner_sees_when_support_looked(staff_client, customer, client):
    start(staff_client, customer)
    client.force_login(customer)

    response = client.get(reverse("organizations:settings"))

    assert b"support viewed your account on" in response.content
    assert timezone.now().strftime("%Y").encode() in response.content


@pytest.mark.django_db
def test_other_orgs_do_not_see_it(staff_client, customer, client):
    start(staff_client, customer)
    client.force_login(make_user("stranger@example.com"))

    assert b"support viewed your account" not in client.get(reverse("organizations:settings")).content


@pytest.mark.django_db
def test_session_store_is_database_backed_so_marker_dies_with_the_session(staff_client, customer):
    start(staff_client, customer)
    key = staff_client.session.session_key

    staff_client.post(reverse("accounts:logout"))

    assert not SessionStore(session_key=key).exists(key)
