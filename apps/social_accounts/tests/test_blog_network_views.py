"""Connecting Medium, WordPress, Hashnode and Listmonk: the generic credentials form and what it stores."""

from unittest.mock import patch

import pytest
from django.urls import reverse

from apps.social_accounts.models import SocialAccount
from providers.exceptions import PublishError
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


def connect_page(workspace):
    return reverse("social_accounts:connect", kwargs={"workspace_id": workspace.id})


def form_url(platform, workspace):
    return reverse(f"social_accounts:connect_{platform}", kwargs={"workspace_id": workspace.id})


WP_FORM = {"site_url": "blog.example", "username": "ann", "app_password": "abcd efgh"}


def wp_profile():
    return AccountProfile(
        platform_id="blog.example:7",
        name="Ann's Blog",
        handle="ann",
        extra={"account_data": {"username": "ann"}, "can_publish": True},
    )


@pytest.mark.django_db
class TestConnectPage:
    @pytest.mark.parametrize("platform", ["medium", "wordpress", "hashnode", "listmonk"])
    def test_offered_without_any_platform_app(self, authenticated_client, workspace, platform):
        body = authenticated_client.get(connect_page(workspace)).content.decode()
        assert f'value="{platform}"' in body

    @pytest.mark.parametrize("platform", ["medium", "wordpress", "hashnode", "listmonk"])
    def test_connect_button_goes_to_the_form_not_oauth(self, authenticated_client, workspace, platform):
        response = authenticated_client.post(connect_page(workspace), {"platform": platform})
        assert response.status_code == 302
        assert response.url == form_url(platform, workspace)

    def test_the_form_lists_the_fields_and_hides_secrets(self, authenticated_client, workspace):
        body = authenticated_client.get(form_url("wordpress", workspace)).content.decode()
        assert 'name="site_url"' in body
        assert 'name="username"' in body
        assert 'type="password" id="app_password"' in body

    def test_medium_form_says_new_tokens_cannot_be_created(self, authenticated_client, workspace):
        body = authenticated_client.get(form_url("medium", workspace)).content.decode()
        assert "no longer issues new integration tokens" in body

    def test_needs_the_manage_permission(self, client, user, organization, workspace):
        from apps.members.models import OrgMembership, WorkspaceMembership

        OrgMembership.objects.create(user=user, organization=organization, org_role="member")
        WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role="viewer")
        client.force_login(user)
        assert client.get(form_url("medium", workspace)).status_code in (302, 403)


@pytest.mark.django_db
class TestConnectWordPress:
    def test_connecting_stores_the_secret_encrypted_the_url_and_the_username(self, authenticated_client, workspace):
        with (
            patch("apps.social_accounts.views._is_safe_url", return_value=True),
            patch(
                "providers.wordpress.WordPressProvider.connect_with_form", return_value=(wp_profile(), "abcd efgh")
            ) as connect,
        ):
            response = authenticated_client.post(form_url("wordpress", workspace), WP_FORM)
        assert response.status_code == 302
        assert connect.call_args.args[0]["site_url"] == "https://blog.example"
        account = SocialAccount.objects.get(workspace=workspace, platform="wordpress")
        assert account.instance_url == "https://blog.example"
        assert account.platform_data == {"username": "ann"}
        assert account.oauth_access_token == "abcd efgh"
        assert account.connection_status == SocialAccount.ConnectionStatus.CONNECTED

    def test_the_secret_is_encrypted_at_rest(self, authenticated_client, workspace):
        from django.db import connection

        with (
            patch("apps.social_accounts.views._is_safe_url", return_value=True),
            patch("providers.wordpress.WordPressProvider.connect_with_form", return_value=(wp_profile(), "abcd efgh")),
        ):
            authenticated_client.post(form_url("wordpress", workspace), WP_FORM)
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT oauth_access_token FROM {SocialAccount._meta.db_table} WHERE platform = 'wordpress'"
            )
            stored = cursor.fetchone()[0]
        assert "abcd efgh" not in stored

    @pytest.mark.parametrize("address", ["http://blog.example", "localhost", "https://user:pw@blog.example"])
    def test_non_https_addresses_are_refused(self, authenticated_client, workspace, address):
        with patch("providers.wordpress.WordPressProvider.connect_with_form") as connect:
            response = authenticated_client.post(form_url("wordpress", workspace), {**WP_FORM, "site_url": address})
        connect.assert_not_called()
        assert response.status_code == 200
        assert not SocialAccount.objects.filter(platform="wordpress").exists()

    def test_private_addresses_are_refused(self, authenticated_client, workspace):
        with (
            patch("apps.social_accounts.views._is_safe_url", return_value=False),
            patch("providers.wordpress.WordPressProvider.connect_with_form") as connect,
        ):
            response = authenticated_client.post(form_url("wordpress", workspace), WP_FORM)
        connect.assert_not_called()
        assert "Private or reserved addresses are not allowed" in response.content.decode()

    def test_the_password_is_not_echoed_back_on_an_error(self, authenticated_client, workspace):
        with (
            patch("apps.social_accounts.views._is_safe_url", return_value=True),
            patch(
                "providers.wordpress.WordPressProvider.connect_with_form",
                side_effect=PublishError("WordPress rejected the login."),
            ),
        ):
            response = authenticated_client.post(form_url("wordpress", workspace), WP_FORM)
        body = response.content.decode()
        assert "WordPress rejected the login." in body
        assert "abcd efgh" not in body
        assert 'value="https://blog.example"' in body
        assert not SocialAccount.objects.filter(platform="wordpress").exists()

    def test_a_blank_field_is_refused(self, authenticated_client, workspace):
        response = authenticated_client.post(form_url("wordpress", workspace), {**WP_FORM, "username": ""})
        assert "Fill in every WordPress field." in response.content.decode()


