"""Email ownership has to be proven before an address can stand for an account.

The attack this closes is the pre-account hijack: register someone else's
address with a password, then wait. When the real owner later signs in with
Google, matching by email would drop them into the attacker's account, with
the attacker's password still working. So a password signup must confirm its
address before it can sign in, a Google login only connects to an account
whose address is verified, and auto-signup only trusts emails the provider
itself vouches for.

Invitees are no exception: an invite link can be forwarded.
"""

import copy
import importlib
import uuid
from datetime import timedelta

import pytest
from allauth.account.models import EmailAddress
from allauth.core import context
from allauth.socialaccount.helpers import complete_social_login
from allauth.socialaccount.models import SocialAccount, SocialLogin
from django.apps import apps as django_apps
from django.contrib.auth.models import AnonymousUser
from django.contrib.messages.middleware import MessageMiddleware
from django.contrib.sessions.middleware import SessionMiddleware
from django.core import mail
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.members.models import Invitation
from apps.organizations.models import Organization

PASSWORD = "S3cure-pass-for-tests!"

migration = importlib.import_module("apps.accounts.migrations.0006_verify_emails_of_existing_logins")


@pytest.fixture
def google(settings):
    """A configured Google app, so the provider resolves in the callback flow."""
    providers = copy.deepcopy(settings.SOCIALACCOUNT_PROVIDERS)
    providers["google"]["APP"] = {"client_id": "test-client", "secret": "test-secret"}
    settings.SOCIALACCOUNT_PROVIDERS = providers


@pytest.fixture
def invitation(db):
    return Invitation.objects.create(
        organization=Organization.objects.create(name="Inviting Org"),
        email="invitee@example.com",
        expires_at=timezone.now() + timedelta(days=7),
    )


def _google_callback(email, *, verified=True):
    """Run allauth's provider-callback flow for a Google email."""
    request = RequestFactory().get("/accounts/google/login/callback/")
    SessionMiddleware(lambda r: None).process_request(request)
    MessageMiddleware(lambda r: None).process_request(request)
    request.user = AnonymousUser()

    account = SocialAccount(provider="google", uid=f"uid-{uuid.uuid4()}", extra_data={"email": email})
    sociallogin = SocialLogin(
        user=User(email=email),
        account=account,
        email_addresses=[EmailAddress(email=email, verified=verified, primary=True)],
    )
    sociallogin.state = {"process": "login"}
    with context.request_context(request):
        sociallogin.provider = account.get_provider(request)
        return complete_social_login(request, sociallogin), request


@pytest.mark.django_db
class TestPasswordSignup:
    def test_signup_does_not_sign_in_until_the_address_is_confirmed(self, client):
        response = client.post(reverse("account_signup"), {"email": "new@example.com", "password1": PASSWORD})

        assert response.status_code == 302
        assert response.url == reverse("account_email_verification_sent")
        assert not response.wsgi_request.user.is_authenticated
        assert not EmailAddress.objects.get(email="new@example.com").verified
        assert [m.to for m in mail.outbox] == [["new@example.com"]]

    def test_unconfirmed_account_cannot_sign_in(self, client):
        client.post(reverse("account_signup"), {"email": "new@example.com", "password1": PASSWORD})

        response = client.post(reverse("account_login"), {"login": "new@example.com", "password": PASSWORD})

        assert response.status_code == 302
        assert response.url == reverse("account_email_verification_sent")
        assert not response.wsgi_request.user.is_authenticated

    def test_confirmed_account_signs_in(self, client):
        user = User.objects.create_user(email="known@example.com", password=PASSWORD)
        EmailAddress.objects.create(user=user, email="known@example.com", verified=True, primary=True)

        response = client.post(reverse("account_login"), {"login": "known@example.com", "password": PASSWORD})

        assert response.status_code == 302
        assert response.wsgi_request.user == user

    def test_invite_link_does_not_verify_the_address(self, client, invitation):
        """A forwarded link must not let its holder claim the invited address with their own password."""
        client.get(reverse("members:accept_invite", args=[invitation.token]))

        response = client.post(reverse("account_signup"), {"email": "invitee@example.com", "password1": PASSWORD})

        assert response.status_code == 302
        assert not response.wsgi_request.user.is_authenticated
        assert not EmailAddress.objects.get(email="invitee@example.com").verified
        assert len(mail.outbox) == 1


