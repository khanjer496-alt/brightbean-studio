"""Connecting Tumblr, Lemmy, Nostr and Farcaster: the views, the connect page and publish-time credentials."""

from unittest.mock import patch

import pytest
from django.urls import reverse

from apps.credentials.models import PlatformCredential
from apps.publisher.engine import _resolve_publish_credentials
from apps.social_accounts.models import SocialAccount
from providers.exceptions import APIError, OAuthError
from providers.types import AccountProfile, OAuthTokens

NSEC = "nsec1vl029mgpspedva04g90vltkh6fvh240zqtv9k0t9af8935ke9laqsnlfe5"
SECRET_HEX = "67dea2ed018072d675f5415ecfaed7d2597555e202d85b3d65ea4e58d2d92ffa"
PUBKEY = "7e7e9c42a91bfef19fa929e5fda1b72e0ebc1a4c1141673e2794234d86addf4e"


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


def url(name, workspace):
    return reverse(f"social_accounts:{name}", kwargs={"workspace_id": workspace.id})


@pytest.mark.django_db
class TestConnectPage:
    @pytest.mark.parametrize("platform", ["lemmy", "nostr", "farcaster"])
    def test_user_supplied_networks_are_offered_without_platform_credentials(
        self, authenticated_client, workspace, platform
    ):
        body = authenticated_client.get(url("connect", workspace)).content.decode()
        assert f'value="{platform}"' in body

    def test_tumblr_is_offered_only_when_its_app_is_configured(self, authenticated_client, workspace, settings):
        settings.PLATFORM_CREDENTIALS_FROM_ENV = {
            **settings.PLATFORM_CREDENTIALS_FROM_ENV,
            "tumblr": {"client_id": "", "client_secret": ""},
        }
        assert 'value="tumblr"' not in authenticated_client.get(url("connect", workspace)).content.decode()
        settings.PLATFORM_CREDENTIALS_FROM_ENV = {
            **settings.PLATFORM_CREDENTIALS_FROM_ENV,
            "tumblr": {"client_id": "key", "client_secret": "secret"},
        }
        assert 'value="tumblr"' in authenticated_client.get(url("connect", workspace)).content.decode()

    @pytest.mark.parametrize("platform", ["lemmy", "nostr", "farcaster"])
    def test_connect_button_goes_to_the_form_not_oauth(self, authenticated_client, workspace, platform):
        response = authenticated_client.post(url("connect", workspace), {"platform": platform})
        assert response.status_code == 302
        assert response.url == url(f"connect_{platform}", workspace)

    @pytest.mark.parametrize("platform", ["lemmy", "nostr", "farcaster"])
    def test_form_renders(self, authenticated_client, workspace, platform):
        response = authenticated_client.get(url(f"connect_{platform}", workspace))
        assert response.status_code == 200
        assert f"Connect {platform.title()}" in response.content.decode()


@pytest.mark.django_db
class TestTumblrBlogPicker:
    def test_each_chosen_blog_keeps_the_grants_refresh_token_and_expiry(self, authenticated_client, workspace):
        session = authenticated_client.session
        session["oauth_page_select"] = {
            "workspace_id": str(workspace.id),
            "platform": "tumblr",
            "user_tokens": {"access_token": "", "refresh_token": None, "expires_in": None},
            "pages": [
                {
                    "id": "t:1",
                    "name": "First",
                    "handle": "first",
                    "picture": "",
                    "access_token": "at",
                    "refresh_token": "rt",
                    "expires_in": 2520,
                },
                {"id": "t:2", "name": "Second", "handle": "second", "access_token": "at", "refresh_token": "rt"},
            ],
        }
        session.save()
        response = authenticated_client.post(reverse("social_accounts:select_account"), {"selected_pages": ["t:1"]})
        assert response.status_code == 302
        account = SocialAccount.objects.get(workspace=workspace, platform="tumblr")
        assert (account.account_handle, account.oauth_access_token, account.oauth_refresh_token) == (
            "first",
            "at",
            "rt",
        )
        assert account.token_expires_at is not None
        assert not SocialAccount.objects.filter(account_platform_id="t:2").exists()


