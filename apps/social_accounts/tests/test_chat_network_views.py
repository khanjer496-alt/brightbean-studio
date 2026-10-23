"""Connecting Telegram, Discord, Slack and Reddit: the views and the connect page."""

from unittest.mock import patch

import pytest
from django.urls import reverse

from apps.social_accounts.models import SocialAccount
from providers.exceptions import APIError, PublishError
from providers.types import AccountProfile


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


def connect_url(workspace):
    return reverse("social_accounts:connect", kwargs={"workspace_id": workspace.id})


@pytest.mark.django_db
class TestConnectPageVisibility:
    """A network is offered only when its environment credentials are set."""

    @pytest.mark.parametrize(
        ("platform", "env"),
        [
            ("telegram", {"bot_token": "123:ABC"}),
            ("discord", {"client_id": "app", "client_secret": "shh", "bot_token": "bot"}),
            ("slack", {"client_id": "app", "client_secret": "shh"}),
        ],
    )
    def test_offered_when_configured_and_hidden_when_not(
        self, authenticated_client, workspace, settings, platform, env
    ):
        settings.PLATFORM_CREDENTIALS_FROM_ENV = {**settings.PLATFORM_CREDENTIALS_FROM_ENV, platform: env}
        body = authenticated_client.get(connect_url(workspace)).content.decode()
        assert f'value="{platform}"' in body

        blank = {key: "" for key in env}
        settings.PLATFORM_CREDENTIALS_FROM_ENV = {**settings.PLATFORM_CREDENTIALS_FROM_ENV, platform: blank}
        body = authenticated_client.get(connect_url(workspace)).content.decode()
        assert f'value="{platform}"' not in body


@pytest.mark.django_db
class TestTelegramConnect:
    @pytest.fixture(autouse=True)
    def configured(self, settings):
        settings.PLATFORM_CREDENTIALS_FROM_ENV = {
            **settings.PLATFORM_CREDENTIALS_FROM_ENV,
            "telegram": {"bot_token": "123:ABC"},
        }

    def url(self, workspace):
        return reverse("social_accounts:connect_telegram", kwargs={"workspace_id": workspace.id})

    def test_connect_button_goes_to_the_form_not_oauth(self, authenticated_client, workspace):
        response = authenticated_client.post(connect_url(workspace), {"platform": "telegram"})
        assert response.status_code == 302
        assert response.url == self.url(workspace)

    def test_form_names_the_bot_to_add(self, authenticated_client, workspace):
        with patch("providers.telegram.TelegramProvider.get_bot_username", return_value="pd_bot"):
            body = authenticated_client.get(self.url(workspace)).content.decode()
        assert "@pd_bot" in body

    def test_connecting_stores_the_chat_id_as_the_credential(self, authenticated_client, workspace):
        profile = AccountProfile(platform_id="-1001234567890", name="News", handle="@newsroom", follower_count=12)
        with patch("providers.telegram.TelegramProvider.get_profile", return_value=profile) as get_profile:
            response = authenticated_client.post(self.url(workspace), {"chat": "https://t.me/newsroom"})
        get_profile.assert_called_once_with("@newsroom")
        assert response.status_code == 302
        account = SocialAccount.objects.get(workspace=workspace, platform="telegram")
        assert account.account_platform_id == "-1001234567890"
        assert account.account_name == "News"
        assert account.account_handle == "@newsroom"
        assert account.oauth_access_token == "-1001234567890"
        assert account.connection_status == SocialAccount.ConnectionStatus.CONNECTED

    def test_a_bot_that_cannot_post_shows_why(self, authenticated_client, workspace):
        error = PublishError("The bot can't post there yet. Add it as an administrator.", retryable=False)
        with patch("providers.telegram.TelegramProvider.get_profile", side_effect=error):
            response = authenticated_client.post(self.url(workspace), {"chat": "@newsroom"})
        assert response.status_code == 200
        assert "Add it as an administrator" in response.content.decode()
        assert not SocialAccount.objects.filter(platform="telegram").exists()

    def test_an_unknown_chat_is_explained(self, authenticated_client, workspace):
        with patch(
            "providers.telegram.TelegramProvider.get_profile",
            side_effect=APIError("chat not found", status_code=400, retryable=False),
        ):
            response = authenticated_client.post(self.url(workspace), {"chat": "@missing_chat"})
        assert "couldn" in response.content.decode()
        assert not SocialAccount.objects.filter(platform="telegram").exists()

    def test_an_invalid_reference_never_calls_telegram(self, authenticated_client, workspace):
        with patch("providers.telegram.TelegramProvider.get_profile", side_effect=AssertionError("called")):
            response = authenticated_client.post(self.url(workspace), {"chat": "https://t.me/+invite"})
        assert response.status_code == 200
        assert "numeric chat id" in response.content.decode()

    def test_reconnect_returns_to_the_form(self, authenticated_client, workspace):
        account = SocialAccount.objects.create(
            workspace=workspace, platform="telegram", account_platform_id="-100", account_name="News"
        )
        response = authenticated_client.post(
            reverse("social_accounts:reconnect", kwargs={"workspace_id": workspace.id, "account_id": account.id})
        )
        assert response.url == self.url(workspace)

    def test_requires_login(self, client, workspace):
        assert client.get(self.url(workspace)).status_code == 302


