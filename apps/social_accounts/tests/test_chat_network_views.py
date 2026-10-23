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
