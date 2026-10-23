"""Public profile metrics for competitor benchmarking: each supported network against mocked HTTP,
and the networks that stay "not available" because they offer no official route."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from providers import PROVIDER_REGISTRY, get_provider
from providers.exceptions import PublicProfileNotFoundError, RateLimitError, TokenExpiredError

NOW = datetime(2026, 10, 24, 12, tzinfo=UTC)


def _iso(days_ago: int) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z")


@contextmanager
def mock_http(routes):
    """Patch httpx.Client so a request is answered by the first route whose substring is in the URL.

    A route is ``(status, body)``; the list of ``(method, url, params)`` calls made is yielded.
    """
    calls = []

    class Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def request(self, method, url, headers=None, params=None, **kw):
            calls.append((method, url, params or {}, headers or {}))
            for needle, (status, body) in routes.items():
                if needle in url:
                    return httpx.Response(status, json=body, request=httpx.Request(method, url))
            raise AssertionError(f"unexpected request {method} {url}")

    with patch("providers.base.httpx.Client", Client):
        yield calls


ACCOUNT = SimpleNamespace(account_platform_id="17841400000000000", account_name="Mine")


class TestBluesky:
    def test_profile_and_feed(self):
        feed = {
            "feed": [
                {
                    "post": {
                        "likeCount": 10,
                        "repostCount": 2,
                        "replyCount": 1,
                        "quoteCount": 1,
                        "record": {"createdAt": _iso(1)},
                    }
                },
                {"post": {"likeCount": 5, "repostCount": 0, "replyCount": 0, "record": {"createdAt": _iso(7)}}},
                # A repost of someone else's post is not this account's own.
                {"reason": {"$type": "reason.repost"}, "post": {"likeCount": 999, "record": {"createdAt": _iso(2)}}},
            ]
        }
        routes = {
            "getProfile": (
                200,
                {"handle": "acme.bsky.social", "displayName": "Acme", "followersCount": 1500, "postsCount": 240},
            ),
            "getAuthorFeed": (200, feed),
        }
        provider = get_provider("bluesky", {})
        with mock_http(routes) as calls:
            m = provider.public_profile_metrics("acme.bsky.social", ACCOUNT, "")
        assert m.followers == 1500 and m.posts_count == 240 and m.display_name == "Acme"
        assert m.sampled_posts == 2 and m.avg_engagement == 9.5
        assert m.posts_per_week is not None and m.posts_per_week > 0
        assert all(url.startswith("https://public.api.bsky.app/") for _, url, _, _ in calls)
        assert not any("Authorization" in h for *_, h in calls)

    def test_unknown_handle_is_not_found(self):
        with mock_http({"getProfile": (400, {"error": "InvalidRequest"})}), pytest.raises(PublicProfileNotFoundError):
            get_provider("bluesky", {}).public_profile_metrics("nobody.example", ACCOUNT, "")

    def test_feed_failure_keeps_the_profile_numbers(self):
        routes = {"getProfile": (200, {"handle": "a.b", "followersCount": 3}), "getAuthorFeed": (500, {})}
        with mock_http(routes):
            m = get_provider("bluesky", {}).public_profile_metrics("a.b", ACCOUNT, "")
        assert m.followers == 3 and m.avg_engagement is None and m.posts_per_week is None


class TestMastodon:
    STATUSES = [
        {"favourites_count": 4, "reblogs_count": 1, "replies_count": 1, "created_at": _iso(2)},
        {"favourites_count": 2, "reblogs_count": 0, "replies_count": 0, "created_at": _iso(9)},
    ]

    def test_local_account_uses_lookup(self):
        routes = {
            "/accounts/lookup": (
                200,
                {
                    "id": "7",
                    "acct": "acme",
                    "display_name": "Acme",
                    "followers_count": 80,
                    "statuses_count": 12,
                    "url": "https://m.example/@acme",
                },
            ),
            "/accounts/7/statuses": (200, self.STATUSES),
        }
        provider = get_provider("mastodon", {"instance_url": "https://m.example"})
        with mock_http(routes) as calls:
            m = provider.public_profile_metrics("acme", ACCOUNT, "tok")
        assert (m.followers, m.posts_count, m.avg_engagement, m.sampled_posts) == (80, 12, 4.0, 2)
        assert calls[0][2] == {"acct": "acme"}
        # Sent even for a local lookup: an instance in authorized-fetch mode refuses anonymous reads.
        assert calls[0][3]["Authorization"] == "Bearer tok"

    def test_remote_account_resolves_through_own_instance(self):
        routes = {
            "/accounts/search": (
                200,
                [{"id": "55", "acct": "bob@other.example", "followers_count": 5, "statuses_count": 1}],
            ),
            "/accounts/55/statuses": (200, []),
        }
        provider = get_provider("mastodon", {"instance_url": "https://m.example"})
        with mock_http(routes) as calls:
            m = provider.public_profile_metrics("bob@other.example", ACCOUNT, "tok")
        assert m.followers == 5 and m.avg_engagement is None
        assert calls[0][1].startswith("https://m.example/") and calls[0][2]["resolve"] == "true"
        assert calls[0][3]["Authorization"] == "Bearer tok"

    def test_missing_account(self):
        provider = get_provider("mastodon", {"instance_url": "https://m.example"})
        with (
            mock_http({"/accounts/lookup": (404, {"error": "Record not found"})}),
            pytest.raises(PublicProfileNotFoundError),
        ):
            provider.public_profile_metrics("ghost", ACCOUNT, "")
        with mock_http({"/accounts/search": (200, [])}), pytest.raises(PublicProfileNotFoundError):
            provider.public_profile_metrics("ghost@other.example", ACCOUNT, "tok")


class TestYouTube:
    def routes(self, stats):
        return {
            "/channels": (
                200,
                {
                    "items": [
                        {
                            "id": "UC" + "a" * 22,
                            "snippet": {"title": "Acme TV", "customUrl": "@acmetv"},
                            "statistics": stats,
                            "contentDetails": {"relatedPlaylists": {"uploads": "UU123"}},
                        }
                    ]
                },
            ),
            "/playlistItems": (
                200,
                {"items": [{"contentDetails": {"videoId": "v1"}}, {"contentDetails": {"videoId": "v2"}}]},
            ),
            "/videos": (
                200,
                {
                    "items": [
                        {"snippet": {"publishedAt": _iso(3)}, "statistics": {"likeCount": "30", "commentCount": "10"}},
                        {"snippet": {"publishedAt": _iso(20)}, "statistics": {"likeCount": "10", "commentCount": "0"}},
                    ]
                },
            ),
        }

    def test_statistics_and_recent_videos(self):
        provider = get_provider("youtube", {})
        with mock_http(self.routes({"subscriberCount": "1200", "videoCount": "88"})) as calls:
            m = provider.public_profile_metrics("acmetv", ACCOUNT, "yt-token")
        assert (m.followers, m.posts_count, m.avg_engagement, m.sampled_posts) == (1200, 88, 25.0, 2)
        assert calls[0][2]["forHandle"] == "@acmetv"
        assert calls[0][3]["Authorization"] == "Bearer yt-token"
        assert provider.last_call_quota_units == 3

    def test_hidden_subscriber_count_is_unknown_not_zero(self):
        with mock_http(self.routes({"hiddenSubscriberCount": True, "subscriberCount": "0", "videoCount": "4"})):
            m = get_provider("youtube", {}).public_profile_metrics("acmetv", ACCOUNT, "t")
        assert m.followers is None and m.posts_count == 4

    def test_channel_id_lookup_and_missing_channel(self):
        provider = get_provider("youtube", {})
        with mock_http(self.routes({"subscriberCount": "1"})) as calls:
            provider.public_profile_metrics("UC" + "a" * 22, ACCOUNT, "t")
        assert calls[0][2]["id"] == "UC" + "a" * 22
        with mock_http({"/channels": (200, {"items": []})}), pytest.raises(PublicProfileNotFoundError):
            provider.public_profile_metrics("nobody", ACCOUNT, "t")


@pytest.mark.parametrize("platform,base", [("instagram", "graph.facebook.com")])
class TestInstagramBusinessDiscovery:
    def test_business_discovery_query(self, platform, base):
        body = {
            "business_discovery": {
                "username": "rival",
                "name": "Rival Co",
                "followers_count": 9000,
                "media_count": 300,
                "media": {
                    "data": [
                        {"like_count": 100, "comments_count": 10, "timestamp": _iso(2).replace("Z", "+0000")},
                        {"like_count": 50, "comments_count": 0, "timestamp": _iso(10).replace("Z", "+0000")},
                    ]
                },
            }
        }
        provider = get_provider(platform, {"ig_user_id": "1784"})
        with mock_http({"/1784": (200, body)}) as calls:
            m = provider.public_profile_metrics("rival", ACCOUNT, "ig-token")
        assert (m.followers, m.posts_count, m.avg_engagement, m.sampled_posts) == (9000, 300, 80.0, 2)
        method, url, params, headers = calls[0]
        assert base in url and url.endswith("/1784")
        assert params["fields"].startswith("business_discovery.username(rival){")
        assert headers["Authorization"] == "Bearer ig-token"

    def test_personal_or_unknown_account_is_not_found(self, platform, base):
        provider = get_provider(platform, {"ig_user_id": "1784"})
        with (
            mock_http({"/1784": (400, {"error": {"code": 110, "message": "Invalid user id"}})}),
            pytest.raises(PublicProfileNotFoundError),
        ):
            provider.public_profile_metrics("personal", ACCOUNT, "t")

    @pytest.mark.parametrize("code,raised", [(4, RateLimitError), (80002, RateLimitError), (190, TokenExpiredError)])
    def test_throttling_and_dead_tokens_are_not_reported_as_not_found(self, platform, base, code, raised):
        provider = get_provider(platform, {"ig_user_id": "1784"})
        with mock_http({"/1784": (400, {"error": {"code": code, "message": "x"}})}), pytest.raises(raised):
            provider.public_profile_metrics("rival", ACCOUNT, "t")

    def test_falls_back_to_the_connected_accounts_id(self, platform, base):
        with mock_http(
            {f"/{ACCOUNT.account_platform_id}": (200, {"business_discovery": {"username": "x", "followers_count": 1}})}
        ):
            m = get_provider(platform, {}).public_profile_metrics("x", ACCOUNT, "t")
        assert m.followers == 1 and m.avg_engagement is None


class TestNotAvailable:
    # Instagram Login is left out: Meta documents Business Discovery only for the Facebook Login API.
    SUPPORTED = {"bluesky", "mastodon", "youtube", "instagram"}

    @pytest.mark.parametrize("platform", sorted(set(PROVIDER_REGISTRY) - SUPPORTED))
    def test_other_networks_declare_no_support(self, platform):
        cls = PROVIDER_REGISTRY[platform]
        assert cls.supports_public_profile_metrics is False
        assert cls.public_profile_unavailable_reason
        with pytest.raises(NotImplementedError):
            cls({}).public_profile_metrics("someone", ACCOUNT, "t")

    def test_supported_set_is_exactly_the_documented_networks(self):
        assert {p for p, c in PROVIDER_REGISTRY.items() if c.supports_public_profile_metrics} == self.SUPPORTED


class TestHandleNormalisation:
    @pytest.mark.parametrize(
        "platform,typed,expected",
        [
            ("bluesky", "@Acme.bsky.social", "acme.bsky.social"),
            ("bluesky", "https://bsky.app/profile/acme.bsky.social", "acme.bsky.social"),
            ("youtube", "https://www.youtube.com/@AcmeTV", "acmetv"),
            ("instagram", "https://www.instagram.com/rival/", "rival"),
            ("mastodon", "@bob@other.example", "bob@other.example"),
            ("instagram", "not a handle!", ""),
            ("mastodon", "bob@nodot", ""),
        ],
    )
    def test_normalize(self, platform, typed, expected):
        assert PROVIDER_REGISTRY[platform].normalize_competitor_handle(typed) == expected

    def test_posts_per_week_measures_to_now(self):
        stamps = [NOW - timedelta(days=d) for d in (1, 8, 14)]
        assert PROVIDER_REGISTRY["bluesky"]._posts_per_week(stamps, NOW) == 1.5
        assert PROVIDER_REGISTRY["bluesky"]._posts_per_week([], NOW) is None