@pytest.mark.django_db
class TestConnectTokenOnly:
    def test_medium_stores_the_token(self, authenticated_client, workspace):
        profile = AccountProfile(platform_id="u1", name="Ann", handle="ann")
        with patch("providers.medium.MediumProvider.get_profile", return_value=profile):
            response = authenticated_client.post(form_url("medium", workspace), {"token": "tok-1"})
        assert response.status_code == 302
        account = SocialAccount.objects.get(workspace=workspace, platform="medium")
        assert account.oauth_access_token == "tok-1"
        assert account.instance_url == ""

    def test_listmonk_keeps_the_address_and_username(self, authenticated_client, workspace):
        profile = AccountProfile(
            platform_id="news.example:api",
            name="news.example",
            handle="api",
            extra={"account_data": {"username": "api"}},
        )
        with (
            patch("apps.social_accounts.views._is_safe_url", return_value=True),
            patch("providers.listmonk.ListmonkProvider.connect_with_form", return_value=(profile, "tok")),
        ):
            authenticated_client.post(
                form_url("listmonk", workspace), {"site_url": "news.example", "username": "api", "token": "tok"}
            )
        account = SocialAccount.objects.get(workspace=workspace, platform="listmonk")
        assert account.instance_url == "https://news.example"
        assert account.platform_data == {"username": "api"}


@pytest.mark.django_db
class TestPublishCredentials:
    def make_account(self, workspace, **extra):
        return SocialAccount.objects.create(
            workspace=workspace,
            platform="wordpress",
            account_platform_id="blog.example:7",
            account_name="Ann's Blog",
            instance_url="https://blog.example",
            platform_data={"username": "ann"},
            oauth_access_token="pw",
            **extra,
        )

    def test_the_provider_gets_the_site_and_username(self, workspace):
        from apps.publisher.engine import _resolve_publish_credentials

        with patch("apps.common.validators.is_safe_url", return_value=True):
            credentials = _resolve_publish_credentials(self.make_account(workspace))
        assert credentials["site_url"] == "https://blog.example"
        assert credentials["username"] == "ann"

    def test_an_address_that_now_fails_the_ssrf_check_is_withheld(self, workspace):
        from apps.publisher.engine import _resolve_publish_credentials

        with patch("apps.common.validators.is_safe_url", return_value=False):
            credentials = _resolve_publish_credentials(self.make_account(workspace))
        assert "site_url" not in credentials

    def test_setting_options_load_through_the_composer_endpoint(self, authenticated_client, workspace):
        account = self.make_account(workspace)
        url = reverse(
            "composer:post_setting_options",
            kwargs={"workspace_id": workspace.id, "account_id": account.id, "key": "category"},
        )
        with (
            patch("apps.common.validators.is_safe_url", return_value=True),
            patch(
                "providers.wordpress.WordPressProvider.get_setting_options", return_value=[{"id": "3", "name": "News"}]
            ),
        ):
            response = authenticated_client.get(url)
        assert response.json() == {"options": [{"id": "3", "name": "News"}]}