def start_oauth(client, workspace, user, platform):
    """A callback request primed with a valid state and session, as the platform would send it."""
    from apps.social_accounts.views import OAUTH_SESSION_KEY, _sign_state

    nonce = "nonce-1"
    session = client.session
    session[OAUTH_SESSION_KEY] = {"nonce": nonce}
    session.save()
    state = _sign_state(workspace.id, platform, user.id, nonce)
    return reverse("social_accounts:oauth_callback", kwargs={"platform": platform}), state


@pytest.mark.django_db
class TestDiscordConnect:
    @pytest.fixture(autouse=True)
    def configured(self, settings):
        settings.PLATFORM_CREDENTIALS_FROM_ENV = {
            **settings.PLATFORM_CREDENTIALS_FROM_ENV,
            "discord": {"client_id": "app", "client_secret": "shh", "bot_token": "bot"},
        }

    def test_connect_redirects_to_the_bot_install(self, authenticated_client, workspace):
        response = authenticated_client.post(connect_url(workspace), {"platform": "discord"})
        assert response.status_code == 302
        assert response.url.startswith("https://discord.com/oauth2/authorize?")
        assert "scope=bot+guilds" in response.url

    def test_callback_offers_the_servers_channels_to_pick(self, authenticated_client, workspace, user):
        url, state = start_oauth(authenticated_client, workspace, user, "discord")
        destinations = [
            {"id": "c1", "name": "#general", "handle": "My Server", "access_token": "c1", "page_id": "g1"},
        ]
        with (
            patch("providers.discord.DiscordProvider.exchange_code") as exchange,
            patch("providers.discord.DiscordProvider.list_destinations", return_value=destinations),
        ):
            response = authenticated_client.get(url, {"code": "abc", "state": state})
        exchange.assert_called_once()
        assert response.url == reverse("social_accounts:select_account")
        page_data = authenticated_client.session["oauth_page_select"]
        assert page_data["platform"] == "discord"
        assert page_data["pages"][0]["already_connected"] is False
        # The user's token is not kept; each channel posts with its own id.
        assert page_data["user_tokens"] == {"access_token": "", "refresh_token": None, "expires_in": None}

    def test_a_server_with_no_postable_channel_says_so(self, authenticated_client, workspace, user):
        url, state = start_oauth(authenticated_client, workspace, user, "discord")
        with (
            patch("providers.discord.DiscordProvider.exchange_code"),
            patch("providers.discord.DiscordProvider.list_destinations", return_value=[]),
        ):
            response = authenticated_client.get(url, {"code": "abc", "state": state}, follow=True)
        assert "No Discord channels" in response.content.decode()
        assert not SocialAccount.objects.filter(platform="discord").exists()

    def test_picking_channels_creates_one_account_each(self, authenticated_client, workspace):
        session = authenticated_client.session
        session["oauth_page_select"] = {
            "workspace_id": str(workspace.id),
            "platform": "discord",
            "user_tokens": {"access_token": "", "refresh_token": None, "expires_in": None},
            "pages": [
                {"id": "c1", "name": "#general", "handle": "My Server", "access_token": "c1", "page_id": "g1"},
                {"id": "c2", "name": "#news", "handle": "My Server", "access_token": "c2", "page_id": "g1"},
            ],
        }
        session.save()
        response = authenticated_client.post(
            reverse("social_accounts:select_account"), {"selected_pages": ["c1", "c2"]}
        )
        assert response.status_code == 302
        accounts = SocialAccount.objects.filter(workspace=workspace, platform="discord").order_by("account_name")
        assert [a.account_name for a in accounts] == ["#general", "#news"]
        first = accounts[0]
        assert (first.account_platform_id, first.oauth_access_token, first.webhook_target_id) == ("c1", "c1", "g1")
        assert first.account_handle == "My Server"

    def test_publish_credentials_carry_the_server(self, workspace):
        from apps.publisher.engine import _resolve_publish_credentials

        account = SocialAccount.objects.create(
            workspace=workspace,
            platform="discord",
            account_platform_id="c1",
            account_name="#general",
            webhook_target_id="g1",
        )
        creds = _resolve_publish_credentials(account)
        assert creds["guild_id"] == "g1"
        assert creds["bot_token"] == "bot"

    def test_not_offered_on_a_client_connection_link(self, authenticated_client, workspace):
        from apps.onboarding.views import _supports_connection_link

        assert _supports_connection_link("discord") is False
        assert _supports_connection_link("facebook") is True


