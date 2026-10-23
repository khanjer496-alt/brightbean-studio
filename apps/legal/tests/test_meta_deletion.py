import base64
import hashlib
import hmac
import json
import time

import pytest
from django.test import override_settings
from django.urls import reverse

from apps.legal.models import DataDeletionRequest
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

SECRET = "meta-app-secret"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def signed_request(payload, secret=SECRET):
    encoded = _b64(json.dumps(payload).encode())
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return f"{_b64(signature)}.{encoded}"


def payload(user_id="1234", issued_at=None, **extra):
    return {"algorithm": "HMAC-SHA256", "issued_at": issued_at or int(time.time()), "user_id": user_id, **extra}


PLATFORM_ENV = {"facebook": {"client_id": "app", "client_secret": SECRET}}


@pytest.fixture(autouse=True)
def meta_secret():
    with override_settings(PLATFORM_CREDENTIALS_FROM_ENV=PLATFORM_ENV):
        yield


@pytest.fixture
def workspace(db, organization):
    return Workspace.objects.create(name="WS", organization=organization)


def make_account(workspace, **kwargs):
    defaults = {
        "platform": "facebook",
        "account_platform_id": "page-1",
        "account_name": "Page",
        "oauth_access_token": "tok",
        "connection_status": SocialAccount.ConnectionStatus.CONNECTED,
    }
    return SocialAccount.objects.create(workspace=workspace, **{**defaults, **kwargs})


def call(client, value):
    return client.post(reverse("legal:meta_data_deletion"), {"signed_request": value})


@pytest.mark.django_db
def test_valid_request_deletes_accounts_and_returns_code_and_url(client, workspace):
    mine = make_account(workspace, meta_user_id="1234")
    other = make_account(workspace, account_platform_id="page-2", meta_user_id="999")
    response = call(client, signed_request(payload()))
    assert response.status_code == 200
    assert response["Content-Type"] == "application/json"
    body = response.json()
    assert set(body) == {"url", "confirmation_code"}
    assert body["confirmation_code"].isalnum()
    assert body["url"].endswith(reverse("legal:data_deletion_status", kwargs={"code": body["confirmation_code"]}))
    assert not SocialAccount.objects.filter(pk=mine.pk).exists()
    assert SocialAccount.objects.filter(pk=other.pk).exists()


@pytest.mark.django_db
def test_instagram_login_account_matches_on_platform_id(client, workspace):
    account = make_account(workspace, platform="instagram_login", account_platform_id="1234")
    assert call(client, signed_request(payload())).status_code == 200
    assert not SocialAccount.objects.filter(pk=account.pk).exists()


@pytest.mark.django_db
def test_grant_is_revoked_before_deleting(client, workspace, monkeypatch):
    calls = []
    monkeypatch.setattr("apps.social_accounts.removal.revoke_grant", lambda account: calls.append(account.pk))
    monkeypatch.setattr("apps.social_accounts.removal.unsubscribe_account_webhooks", lambda account: None)
    account = make_account(workspace, meta_user_id="1234")
    call(client, signed_request(payload()))
    assert calls == [account.pk]


@pytest.mark.django_db
def test_wrong_signature_is_rejected_and_nothing_is_deleted(client, workspace):
    account = make_account(workspace, meta_user_id="1234")
    response = call(client, signed_request(payload(), secret="not-the-secret"))
    assert response.status_code == 400
    assert SocialAccount.objects.filter(pk=account.pk).exists()
    assert DataDeletionRequest.objects.count() == 0


@pytest.mark.django_db
def test_tampered_payload_is_rejected(client, workspace):
    good = signed_request(payload(user_id="1234"))
    signature = good.split(".")[0]
    forged = f"{signature}.{_b64(json.dumps(payload(user_id='5678')).encode())}"
    assert call(client, forged).status_code == 400


@pytest.mark.django_db
@pytest.mark.parametrize("value", ["", "nodot", "a.b", "!!!.???"])
def test_malformed_requests_are_rejected(client, value):
    assert call(client, value).status_code == 400


