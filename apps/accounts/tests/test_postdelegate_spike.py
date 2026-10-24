"""PostDelegate deployment controls layered on the BrightBean codebase."""

from datetime import timedelta

import pytest
from allauth.socialaccount.models import SocialAccount as AllAuthSocialAccount
from allauth.socialaccount.models import SocialLogin
from django.test import RequestFactory, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.adapters import AccountAdapter, SocialAccountAdapter
from apps.accounts.models import User
from apps.members.models import Invitation
from apps.organizations.models import Organization


def _request(token=None):
    request = RequestFactory().get("/accounts/signup/")
    request.session = {}
    if token:
        request.session["pending_invite_token"] = token
    return request


@pytest.mark.django_db
@override_settings(SIGNUP_MODE="invite")
def test_public_email_signup_is_closed():
    assert AccountAdapter().is_open_for_signup(_request()) is False


@pytest.mark.django_db
@override_settings(SIGNUP_MODE="invite")
def test_public_social_signup_is_closed():
    sociallogin = SocialLogin(
        user=User(email="new@example.com"),
        account=AllAuthSocialAccount(provider="google", uid="new-google-user"),
    )
    assert SocialAccountAdapter().is_open_for_signup(_request(), sociallogin) is False


@pytest.mark.django_db
@override_settings(SIGNUP_MODE="invite")
def test_valid_invitation_allows_signup():
    inviter = User.objects.create(email="owner@example.com")
    org = Organization.objects.create(name="PostDelegate Test")
    invitation = Invitation.objects.create(
        organization=org,
        email="invitee@example.com",
        invited_by=inviter,
        expires_at=timezone.now() + timedelta(days=1),
    )
    assert AccountAdapter().is_open_for_signup(_request(invitation.token)) is True


@pytest.mark.django_db
@override_settings(SIGNUP_MODE="invite")
def test_expired_invitation_does_not_open_signup():
    inviter = User.objects.create(email="owner2@example.com")
    org = Organization.objects.create(name="PostDelegate Test 2")
    invitation = Invitation.objects.create(
        organization=org,
        email="expired@example.com",
        invited_by=inviter,
        expires_at=timezone.now() - timedelta(seconds=1),
    )
    assert AccountAdapter().is_open_for_signup(_request(invitation.token)) is False


@override_settings(MCP_SERVER_NAME="postdelegate")
def test_mcp_initialize_uses_deployment_identity():
    from apps.mcp.transport import _initialize

    result = _initialize({}, {})
    assert result["serverInfo"]["name"] == "postdelegate"


@pytest.mark.django_db
@override_settings(
    BRAND_NAME="PostDelegate",
    BRAND_SHORT_NAME="PostDelegate",
    BRAND_LEGAL_NAME="Nasida Apps LLC",
    BRAND_WEBSITE_URL="https://postdelegate.com",
    BRAND_TERMS_URL="https://postdelegate.com/terms",
    BRAND_PRIVACY_URL="https://postdelegate.com/privacy",
    BRAND_SUPPORT_EMAIL="support@postdelegate.com",
    BRAND_LOGO_STATIC="img/postdelegate-mark.svg",
    BRAND_FAVICON_STATIC="favicon/postdelegate.svg",
    BRAND_FAVICON_ICO_STATIC="favicon/postdelegate.ico",
    BRAND_APPLE_TOUCH_ICON_STATIC="favicon/postdelegate-apple-touch-icon.png",
)
def test_login_surface_renders_postdelegate_brand(client):
    response = client.get(reverse("account_login"))
    assert response.status_code == 200
    html = response.content.decode("utf-8")
    assert "PostDelegate" in html
    # The only upstream name left is the AGPL credit.
    assert (
        html.count("BrightBean")
        == html.count('BrightBean Studio<span class="sr-only"> (opens in a new tab)</span></a> (AGPL-3.0)')
        == 1
    )
    assert "Brightbean" not in html
    assert "postdelegate-mark.svg" in html


@pytest.mark.django_db
@override_settings(SOURCE_CODE_URL="https://github.com/khanjer496-alt/brightbean-studio")
def test_public_pages_credit_upstream_and_link_the_source(client):
    html = client.get(reverse("account_login")).content.decode("utf-8")

    assert 'href="https://github.com/brightbeanxyz/brightbean-studio"' in html
    assert 'href="https://github.com/khanjer496-alt/brightbean-studio"' in html
    assert "(AGPL-3.0)" in html


@pytest.mark.django_db
def test_signed_in_pages_keep_the_source_credit(client):
    user = User.objects.create_user(email="credit@example.com", password="pw", tos_accepted_at=timezone.now())
    client.force_login(user)

    html = client.get(reverse("accounts:settings")).content.decode("utf-8")

    assert "(AGPL-3.0)" in html
    assert 'href="https://github.com/brightbeanxyz/brightbean-studio"' in html


@pytest.mark.django_db
@override_settings(API_TOKEN_PREFIX="postdelegate_")
def test_api_keys_use_postdelegate_prefix():
    from apps.api_keys.services import issue_api_key, parse_token
    from apps.members.models import WorkspaceMembership
    from apps.social_accounts.models import SocialAccount

    user = User.objects.create(email="agent-owner@example.com")
    membership = WorkspaceMembership.objects.select_related("workspace").get(user=user)
    social_account = SocialAccount.objects.create(
        workspace=membership.workspace,
        platform="linkedin_personal",
        account_platform_id="li-prefix-test",
        account_name="Prefix Test",
    )
    issued = issue_api_key(
        workspace=membership.workspace,
        social_accounts=[social_account],
        issued_by=membership.user,
        name="postdelegate-test",
        permissions=["create_posts"],
    )
    assert issued.plaintext_token.startswith("postdelegate_")
    assert parse_token(issued.plaintext_token) is not None


@pytest.mark.django_db
@override_settings(SITE_DOMAIN="app.postdelegate.com", SITE_NAME="PostDelegate")
def test_post_migrate_site_identity_uses_deployment_brand():
    from django.contrib.sites.models import Site

    from apps.accounts.apps import AccountsConfig

    AccountsConfig._sync_site_identity(None)
    site = Site.objects.get(id=1)
    assert site.domain == "app.postdelegate.com"
    assert site.name == "PostDelegate"


@pytest.mark.django_db
@override_settings(SITE_DOMAIN="app.postdelegate.com", SITE_NAME="PostDelegate")
def test_site_identity_migration_replaces_the_upstream_site():
    import importlib

    from django.apps import apps
    from django.contrib.sites.models import Site

    Site.objects.update_or_create(id=1, defaults={"domain": "studio.brightbean.xyz", "name": "Brightbean"})
    migration = importlib.import_module("apps.accounts.migrations.0007_sync_site_identity")

    migration.set_site(apps, None)

    site = Site.objects.get(id=1)
    assert site.domain == "app.postdelegate.com"
    assert site.name == "PostDelegate"