@pytest.mark.django_db
class TestGoogleLogin:
    def test_does_not_connect_to_an_unverified_account(self, google):
        squatter = User.objects.create_user(email="owner@gmail.com", password=PASSWORD)
        EmailAddress.objects.create(user=squatter, email="owner@gmail.com", verified=False, primary=True)

        response, request = _google_callback("owner@gmail.com")

        # allauth tells the address's owner an account already exists, and
        # shows the visitor the same page a verification email would.
        assert response.status_code == 302
        assert response.url == reverse("account_email_verification_sent")
        assert not request.user.is_authenticated
        assert not SocialAccount.objects.filter(user=squatter).exists()
        assert [m.to for m in mail.outbox] == [["owner@gmail.com"]]

    def test_connects_to_a_verified_account(self, google):
        owner = User.objects.create_user(email="owner@gmail.com", password=PASSWORD)
        EmailAddress.objects.create(user=owner, email="owner@gmail.com", verified=True, primary=True)

        response, request = _google_callback("owner@gmail.com")

        assert response.status_code == 302
        assert request.user == owner
        assert SocialAccount.objects.filter(user=owner, provider="google").exists()

    def test_verified_google_email_signs_up_automatically(self, google):
        response, request = _google_callback("new@gmail.com")

        assert response.status_code == 302
        user = User.objects.get(email="new@gmail.com")
        assert request.user == user
        assert EmailAddress.objects.get(user=user).verified

    def test_unverified_google_email_does_not_sign_up_automatically(self, google):
        response, request = _google_callback("new@gmail.com", verified=False)

        assert response.status_code == 302
        assert response.url == reverse("socialaccount_signup")
        assert not User.objects.filter(email="new@gmail.com").exists()


@pytest.mark.django_db
class TestExistingUsersMigration:
    def _run(self):
        migration.verify_emails_of_existing_logins(django_apps, None)

    def test_marks_users_who_signed_in_verified(self):
        user = User.objects.create_user(email="Old@Example.com", password=PASSWORD, last_login=timezone.now())
        EmailAddress.objects.create(user=user, email="old@example.com", verified=False, primary=True)

        self._run()

        assert EmailAddress.objects.get(user=user).verified

    def test_creates_the_missing_address(self):
        user = User.objects.create_user(email="admin-made@example.com", last_login=timezone.now())

        self._run()

        address = EmailAddress.objects.get(user=user)
        assert (address.email, address.verified, address.primary) == ("admin-made@example.com", True, True)

    def test_leaves_never_signed_in_users_unverified(self):
        user = User.objects.create_user(email="never@example.com", password=PASSWORD)
        EmailAddress.objects.create(user=user, email="never@example.com", verified=False, primary=True)

        self._run()

        assert not EmailAddress.objects.get(user=user).verified

    def test_skips_an_address_someone_else_verified(self):
        owner = User.objects.create_user(email="shared@example.com", last_login=timezone.now())
        EmailAddress.objects.create(user=owner, email="shared@example.com", verified=True, primary=True)
        other = User.objects.create_user(email="SHARED@example.com", last_login=timezone.now())

        self._run()

        assert not EmailAddress.objects.filter(user=other, verified=True).exists()

    def test_migrated_user_keeps_signing_in(self, client):
        User.objects.create_user(email="old@example.com", password=PASSWORD, last_login=timezone.now())
        self._run()

        response = client.post(reverse("account_login"), {"login": "old@example.com", "password": PASSWORD})

        assert response.wsgi_request.user.is_authenticated
