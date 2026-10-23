"""Connecting X: the customer enters their own app's credentials, then OAuth 2.0 + PKCE runs against that app."""

import base64
import hashlib
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest
from django.urls import reverse

from apps.credentials.models import PlatformCredential, resolve_platform_credentials
from apps.social_accounts.models import SocialAccount
from apps.social_accounts.views import OAUTH_SESSION_KEY
from providers.types import AccountProfile, OAuthTokens


@pytest.fixture
def workspace(db, organization):
    from apps.workspaces.models import Workspace

    return Workspace.objects.create(name="Test WS", organization=organization)


@pytest.fixture
def authenticated_client(client, user, organization, workspace):
    from apps.members.models import OrgMembership, WorkspaceMembership

    OrgMembership.objects.create(user=user, organization=organization, org_role="owner")
    WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role="manager")
    client.force_login(user)
    return client


def form_url(workspace):
    return reverse("social_accounts:connect_x", kwargs={"workspace_id": workspace.id})


def connect_url(workspace):
    return reverse("social_accounts:connect", kwargs={"workspace_id": workspace.id})


@pytest.mark.django_db
class TestConnectForm:
    def test_offered_without_any_platform_wide_credentials(self, authenticated_client, workspace):
        body = authenticated_client.get(connect_url(workspace)).content.decode()
        assert 'value="x"' in body

    def test_connect_button_goes_to_the_credentials_form(self, authenticated_client, workspace):
        response = authenticated_client.post(connect_url(workspace), {"platform": "x"})
        assert response.status_code == 302
        assert response.url == form_url(workspace)

    def test_the_guide_shows_the_exact_callback_url_steps_and_the_billing_note(self, authenticated_client, workspace):
        body = authenticated_client.get(form_url(workspace)).content.decode()
        callback = reverse("social_accounts:oauth_callback", kwargs={"platform": "x"})
        assert f"http://testserver{callback}" in body
        assert "X bills you for each post" in body
        assert "Client Secret" in body
        assert "OAuth 2.0" in body

    def test_requires_login(self, client, workspace):
        assert client.get(form_url(workspace)).status_code == 302

    def test_missing_credentials_are_refused(self, authenticated_client, workspace, organization):
        response = authenticated_client.post(form_url(workspace), {"client_id": "abc", "client_secret": ""})
        assert response.status_code == 200
        assert "Enter the Client ID and Client Secret" in response.content.decode()
        assert not PlatformCredential.objects.filter(organization=organization, platform="x").exists()


