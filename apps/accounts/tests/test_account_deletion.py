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


def _paid(org):
    Subscription.objects.update_or_create(
        organization=org,
        defaults={"status": Subscription.Status.ACTIVE, "stripe_subscription_id": "sub_1", "plan": "team"},
    )


@pytest.mark.django_db
def test_paid_subscription_is_cancelled_then_the_account_deleted(client, revoked, monkeypatch):
    user = make_user()
    org = own_org(user)
    _paid(org)
    cancelled = []
    monkeypatch.setattr("apps.billing.services.cancel_subscription_now", lambda o: cancelled.append(o.pk))
    client.force_login(user)
    assert "cancelled immediately" in client.get(reverse(URL)).content.decode()
    assert post_delete(client).status_code == 302
    assert cancelled == [org.pk]
    assert not User.objects.filter(pk=user.pk).exists()


@pytest.mark.django_db
def test_failed_cancellation_deletes_nothing(client, revoked, monkeypatch):
    user = make_user()
    org = own_org(user)
    _paid(org)

    def stripe_down(o):
        raise RuntimeError("stripe down")

    monkeypatch.setattr("apps.billing.services.cancel_subscription_now", stripe_down)
    client.force_login(user)
    response = post_delete(client)
    assert response.status_code == 200
    assert "nothing was deleted" in response.content.decode()
    assert User.objects.filter(pk=user.pk).exists()
    assert Organization.objects.filter(pk=org.pk).exists()


@pytest.mark.django_db
def test_uploaded_files_and_avatar_are_removed_from_storage(
    client, revoked, tmp_path, settings, django_capture_on_commit_callbacks
):
    from django.core.files.base import ContentFile

    from apps.media_library.models import MediaAsset

    settings.MEDIA_ROOT = str(tmp_path)
    user = make_user()
    org = own_org(user)
    user.avatar.save("me.png", ContentFile(b"png"), save=True)
    asset = MediaAsset(organization=org, filename="a.jpg", media_type="image", file_size=3, mime_type="image/jpeg")
    asset.file.save("a.jpg", ContentFile(b"jpg"), save=False)
    asset.save()
    files = [tmp_path / user.avatar.name, tmp_path / asset.file.name]
    assert all(f.exists() for f in files)
    client.force_login(user)
    with django_capture_on_commit_callbacks(execute=True):
        assert post_delete(client).status_code == 302
    assert not any(f.exists() for f in files)


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
def test_user_without_password_confirms_through_an_emailed_link(client, mailoutbox):
    user = User.objects.create_user(email="social@example.com", tos_accepted_at=timezone.now())
    user.set_unusable_password()
    user.save()
    client.force_login(user)
    response = client.post(reverse(URL), {"confirmation": deletion.CONFIRMATION_PHRASE, "email": user.email})
    assert response.status_code == 200
    assert "sent a confirmation link" in response.content.decode()
    assert User.objects.filter(pk=user.pk).exists()
    assert len(mailoutbox) == 1 and mailoutbox[0].to == [user.email]
    link = next(line for line in mailoutbox[0].body.splitlines() if "/settings/delete/confirm/" in line).strip()
    path = link[link.index("/accounts/") :]

    # Opening the link (or a mail scanner prefetching it) deletes nothing.
    assert client.get(path).status_code == 200
    assert User.objects.filter(pk=user.pk).exists()
    assert client.post(path).status_code == 302
    assert not User.objects.filter(pk=user.pk).exists()


@pytest.mark.django_db
def test_emailed_link_only_works_for_its_own_account(client):
    owner = User.objects.create_user(email="social@example.com", tos_accepted_at=timezone.now())
    other = make_user("other@example.com")
    path = reverse("accounts:confirm_delete_account", kwargs={"token": deletion.email_link_token(owner)})
    client.force_login(other)
    assert client.post(path).status_code == 302
    assert User.objects.filter(pk=other.pk).exists()
    assert not deletion.email_link_is_valid(owner, "tampered")


@pytest.mark.django_db
def test_emailed_link_expires(client, monkeypatch):
    user = User.objects.create_user(email="social@example.com", tos_accepted_at=timezone.now())
    token = deletion.email_link_token(user)
    monkeypatch.setattr(deletion, "EMAIL_LINK_MAX_AGE_SECONDS", -1)
    assert not deletion.email_link_is_valid(user, token)


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
