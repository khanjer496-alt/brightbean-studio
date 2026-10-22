"""Two-factor authentication through allauth.mfa (authenticator app + recovery codes).

What has to hold: once a user turns it on, no way of signing in skips the
code — not the password form, and not Django's admin login, which would
otherwise accept a password alone. Setting it up happens on Settings >
Security, inside the account settings layout.
"""

from html.parser import HTMLParser

import pytest
from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator
from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
from allauth.mfa.totp.internal.auth import (
    SECRET_SESSION_KEY,
    TOTP,
    format_hotp_value,
    generate_totp_secret,
    hotp_value,
    yield_hotp_counters_from_time,
)
from django.urls import reverse
from django.utils import timezone

from apps.accounts.middleware import AUTH_RATE_LIMIT
from apps.accounts.models import User

PASSWORD = "S3cure-pass-for-tests!"


def _current_code(secret):
    return format_hotp_value(hotp_value(secret, next(yield_hotp_counters_from_time())))


@pytest.fixture
def member(db):
    user = User.objects.create_user(email="member@example.com", password=PASSWORD, tos_accepted_at=timezone.now())
    EmailAddress.objects.create(user=user, email="member@example.com", verified=True, primary=True)
    return user


@pytest.fixture
def signed_in(client, member):
    """Signed in and freshly reauthenticated, as allauth asks before changing 2FA."""
    client.post(reverse("account_login"), {"login": "member@example.com", "password": PASSWORD})
    client.post(reverse("account_reauthenticate"), {"password": PASSWORD})
    return client


@pytest.fixture
def totp_secret(member):
    secret = generate_totp_secret()
    TOTP.activate(member, secret)
    RecoveryCodes.activate(member)
    return secret


class Labels(HTMLParser):
    """Maps input names to ids, and collects the ``for`` targets of labels."""

    def __init__(self, html):
        super().__init__()
        self.ids, self.labelled = {}, set()
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and attrs.get("name"):
            self.ids[attrs["name"]] = attrs.get("id")
        if tag == "label" and attrs.get("for"):
            self.labelled.add(attrs["for"])

    def is_labelled(self, name):
        return self.ids.get(name) is not None and self.ids[name] in self.labelled


@pytest.mark.django_db
class TestSettingUp:
    def test_security_page_is_in_the_settings_layout(self, signed_in):
        response = signed_in.get(reverse("mfa_index"))

        assert response.status_code == 200
        content = response.content.decode()
        assert 'class="sidebar-nav-item mb-1 active"' in content
        assert "Authenticator app" in content
        assert reverse("mfa_activate_totp") in content

    def test_profile_page_links_to_security(self, signed_in):
        assert reverse("mfa_index") in signed_in.get(reverse("accounts:settings")).content.decode()

    def test_setup_asks_for_the_password_again(self, client, member):
        client.post(reverse("account_login"), {"login": "member@example.com", "password": PASSWORD})

        response = client.get(reverse("mfa_activate_totp"))

        assert response.status_code == 302
        assert response.url.startswith(reverse("account_reauthenticate"))
        assert Labels(client.get(response.url).content.decode()).is_labelled("password")

    def test_activation_with_a_valid_code(self, signed_in, member):
        page = signed_in.get(reverse("mfa_activate_totp"))
        assert page.status_code == 200
        assert Labels(page.content.decode()).is_labelled("code")
        secret = signed_in.session[SECRET_SESSION_KEY]

        response = signed_in.post(reverse("mfa_activate_totp"), {"code": _current_code(secret)})

        assert response.status_code == 302
        assert Authenticator.objects.filter(user=member, type=Authenticator.Type.TOTP).exists()
        assert Authenticator.objects.filter(user=member, type=Authenticator.Type.RECOVERY_CODES).exists()

    def test_activation_with_a_wrong_code_shows_the_error(self, signed_in, member):
        signed_in.get(reverse("mfa_activate_totp"))

        response = signed_in.post(reverse("mfa_activate_totp"), {"code": "000000"})

        assert response.status_code == 200
        assert 'aria-invalid="true"' in response.content.decode()
        assert not Authenticator.objects.filter(user=member).exists()

    def test_recovery_codes_are_listed(self, signed_in, member, totp_secret):
        response = signed_in.get(reverse("mfa_view_recovery_codes"))

        codes = RecoveryCodes(Authenticator.objects.get(user=member, type="recovery_codes")).get_unused_codes()
        assert all(code in response.content.decode() for code in codes)


@pytest.mark.django_db
class TestSigningIn:
    def _password(self, client):
        return client.post(reverse("account_login"), {"login": "member@example.com", "password": PASSWORD})

    def test_password_alone_is_not_enough(self, client, totp_secret):
        response = self._password(client)

        assert response.status_code == 302
        assert response.url == reverse("mfa_authenticate")
        assert not response.wsgi_request.user.is_authenticated

    def test_code_page_labels_its_input(self, client, totp_secret):
        self._password(client)
        assert Labels(client.get(reverse("mfa_authenticate")).content.decode()).is_labelled("code")

    def test_authenticator_code_completes_the_login(self, client, member, totp_secret):
        self._password(client)

        response = client.post(reverse("mfa_authenticate"), {"code": _current_code(totp_secret)})

        assert response.status_code == 302
        assert response.wsgi_request.user == member

    def test_recovery_code_completes_the_login(self, client, member, totp_secret):
        code = RecoveryCodes(Authenticator.objects.get(user=member, type="recovery_codes")).get_unused_codes()[0]
        self._password(client)

        response = client.post(reverse("mfa_authenticate"), {"code": code})

        assert response.wsgi_request.user == member

    def test_wrong_code_does_not(self, client, totp_secret):
        self._password(client)

        response = client.post(reverse("mfa_authenticate"), {"code": "000000"})

        assert not response.wsgi_request.user.is_authenticated

    def test_code_guessing_is_rate_limited(self, client, totp_secret):
        self._password(client)  # one of the limit's attempts
        for _ in range(AUTH_RATE_LIMIT - 1):
            client.post(reverse("mfa_authenticate"), {"code": "000000"})

        assert client.post(reverse("mfa_authenticate"), {"code": "000000"}).status_code == 429


@pytest.mark.django_db
class TestAdminLogin:
    def test_admin_sends_staff_to_the_app_login(self, client):
        response = client.get(reverse("admin:login"))

        assert response.status_code == 302
        assert response.url.startswith(reverse("account_login"))

    def test_admin_login_form_cannot_be_posted_to(self, client, member, totp_secret):
        member.is_staff = True
        member.save(update_fields=["is_staff"])

        client.post(reverse("admin:login"), {"username": "member@example.com", "password": PASSWORD})

        assert not client.get(reverse("admin:index")).wsgi_request.user.is_authenticated


@pytest.mark.django_db
class TestTurningOff:
    def test_deactivation_removes_the_authenticator(self, signed_in, member, totp_secret):
        assert signed_in.get(reverse("mfa_deactivate_totp")).status_code == 200

        response = signed_in.post(reverse("mfa_deactivate_totp"))

        assert response.status_code == 302
        assert not Authenticator.objects.filter(user=member, type=Authenticator.Type.TOTP).exists()