@pytest.mark.django_db
class TestLemmyConnect:
    FORM = {"instance_url": "lemmy.example", "username": "alice", "password": "hunter2", "totp_token": ""}

    def provider(self, **overrides):
        from unittest.mock import MagicMock

        mock = MagicMock()
        mock.login.return_value = OAuthTokens(access_token="the-jwt")
        mock.get_profile.return_value = AccountProfile(
            platform_id="7@lemmy.example", name="Alice", handle="alice@lemmy.example"
        )
        for key, value in overrides.items():
            setattr(mock, key, value)
        return mock

    def test_connects_with_the_jwt_and_never_stores_the_password(self, authenticated_client, workspace):
        mock = self.provider()
        with (
            patch("apps.social_accounts.views._is_safe_url", return_value=True),
            patch("apps.social_accounts.views._get_provider_for_platform", return_value=mock) as factory,
        ):
            response = authenticated_client.post(url("connect_lemmy", workspace), self.FORM)
        assert response.status_code == 302
        assert factory.call_args.kwargs["instance_url"] == "https://lemmy.example"
        mock.login.assert_called_once_with("alice", "hunter2", "")
        account = SocialAccount.objects.get(workspace=workspace, platform="lemmy")
        assert account.oauth_access_token == "the-jwt"
        assert account.instance_url == "https://lemmy.example"
        assert "hunter2" not in repr(account.__dict__)

    def test_a_private_instance_is_refused_before_any_login(self, authenticated_client, workspace):
        with (
            patch("apps.social_accounts.views._is_safe_url", return_value=False),
            patch("apps.social_accounts.views._get_provider_for_platform") as factory,
        ):
            response = authenticated_client.post(url("connect_lemmy", workspace), self.FORM)
        assert "Private or reserved addresses" in response.content.decode()
        factory.assert_not_called()
        assert not SocialAccount.objects.filter(platform="lemmy").exists()

    def test_a_failed_login_does_not_echo_the_password(self, authenticated_client, workspace):
        mock = self.provider()
        mock.login.side_effect = OAuthError("nope")
        with (
            patch("apps.social_accounts.views._is_safe_url", return_value=True),
            patch("apps.social_accounts.views._get_provider_for_platform", return_value=mock),
        ):
            response = authenticated_client.post(url("connect_lemmy", workspace), self.FORM)
        body = response.content.decode()
        assert "Failed to connect Lemmy" in body and "hunter2" not in body
        assert not SocialAccount.objects.filter(platform="lemmy").exists()

    def test_missing_fields_are_refused(self, authenticated_client, workspace):
        response = authenticated_client.post(url("connect_lemmy", workspace), {"instance_url": "lemmy.example"})
        assert "required" in response.content.decode()

    def test_publish_credentials_carry_the_instance_when_public(self, authenticated_client, workspace):
        account = SocialAccount.objects.create(
            workspace=workspace,
            platform="lemmy",
            account_platform_id="1@x",
            account_name="A",
            instance_url="https://lemmy.example",
        )
        with patch("providers.lemmy.is_safe_url", return_value=True):
            assert _resolve_publish_credentials(account)["instance_url"] == "https://lemmy.example"
        with patch("providers.lemmy.is_safe_url", return_value=False):
            assert "instance_url" not in _resolve_publish_credentials(account)


@pytest.mark.django_db
class TestNostrConnect:
    def test_connects_with_an_encrypted_key_and_the_relay_list(self, authenticated_client, workspace):
        with patch("providers.nostr._fetch_profile", return_value={"name": "alice", "display_name": "Alice"}):
            response = authenticated_client.post(
                url("connect_nostr", workspace),
                {"secret_key": NSEC, "relays": "wss://a.example\nws://plain.example\nwss://b.example"},
            )
        assert response.status_code == 302
        account = SocialAccount.objects.get(workspace=workspace, platform="nostr")
        assert account.account_platform_id == PUBKEY
        assert account.oauth_access_token == SECRET_HEX
        assert account.platform_data == {"relays": ["wss://a.example", "wss://b.example"]}
        assert _resolve_publish_credentials(account)["relays"] == ["wss://a.example", "wss://b.example"]

    def test_the_key_is_encrypted_at_rest(self, authenticated_client, workspace):
        from django.db import connection

        with patch("providers.nostr._fetch_profile", return_value={}):
            authenticated_client.post(
                url("connect_nostr", workspace), {"secret_key": SECRET_HEX, "relays": "wss://a.example"}
            )
        with connection.cursor() as cursor:
            cursor.execute(f"select oauth_access_token from {SocialAccount._meta.db_table} where platform = 'nostr'")
            stored = cursor.fetchone()[0]
        assert SECRET_HEX not in stored

    def test_a_bad_key_is_refused_and_not_echoed(self, authenticated_client, workspace):
        response = authenticated_client.post(
            url("connect_nostr", workspace), {"secret_key": "nsec1notakey", "relays": "wss://a.example"}
        )
        body = response.content.decode()
        assert "nsec1" in body and "nsec1notakey" not in body
        assert not SocialAccount.objects.filter(platform="nostr").exists()

    def test_relays_must_be_wss(self, authenticated_client, workspace):
        response = authenticated_client.post(
            url("connect_nostr", workspace), {"secret_key": SECRET_HEX, "relays": "ws://plain.example"}
        )
        assert "wss://" in response.content.decode()
        assert not SocialAccount.objects.filter(platform="nostr").exists()


