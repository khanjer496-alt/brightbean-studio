"""An invitation can only be accepted by the address it was sent to.

The token in the link proves that someone opened it, not who: invitation
emails get forwarded, pasted into chats and land in shared inboxes. So
whoever holds the token must still sign up (or be signed in) under the
invited address, compared case-insensitively, or the invite stays unused.
"""

import copy
import threading
import uuid
from datetime import timedelta

import pytest
from allauth.account.models import EmailAddress
from allauth.core import context
from allauth.socialaccount.helpers import complete_social_login
from allauth.socialaccount.models import SocialAccount, SocialLogin
from django.contrib.auth.models import AnonymousUser
from django.contrib.messages.middleware import MessageMiddleware
from django.contrib.sessions.middleware import SessionMiddleware
from django.db import connection, transaction
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.members.models import Invitation, OrgMembership
from apps.members.services import accept_invitation
from apps.organizations.models import Organization


@pytest.fixture
def google(settings):
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


def _google_callback(email, session):
    request = RequestFactory().get("/accounts/google/login/callback/")
    SessionMiddleware(lambda r: None).process_request(request)
    MessageMiddleware(lambda r: None).process_request(request)
    request.user = AnonymousUser()
    for key, value in session.items():
        request.session[key] = value

    account = SocialAccount(provider="google", uid=f"uid-{uuid.uuid4()}", extra_data={"email": email})
    sociallogin = SocialLogin(
        user=User(email=email),
        account=account,
        email_addresses=[EmailAddress(email=email, verified=True, primary=True)],
    )
    sociallogin.state = {"process": "login"}
    with context.request_context(request):
        sociallogin.provider = account.get_provider(request)
        return complete_social_login(request, sociallogin)


@pytest.mark.django_db
class TestInviteModeSignup:
    def test_google_signup_under_another_email_is_refused(self, settings, google, invitation):
        settings.SIGNUP_MODE = "invite"

        response = _google_callback("someone-else@gmail.com", {"pending_invite_token": invitation.token})

        assert response.status_code == 200
        assert b"This invitation was sent to invitee@example.com" in response.content
        assert not User.objects.filter(email="someone-else@gmail.com").exists()
        invitation.refresh_from_db()
        assert invitation.accepted_at is None

    def test_google_signup_under_the_invited_email_in_another_case_joins(self, settings, google, invitation):
        settings.SIGNUP_MODE = "invite"

        _google_callback("Invitee@Example.com", {"pending_invite_token": invitation.token})

        user = User.objects.get(email__iexact="invitee@example.com")
        assert OrgMembership.objects.filter(user=user, organization=invitation.organization).exists()

    def test_form_cannot_be_posted_with_another_email(self, settings, client, invitation):
        """The field is disabled, so a tampered POST still signs up the invited address."""
        settings.SIGNUP_MODE = "invite"
        client.get(reverse("members:accept_invite", args=[invitation.token]))

        client.post(
            reverse("account_signup"), {"email": "someone-else@example.com", "password1": "S3cure-pass-for-tests!"}
        )

        assert not User.objects.filter(email="someone-else@example.com").exists()
        user = User.objects.get(email="invitee@example.com")
        assert OrgMembership.objects.filter(user=user, organization=invitation.organization).exists()


@pytest.mark.django_db
class TestOpenModeSignup:
    def test_google_signup_under_another_email_does_not_join(self, settings, google, invitation):
        settings.SIGNUP_MODE = "open"

        _google_callback("someone-else@gmail.com", {"pending_invite_token": invitation.token})

        user = User.objects.get(email="someone-else@gmail.com")
        assert not OrgMembership.objects.filter(user=user, organization=invitation.organization).exists()
        invitation.refresh_from_db()
        assert invitation.accepted_at is None


@pytest.mark.django_db
class TestAcceptInvitation:
    def test_signed_in_user_with_another_email_is_refused(self, invitation):
        other = User.objects.create_user(email="someone-else@example.com")

        with pytest.raises(ValueError, match="different email address"):
            accept_invitation(invitation, other)

        assert not OrgMembership.objects.filter(user=other, organization=invitation.organization).exists()

    def test_accept_page_shows_the_refusal(self, client, invitation):
        other = User.objects.create_user(email="someone-else@example.com", tos_accepted_at=timezone.now())
        client.force_login(other)

        response = client.post(reverse("members:accept_invite", args=[invitation.token]))

        assert b"This invitation was sent to a different email address." in response.content
        invitation.refresh_from_db()
        assert invitation.accepted_at is None


@pytest.mark.django_db(transaction=True)
def test_concurrent_acceptances_accept_once(invitation):
    if connection.vendor != "postgresql":
        pytest.skip("needs real row locks")
    user = User.objects.create_user(email="invitee@example.com")
    first_inside = threading.Event()
    release_first = threading.Event()
    outcomes = {}

    def accept(name, hold):
        try:
            with transaction.atomic():
                accept_invitation(invitation, user)
                if hold:
                    first_inside.set()
                    release_first.wait(10)
            outcomes[name] = "accepted"
        except ValueError:
            outcomes[name] = "refused"
        finally:
            connection.close()

    first = threading.Thread(target=accept, args=("first", True))
    second = threading.Thread(target=accept, args=("second", False))
    first.start()
    assert first_inside.wait(10)
    second.start()
    second.join(1)  # blocked on the invitation's row lock
    assert second.is_alive()
    release_first.set()
    first.join(10)
    second.join(10)

    assert outcomes == {"first": "accepted", "second": "refused"}
