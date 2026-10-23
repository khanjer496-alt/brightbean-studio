import pytest
from allauth.mfa.models import Authenticator
from allauth.mfa.totp.internal.auth import (
    TOTP,
    format_hotp_value,
    generate_totp_secret,
    hotp_value,
    yield_hotp_counters_from_time,
)
from django.urls import reverse
from django.utils import timezone

from apps.accounts import deletion
from apps.accounts.models import User
from apps.billing.models import Subscription
from apps.members.models import OrgMembership
from apps.organizations.models import Organization
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

PASSWORD = "S3cure-pass-for-tests!"
URL = "accounts:delete_account"


def make_user(email="gone@example.com", **kwargs):
    return User.objects.create_user(email=email, password=PASSWORD, tos_accepted_at=timezone.now(), **kwargs)


def own_org(user):
    return OrgMembership.objects.get(user=user).organization


def post_delete(client, **overrides):
    data = {"password": PASSWORD, "confirmation": deletion.CONFIRMATION_PHRASE, **overrides}
    return client.post(reverse(URL), data)


@pytest.fixture
def revoked(monkeypatch):
    calls = []
    monkeypatch.setattr("apps.social_accounts.removal.revoke_grant", lambda account: calls.append(account.platform))
    monkeypatch.setattr("apps.social_accounts.removal.unsubscribe_account_webhooks", lambda account: None)
    return calls


@pytest.mark.django_db
def test_requires_login(client):
    assert client.get(reverse(URL)).status_code == 302
    assert client.post(reverse(URL), {}).status_code == 302
    assert User.objects.count() == 0


@pytest.mark.django_db
def test_wrong_password_deletes_nothing(client):
    user = make_user()
    client.force_login(user)
    response = post_delete(client, password="wrong")
    assert response.status_code == 200
    assert "password is incorrect" in response.content.decode()
    assert User.objects.filter(pk=user.pk).exists()


@pytest.mark.django_db
def test_wrong_confirmation_phrase_deletes_nothing(client):
    user = make_user()
    client.force_login(user)
    response = post_delete(client, confirmation="delete")
    assert "exactly" in response.content.decode()
    assert User.objects.filter(pk=user.pk).exists()


@pytest.mark.django_db
def test_deletes_user_solo_org_and_tokens_and_signs_out(client, revoked):
    user = make_user()
    org = own_org(user)
    workspace = Workspace.objects.filter(organization=org).first()
    SocialAccount.objects.create(
        workspace=workspace,
        platform="facebook",
        account_platform_id="p1",
        account_name="P",
        oauth_access_token="tok",
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )
    client.force_login(user)
    response = post_delete(client)
    assert response.status_code == 302
    assert response.url == reverse("account_login")
    assert not User.objects.filter(pk=user.pk).exists()
    assert not Organization.objects.filter(pk=org.pk).exists()
    assert not SocialAccount.objects.exists()
    assert revoked == ["facebook"]
    assert client.get(reverse("accounts:settings")).status_code == 302
    assert "_auth_user_id" not in client.session


@pytest.mark.django_db
def test_revocation_failure_does_not_block_deletion(client, monkeypatch):
    user = make_user()
    workspace = Workspace.objects.filter(organization=own_org(user)).first()
    SocialAccount.objects.create(
        workspace=workspace, platform="facebook", account_platform_id="p1", account_name="P", oauth_access_token="t"
    )
    monkeypatch.setattr("apps.social_accounts.removal.unsubscribe_account_webhooks", lambda account: None)

    def boom(*args, **kwargs):
        raise RuntimeError("platform down")

    monkeypatch.setattr("providers.get_provider", boom)
    client.force_login(user)
    assert post_delete(client).status_code == 302
    assert not User.objects.filter(pk=user.pk).exists()


