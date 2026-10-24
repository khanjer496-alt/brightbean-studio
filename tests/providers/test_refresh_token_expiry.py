"""Platforms that say when their refresh token lapses, and the account keeping it."""

from datetime import timedelta
from unittest.mock import patch

import httpx
import pytest
from django.utils import timezone

from providers.linkedin import LinkedInProvider
from providers.pinterest import PinterestProvider
from providers.tiktok import TikTokProvider

CREDS = {"client_id": "id", "client_secret": "secret", "client_key": "key"}


def answer(body):
    def fake(self, method, url, **kwargs):
        return httpx.Response(200, request=httpx.Request(method, url), json=body)

    return patch.object(httpx.Client, "request", fake)


@pytest.mark.parametrize(
    ("provider_cls", "field"),
    [
        (TikTokProvider, "refresh_expires_in"),
        (PinterestProvider, "refresh_token_expires_in"),
        (LinkedInProvider, "refresh_token_expires_in"),
    ],
)
def test_the_refresh_token_lifetime_is_read_from_both_token_calls(provider_cls, field):
    body = {"access_token": "a", "refresh_token": "r", "expires_in": 3600, field: 5184000}
    provider = provider_cls(CREDS)

    with answer(body):
        exchanged = provider.exchange_code("code", "https://app/cb")
        refreshed = provider.refresh_token("r")

    assert exchanged.refresh_expires_in == 5184000
    assert refreshed.refresh_expires_in == 5184000


@pytest.mark.parametrize("provider_cls", [TikTokProvider, PinterestProvider, LinkedInProvider])
def test_no_lifetime_in_the_response_means_none(provider_cls):
    provider = provider_cls(CREDS)
    with answer({"access_token": "a", "refresh_token": "r", "expires_in": 3600}):
        assert provider.refresh_token("r").refresh_expires_in is None


@pytest.mark.django_db
def test_connecting_stores_when_the_refresh_token_lapses(organization):
    from apps.social_accounts.views import _create_or_update_account
    from apps.workspaces.models import Workspace
    from providers.types import AccountProfile

    workspace = Workspace.objects.create(organization=organization, name="WS")
    account = _create_or_update_account(
        workspace_id=workspace.id,
        platform="tiktok",
        profile=AccountProfile(platform_id="tt-1", name="TT"),
        access_token="a",
        refresh_token="r",
        expires_in=86400,
        refresh_expires_in=365 * 86400,
    )

    assert account.refresh_token_expires_at > timezone.now() + timedelta(days=364)


@pytest.mark.django_db
def test_connecting_without_one_stores_nothing(organization):
    from apps.social_accounts.views import _create_or_update_account
    from apps.workspaces.models import Workspace
    from providers.types import AccountProfile

    workspace = Workspace.objects.create(organization=organization, name="WS")
    account = _create_or_update_account(
        workspace_id=workspace.id,
        platform="tiktok",
        profile=AccountProfile(platform_id="tt-2", name="TT"),
        access_token="a",
    )

    assert account.refresh_token_expires_at is None