@pytest.mark.django_db
def test_wrong_algorithm_is_rejected(client):
    assert call(client, signed_request({**payload(), "algorithm": "HMAC-SHA1"})).status_code == 400


@pytest.mark.django_db
def test_stale_request_is_rejected(client):
    old = int(time.time()) - 30 * 24 * 3600
    assert call(client, signed_request(payload(issued_at=old))).status_code == 400


@pytest.mark.django_db
def test_get_is_not_allowed(client):
    assert client.get(reverse("legal:meta_data_deletion")).status_code == 405


@pytest.mark.django_db
def test_no_configured_secret_rejects_everything(client):
    with override_settings(PLATFORM_CREDENTIALS_FROM_ENV={}):
        assert call(client, signed_request(payload())).status_code == 400


@pytest.mark.django_db
def test_replay_returns_the_same_code_without_deleting_again(client, workspace):
    make_account(workspace, meta_user_id="1234")
    value = signed_request(payload())
    first = call(client, value).json()
    later = make_account(workspace, account_platform_id="page-9", meta_user_id="1234")
    second = call(client, value).json()
    assert first == second
    assert DataDeletionRequest.objects.count() == 1
    assert SocialAccount.objects.filter(pk=later.pk).exists()


@pytest.mark.django_db
def test_status_page_shows_the_outcome_and_unknown_codes_404(client, workspace):
    make_account(workspace, meta_user_id="1234")
    code = call(client, signed_request(payload())).json()["confirmation_code"]
    page = client.get(reverse("legal:data_deletion_status", kwargs={"code": code}))
    assert page.status_code == 200
    assert code in page.content.decode()
    assert "Completed" in page.content.decode()
    assert client.get(reverse("legal:data_deletion_status", kwargs={"code": "nope"})).status_code == 404


@pytest.mark.django_db
def test_an_organizations_own_app_secret_only_reaches_that_organization(client, workspace):
    from apps.credentials.models import PlatformCredential
    from apps.organizations.models import Organization

    victim = make_account(workspace, platform="instagram_login", account_platform_id="victim")
    tenant = Organization.objects.create(name="Tenant")
    tenant_workspace = Workspace.objects.create(name="T", organization=tenant)
    own = make_account(tenant_workspace, platform="instagram_login", account_platform_id="victim")
    PlatformCredential.objects.create(
        organization=tenant,
        platform="instagram_login",
        credentials={"client_id": "tenant-app", "client_secret": "tenant-secret"},
    )
    response = call(client, signed_request(payload(user_id="victim"), secret="tenant-secret"))
    assert response.status_code == 200
    assert SocialAccount.objects.filter(pk=victim.pk).exists()
    assert not SocialAccount.objects.filter(pk=own.pk).exists()


@pytest.mark.django_db
def test_every_configured_meta_app_secret_is_accepted(client, workspace):
    account = make_account(workspace, platform="threads", account_platform_id="threads-user")
    env = {**PLATFORM_ENV, "threads": {"client_id": "t", "client_secret": "threads-secret"}}
    with override_settings(PLATFORM_CREDENTIALS_FROM_ENV=env):
        response = call(client, signed_request(payload(user_id="threads-user"), secret="threads-secret"))
    assert response.status_code == 200
    assert not SocialAccount.objects.filter(pk=account.pk).exists()


@pytest.mark.django_db
def test_backfill_records_the_user_behind_older_connections(workspace, monkeypatch):
    from django.core.management import call_command

    page = make_account(workspace, account_platform_id="page-9")

    class Provider:
        def debug_token(self, token):
            return {"user_id": "person-9"}

    monkeypatch.setattr("providers.get_provider", lambda platform, credentials: Provider())
    call_command("backfill_meta_user_ids")
    page.refresh_from_db()
    assert page.meta_user_id == ""
    call_command("backfill_meta_user_ids", "--apply")
    page.refresh_from_db()
    assert page.meta_user_id == "person-9"