@pytest.mark.django_db
def test_shared_org_content_stays_and_loses_its_author(client, revoked):
    owner = make_user("owner@example.com")
    member = make_user("member@example.com")
    org = own_org(owner)
    OrgMembership.objects.filter(user=member).delete()
    OrgMembership.objects.create(user=member, organization=org, org_role=OrgMembership.OrgRole.MEMBER)
    workspace = Workspace.objects.filter(organization=org).first()
    account = SocialAccount.objects.create(
        workspace=workspace, platform="facebook", account_platform_id="p1", account_name="P", oauth_access_token="t"
    )
    client.force_login(member)
    assert post_delete(client).status_code == 302
    assert not User.objects.filter(pk=member.pk).exists()
    assert Organization.objects.filter(pk=org.pk).exists()
    assert SocialAccount.objects.filter(pk=account.pk).exists()
    assert revoked == []


@pytest.mark.django_db
def test_sole_owner_of_org_with_members_is_blocked(client):
    owner = make_user("owner@example.com")
    member = make_user("member@example.com")
    org = own_org(owner)
    OrgMembership.objects.create(user=member, organization=org, org_role=OrgMembership.OrgRole.MEMBER)
    client.force_login(owner)
    page = client.get(reverse(URL))
    assert "only owner" in page.content.decode()
    post_delete(client)
    assert User.objects.filter(pk=owner.pk).exists()


@pytest.mark.django_db
def test_active_paid_subscription_blocks_deletion(client):
    user = make_user()
    org = own_org(user)
    Subscription.objects.update_or_create(
        organization=org,
        defaults={"status": Subscription.Status.ACTIVE, "stripe_subscription_id": "sub_1", "plan": "team"},
    )
    client.force_login(user)
    post_delete(client)
    assert User.objects.filter(pk=user.pk).exists()
    assert "paid subscription" in client.get(reverse(URL)).content.decode()


@pytest.mark.django_db
def test_two_factor_code_is_required_when_enabled(client, revoked):
    user = make_user()
    secret = generate_totp_secret()
    TOTP.activate(user, secret)
    client.force_login(user)
    assert "authenticator" in post_delete(client).content.decode()
    assert User.objects.filter(pk=user.pk).exists()
    code = format_hotp_value(hotp_value(secret, next(yield_hotp_counters_from_time())))
    assert Authenticator.objects.filter(user=user).exists()
    assert post_delete(client, code=code).status_code == 302
    assert not User.objects.filter(pk=user.pk).exists()


@pytest.mark.django_db
def test_user_without_password_must_type_their_email(client):
    user = User.objects.create_user(email="social@example.com", tos_accepted_at=timezone.now())
    user.set_unusable_password()
    user.save()
    client.force_login(user)
    data = {"confirmation": deletion.CONFIRMATION_PHRASE}
    assert client.post(reverse(URL), {**data, "email": "other@example.com"}).status_code == 200
    assert User.objects.filter(pk=user.pk).exists()
    assert client.post(reverse(URL), {**data, "email": "SOCIAL@example.com"}).status_code == 302
    assert not User.objects.filter(pk=user.pk).exists()


@pytest.mark.django_db
def test_youtube_channel_authorised_by_user_is_purged(client, revoked):
    owner = make_user("owner@example.com")
    member = make_user("member@example.com")
    org = own_org(owner)
    OrgMembership.objects.create(user=member, organization=org, org_role=OrgMembership.OrgRole.MEMBER)
    workspace = Workspace.objects.filter(organization=org).first()
    channel = SocialAccount.objects.create(
        workspace=workspace,
        platform="youtube",
        account_platform_id="UC1",
        account_name="Chan",
        oauth_access_token="t",
        youtube_authorized_by=member,
    )
    client.force_login(member)
    assert post_delete(client).status_code == 302
    assert not SocialAccount.objects.filter(pk=channel.pk).exists()


@pytest.mark.django_db
def test_settings_page_links_to_deletion_flow(client):
    user = make_user()
    client.force_login(user)
    html = client.get(reverse("accounts:settings")).content.decode()
    assert reverse(URL) in html
    assert "delete_account" not in html.replace(reverse(URL), "")
