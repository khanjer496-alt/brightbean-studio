"""``SocialProvider.mention_lookup`` — which platforms can suggest accounts to @mention."""

from unittest.mock import MagicMock, patch

import pytest

from providers import PROVIDER_REGISTRY, mention_lookup_for
from providers.base import SocialProvider
from providers.bluesky import BlueskyProvider
from providers.mastodon import MastodonProvider
from providers.types import MentionSuggestion


def _response(body) -> MagicMock:
    return MagicMock(json=MagicMock(return_value=body))


def test_lookup_is_read_off_the_class():
    assert mention_lookup_for("bluesky") is True
    assert mention_lookup_for("mastodon") is True
    assert mention_lookup_for("linkedin_personal") is False
    assert mention_lookup_for("not-a-platform") is False


def test_every_declared_lookup_is_implemented():
    for platform, provider_cls in PROVIDER_REGISTRY.items():
        if provider_cls.mention_lookup:
            assert provider_cls.search_mentions is not SocialProvider.search_mentions, platform


def test_default_provider_does_not_look_up():
    provider = PROVIDER_REGISTRY["linkedin_personal"]()
    with pytest.raises(NotImplementedError):
        provider.search_mentions("token", "alice")


class TestBlueskySearchMentions:
    @patch.object(BlueskyProvider, "_request")
    def test_searches_actor_typeahead_with_the_accounts_token(self, mock_request):
        mock_request.return_value = _response(
            {
                "actors": [
                    {"handle": "alice.bsky.social", "displayName": "Alice", "avatar": "https://cdn.example/a.jpg"},
                    {"handle": "alicia.example.com"},
                    {"displayName": "No handle"},
                ]
            }
        )

        results = BlueskyProvider({"pds_url": "https://pds.example"}).search_mentions("jwt", "ali", limit=5)

        assert results == [
            MentionSuggestion(handle="alice.bsky.social", name="Alice", avatar_url="https://cdn.example/a.jpg"),
            MentionSuggestion(handle="alicia.example.com"),
        ]
        args, kwargs = mock_request.call_args
        assert args == ("GET", "https://pds.example/xrpc/app.bsky.actor.searchActorsTypeahead")
        assert kwargs["params"] == {"q": "ali", "limit": 5}
        assert kwargs["access_token"] == "jwt"


class TestMastodonSearchMentions:
    @patch.object(MastodonProvider, "_request")
    def test_searches_accounts_on_the_instance_without_resolving(self, mock_request):
        mock_request.return_value = _response(
            [
                {"acct": "alice", "display_name": "Alice", "avatar": "https://cdn.example/a.png"},
                {"acct": "bob@other.social", "display_name": ""},
                {"display_name": "No acct"},
            ]
        )

        results = MastodonProvider({"instance_url": "https://mastodon.example/"}).search_mentions("tok", "a")

        assert results == [
            MentionSuggestion(handle="alice", name="Alice", avatar_url="https://cdn.example/a.png"),
            MentionSuggestion(handle="bob@other.social"),
        ]
        args, kwargs = mock_request.call_args
        assert args == ("GET", "https://mastodon.example/api/v1/accounts/search")
        assert kwargs["params"] == {"q": "a", "limit": 8, "resolve": "false"}
        assert kwargs["access_token"] == "tok"