@pytest.mark.django_db
class TestStartOAuth:
    def start(self, client, workspace, **form):
        return client.post(form_url(workspace), {"client_id": "cid", "client_secret": "csecret", **form})

    def test_saves_the_credentials_encrypted_per_organization_and_redirects_to_x_with_pkce(
        self, authenticated_client, workspace, organization
    ):
        response = self.start(authenticated_client, workspace)
        assert response.status_code == 302
        saved = PlatformCredential.objects.get(organization=organization, platform="x")
        assert saved.credentials == {"client_id": "cid", "client_secret": "csecret"}
        assert saved.is_configured is True
        # Stored encrypted at rest, not as the plain JSON.
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute("SELECT credentials FROM credentials_platform_credential WHERE id = %s", [saved.id])
            assert "csecret" not in cursor.fetchone()[0]

        url = urlparse(response.url)
        assert f"{url.scheme}://{url.netloc}{url.path}" == "https://x.com/i/oauth2/authorize"
        query = parse_qs(url.query)
        assert query["client_id"] == ["cid"]
        assert query["code_challenge_method"] == ["S256"]
        verifier = authenticated_client.session[OAUTH_SESSION_KEY]["code_verifier"]
        digest = hashlib.sha256(verifier.encode()).digest()
        assert query["code_challenge"] == [base64.urlsafe_b64encode(digest).rstrip(b"=").decode()]
        assert query["redirect_uri"] == [
            "http://testserver" + reverse("social_accounts:oauth_callback", kwargs={"platform": "x"})
        ]
        assert "csecret" not in response.url

    def test_credentials_are_not_shared_with_other_organizations(self, authenticated_client, workspace):
        from apps.organizations.models import Organization

        self.start(authenticated_client, workspace)
        other = Organization.objects.create(name="Other")
        assert resolve_platform_credentials("x", other.id) == {}

    def test_a_blank_secret_keeps_the_saved_one_and_the_form_never_shows_it(
        self, authenticated_client, workspace, organization
    ):
        self.start(authenticated_client, workspace)
        body = authenticated_client.get(form_url(workspace)).content.decode()
        assert "csecret" not in body
        assert 'value="cid"' in body
        assert "Leave blank to keep it" in body
        response = authenticated_client.post(form_url(workspace), {"client_id": "cid", "client_secret": ""})
        assert response.status_code == 302
        assert (
            PlatformCredential.objects.get(organization=organization, platform="x").credentials["client_secret"]
            == "csecret"
        )

    def test_another_app_cannot_replace_the_credentials_while_accounts_depend_on_them(
        self, authenticated_client, workspace, organization
    ):
        self.start(authenticated_client, workspace)
        SocialAccount.objects.create(
            workspace=workspace, platform="x", account_platform_id="1", account_name="Alice", oauth_access_token="t"
        )
        response = self.start(authenticated_client, workspace, client_id="other-app")
        assert response.status_code == 200
        assert "Disconnect them" in response.content.decode()
        assert PlatformCredential.objects.get(organization=organization, platform="x").credentials["client_id"] == "cid"

    def test_reconnect_returns_to_the_form(self, authenticated_client, workspace):
        account = SocialAccount.objects.create(
            workspace=workspace, platform="x", account_platform_id="1", account_name="Alice"
        )
        response = authenticated_client.post(
            reverse("social_accounts:reconnect", kwargs={"workspace_id": workspace.id, "account_id": account.id})
        )
        assert response.url == form_url(workspace)


@pytest.mark.django_db
class TestCallback:
    def callback(self, client, workspace):
        start = client.post(form_url(workspace), {"client_id": "cid", "client_secret": "csecret"})
        state = parse_qs(urlparse(start.url).query)["state"][0]
        url = reverse("social_accounts:oauth_callback", kwargs={"platform": "x"})
        return client.get(url, {"code": "auth-code", "state": state})

    def tokens(self):
        return OAuthTokens(access_token="at", refresh_token="rt", expires_in=7200)

    def profile(self, premium):
        return AccountProfile(
            platform_id="42",
            name="Alice",
            handle="alice",
            follower_count=5,
            extra={"account_data": {"premium": premium}},
        )

    def test_the_verifier_goes_back_to_x_and_the_account_is_created(self, authenticated_client, workspace):
        with (
            patch("providers.x.XProvider.exchange_code", return_value=self.tokens()) as exchange,
            patch("providers.x.XProvider.get_profile", return_value=self.profile(False)),
        ):
            response = self.callback(authenticated_client, workspace)
        assert response.status_code == 302
        _args, kwargs = exchange.call_args
        assert kwargs["code_verifier"]
        account = SocialAccount.objects.get(workspace=workspace, platform="x")
        assert (account.account_platform_id, account.account_handle) == ("42", "alice")
        assert account.oauth_access_token == "at"
        assert account.oauth_refresh_token == "rt"
        assert account.token_expires_at is not None
        assert account.char_limit == 280

    def test_a_premium_account_gets_the_long_limit(self, authenticated_client, workspace):
        with (
            patch("providers.x.XProvider.exchange_code", return_value=self.tokens()),
            patch("providers.x.XProvider.get_profile", return_value=self.profile(True)),
        ):
            self.callback(authenticated_client, workspace)
        assert SocialAccount.objects.get(workspace=workspace, platform="x").char_limit == 25000

    def test_the_callback_uses_the_customers_own_app(self, authenticated_client, workspace):
        seen = {}

        def exchange(self, code, redirect_uri, code_verifier=None):
            seen.update(self.credentials)
            return OAuthTokens(access_token="at", refresh_token="rt", expires_in=7200)

        with (
            patch("providers.x.XProvider.exchange_code", autospec=True, side_effect=exchange),
            patch("providers.x.XProvider.get_profile", return_value=self.profile(False)),
        ):
            self.callback(authenticated_client, workspace)
        assert seen == {"client_id": "cid", "client_secret": "csecret"}