@pytest.mark.django_db
class TestSlackConnect:
    @pytest.fixture(autouse=True)
    def configured(self, settings):
        settings.PLATFORM_CREDENTIALS_FROM_ENV = {
            **settings.PLATFORM_CREDENTIALS_FROM_ENV,
            "slack": {"client_id": "app", "client_secret": "shh"},
        }

    def test_connect_redirects_to_the_slack_install(self, authenticated_client, workspace):
        response = authenticated_client.post(connect_url(workspace), {"platform": "slack"})
        assert response.url.startswith("https://slack.com/oauth/v2/authorize?")

    def test_no_member_channels_explains_how_to_invite_the_app(self, authenticated_client, workspace, user):
        url, state = start_oauth(authenticated_client, workspace, user, "slack")
        with (
            patch("providers.slack.SlackProvider.exchange_code"),
            patch("providers.slack.SlackProvider.list_destinations", return_value=[]),
        ):
            response = authenticated_client.get(url, {"code": "abc", "state": state}, follow=True)
        assert "/invite" in response.content.decode()

    def test_every_picked_channel_keeps_the_workspace_bot_token(self, authenticated_client, workspace):
        session = authenticated_client.session
        session["oauth_page_select"] = {
            "workspace_id": str(workspace.id),
            "platform": "slack",
            "user_tokens": {"access_token": "", "refresh_token": None, "expires_in": None},
            "pages": [
                {"id": "C1", "name": "#general", "handle": "Acme", "access_token": "xoxb-1", "page_id": "T1"},
                {"id": "G1", "name": "#secret", "handle": "Acme", "access_token": "xoxb-1", "page_id": "T1"},
            ],
        }
        session.save()
        authenticated_client.post(reverse("social_accounts:select_account"), {"selected_pages": ["C1", "G1"]})
        accounts = SocialAccount.objects.filter(workspace=workspace, platform="slack")
        assert {a.account_platform_id for a in accounts} == {"C1", "G1"}
        assert {a.oauth_access_token for a in accounts} == {"xoxb-1"}
        assert all(a.token_expires_at is None for a in accounts)

    def test_publish_credentials_carry_the_channel(self, workspace):
        from apps.publisher.engine import _resolve_publish_credentials

        account = SocialAccount.objects.create(
            workspace=workspace, platform="slack", account_platform_id="C1", account_name="#general"
        )
        assert _resolve_publish_credentials(account)["channel_id"] == "C1"

    def test_disconnecting_one_channel_does_not_revoke_the_shared_token(self, authenticated_client, workspace):
        account = SocialAccount.objects.create(
            workspace=workspace,
            platform="slack",
            account_platform_id="C1",
            account_name="#general",
            oauth_access_token="xoxb-1",
        )
        assert account.revokes_platform_grant_on_disconnect is False
        with patch("providers.slack.SlackProvider._request", side_effect=AssertionError("no request")):
            authenticated_client.post(
                reverse("social_accounts:disconnect", kwargs={"workspace_id": workspace.id, "account_id": account.id})
            )
        assert not SocialAccount.objects.filter(pk=account.pk).exists()