@pytest.mark.django_db
class TestFarcasterConnect:
    PROFILE = AccountProfile(platform_id="3", name="Dan", handle="dwr")

    def mock_provider(self):
        from unittest.mock import MagicMock

        mock = MagicMock()
        mock.connect.return_value = (OAuthTokens(access_token="signer-1"), self.PROFILE)
        return mock

    def test_connects_and_stores_the_key_for_this_workspace_only(self, authenticated_client, workspace, organization):
        mock = self.mock_provider()
        with patch("apps.social_accounts.views._get_provider_for_platform", return_value=mock) as factory:
            response = authenticated_client.post(
                url("connect_farcaster", workspace), {"api_key": "neynar-key", "signer_uuid": "signer-1"}
            )
        assert response.status_code == 302
        assert factory.call_args.kwargs["api_key"] == "neynar-key"
        credential = PlatformCredential.objects.get(organization=organization, platform="farcaster")
        assert credential.workspace_id == workspace.id and credential.credentials == {"api_key": "neynar-key"}
        assert credential.is_configured is True
        account = SocialAccount.objects.get(workspace=workspace, platform="farcaster")
        assert (account.oauth_access_token, account.account_handle) == ("signer-1", "dwr")
        assert _resolve_publish_credentials(account)["api_key"] == "neynar-key"

    def test_an_unapproved_signer_stores_nothing(self, authenticated_client, workspace, organization):
        mock = self.mock_provider()
        mock.connect.side_effect = OAuthError("That signer is not approved yet (status: pending_approval).")
        with patch("apps.social_accounts.views._get_provider_for_platform", return_value=mock):
            response = authenticated_client.post(
                url("connect_farcaster", workspace), {"api_key": "neynar-key", "signer_uuid": "s"}
            )
        assert "not approved yet" in response.content.decode()
        assert not PlatformCredential.objects.filter(platform="farcaster").exists()
        assert not SocialAccount.objects.filter(platform="farcaster").exists()

    def test_a_neynar_failure_is_generic(self, authenticated_client, workspace):
        mock = self.mock_provider()
        mock.connect.side_effect = APIError("secret detail", status_code=500)
        with patch("apps.social_accounts.views._get_provider_for_platform", return_value=mock):
            response = authenticated_client.post(
                url("connect_farcaster", workspace), {"api_key": "neynar-key", "signer_uuid": "s"}
            )
        body = response.content.decode()
        assert "Failed to connect Farcaster" in body and "secret detail" not in body

    def test_a_second_account_reuses_the_saved_key(self, authenticated_client, workspace):
        mock = self.mock_provider()
        with patch("apps.social_accounts.views._get_provider_for_platform", return_value=mock):
            authenticated_client.post(url("connect_farcaster", workspace), {"api_key": "k1", "signer_uuid": "s1"})
            authenticated_client.post(url("connect_farcaster", workspace), {"api_key": "", "signer_uuid": "s2"})
        assert mock.connect.call_args.args == ("s2",)

    def test_switching_keys_with_accounts_connected_is_refused(self, authenticated_client, workspace, organization):
        mock = self.mock_provider()
        with patch("apps.social_accounts.views._get_provider_for_platform", return_value=mock):
            authenticated_client.post(url("connect_farcaster", workspace), {"api_key": "k1", "signer_uuid": "s1"})
            response = authenticated_client.post(
                url("connect_farcaster", workspace), {"api_key": "k2", "signer_uuid": "s2"}
            )
        assert "Disconnect them before using another key" in response.content.decode()
        assert PlatformCredential.objects.get(platform="farcaster").credentials == {"api_key": "k1"}

    def test_the_saved_key_is_never_sent_back_to_the_page(self, authenticated_client, workspace):
        mock = self.mock_provider()
        with patch("apps.social_accounts.views._get_provider_for_platform", return_value=mock):
            authenticated_client.post(
                url("connect_farcaster", workspace), {"api_key": "super-secret-key", "signer_uuid": "s1"}
            )
        body = authenticated_client.get(url("connect_farcaster", workspace)).content.decode()
        assert "super-secret-key" not in body and "Saved. Leave blank" in body
