"""Public profile addresses and the grid-preview flag providers declare."""

from types import SimpleNamespace

import pytest

from providers import PROVIDER_REGISTRY


def account(handle):
    return SimpleNamespace(account_handle=handle)


@pytest.mark.parametrize(
    ("platform", "handle", "expected"),
    [
        ("instagram", "acme.coffee", "https://www.instagram.com/acme.coffee/"),
        ("instagram_login", "@acme", "https://www.instagram.com/acme/"),
        ("x", "acme", "https://x.com/acme"),
        ("threads", "acme", "https://www.threads.net/@acme"),
        ("bluesky", "acme.bsky.social", "https://bsky.app/profile/acme.bsky.social"),
    ],
)
def test_profile_url(platform, handle, expected):
    assert PROVIDER_REGISTRY[platform].profile_url(account(handle)) == expected


@pytest.mark.parametrize("handle", ["", "  ", "a b", 'x"><script>', "a/b", "../x", "a" * 101])
def test_a_handle_that_is_not_plain_gives_no_address(handle):
    assert PROVIDER_REGISTRY["instagram"].profile_url(account(handle)) == ""


def test_platforms_without_a_public_profile_give_none():
    assert PROVIDER_REGISTRY["telegram"].profile_url(account("chat")) == ""


def test_only_instagram_providers_preview_a_grid():
    assert {name for name, cls in PROVIDER_REGISTRY.items() if cls.supports_grid_preview} == {
        "instagram",
        "instagram_login",
    }
