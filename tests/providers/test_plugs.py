"""What each provider declares for engagement automations, and the calls behind the declarations."""

from unittest.mock import MagicMock, patch

import pytest

from providers import PROVIDER_REGISTRY, plug_capability_for
from providers.base import SocialProvider
from providers.bluesky import BlueskyProvider
from providers.linkedin_company import LinkedInCompanyProvider
from providers.mastodon import MastodonProvider
from providers.threads import ThreadsProvider
from providers.types import PlugAction, PlugCapability, PlugTarget, PlugTrigger
from providers.x import XProvider


def _json(payload, headers=None):
    return MagicMock(json=MagicMock(return_value=payload), headers=headers or {})


class TestDeclarations:
    def test_default_takes_no_automations(self):
        assert SocialProvider.plugs == PlugCapability()
        assert not SocialProvider.plugs.watchable
        assert plug_capability_for("no-such-network") == PlugCapability()

    @pytest.mark.parametrize(
        ("platform", "own", "cross", "triggers"),
        [
            ("bluesky", {"repost", "reply", "quote"}, {"repost", "quote"}, {"likes", "reposts"}),
            ("x", {"repost", "reply"}, {"repost"}, {"likes", "reposts", "views"}),
            ("mastodon", {"repost", "reply"}, {"repost"}, {"likes", "reposts"}),
            ("threads", {"reply"}, {"repost"}, {"likes", "reposts", "views"}),
            ("linkedin_company", {"reply"}, {"repost"}, {"likes", "reposts", "views"}),
        ],
    )
    def test_documented_capabilities(self, platform, own, cross, triggers):
        cap = plug_capability_for(platform)
        assert {a.value for a in cap.own_actions} == own
        assert {a.value for a in cap.cross_actions} == cross
        assert {t.value for t in cap.triggers} == triggers
        assert cap.source.startswith("https://")
        assert cap.watchable

    def test_x_quote_is_not_offered_and_reads_are_metered(self):
        cap = XProvider.plugs
        assert PlugAction.QUOTE not in cap.own_actions | cap.cross_actions
        assert cap.metered_reads
        assert cap.check_interval_minutes >= 60

    def test_personal_linkedin_has_no_numbers_to_wait_for(self):
        assert not plug_capability_for("linkedin_personal").watchable

    def test_every_declared_action_is_implemented(self):
        # An action a provider lists must be backed by a method that is not the base class's stub.
        for slug, cls in PROVIDER_REGISTRY.items():
            cap = cls.plugs
            actions = cap.own_actions | cap.cross_actions
            if PlugAction.REPOST in actions:
                assert cls.repost_post is not SocialProvider.repost_post, slug
            if PlugAction.QUOTE in actions:
                assert cls.quote_post is not SocialProvider.quote_post, slug
            if PlugAction.REPLY in cap.own_actions:
                assert cls.publish_comment is not SocialProvider.publish_comment, slug
            if cap.triggers:
                assert (
                    cls.plug_metrics is not SocialProvider.plug_metrics
                    or cls.get_post_metrics is not SocialProvider.get_post_metrics
                ), slug


class TestBluesky:
    uri = "at://did:plc:me/app.bsky.feed.post/3k"

    @patch.object(BlueskyProvider, "_request")
    def test_counts(self, request):
        request.return_value = _json({"posts": [{"uri": self.uri, "likeCount": 12, "repostCount": 0}]})
        assert BlueskyProvider().plug_metrics("t", self.uri) == {"likes": 12}
        assert request.call_args.kwargs["params"] == {"uris": self.uri}

    @patch.object(BlueskyProvider, "_request")
    def test_repost_writes_a_repost_record_for_the_post(self, request):
        request.side_effect = [
            _json({"posts": [{"uri": self.uri, "cid": "bafy"}]}),
            _json({"did": "did:plc:booster"}),
            _json({"uri": "at://did:plc:booster/app.bsky.feed.repost/9"}),
        ]
        result = BlueskyProvider().repost_post("t", PlugTarget(self.uri, cross_account=True))
        body = request.call_args.kwargs["json"]
        assert body["repo"] == "did:plc:booster"
        assert body["collection"] == "app.bsky.feed.repost"
        assert body["record"]["subject"] == {"uri": self.uri, "cid": "bafy"}
        assert result.platform_post_id.endswith("repost/9")

    @patch.object(BlueskyProvider, "_request")
    def test_quote_embeds_the_post(self, request):
        request.side_effect = [
            _json({"posts": [{"uri": self.uri, "cid": "bafy"}]}),
            _json({"did": "did:plc:me"}),
            _json({"uri": "at://did:plc:me/app.bsky.feed.post/1"}),
        ]
        with patch.object(BlueskyProvider, "_parse_facets", return_value=[]):
            BlueskyProvider().quote_post("t", PlugTarget(self.uri), "Worth a read")
        record = request.call_args.kwargs["json"]["record"]
        assert record["embed"] == {"$type": "app.bsky.embed.record", "record": {"uri": self.uri, "cid": "bafy"}}
        assert record["text"] == "Worth a read"

    @patch.object(BlueskyProvider, "_request")
    def test_reply_is_its_own_root_and_parent(self, request):
        request.side_effect = [
            _json({"posts": [{"uri": self.uri, "cid": "bafy"}]}),
            _json({"did": "did:plc:me", "handle": "me.bsky.social"}),
            _json({"uri": "at://did:plc:me/app.bsky.feed.post/2"}),
        ]
        with patch.object(BlueskyProvider, "_parse_facets", return_value=[]):
            result = BlueskyProvider().publish_comment("t", self.uri, "More here")
        reply = request.call_args.kwargs["json"]["record"]["reply"]
        assert reply == {"root": {"uri": self.uri, "cid": "bafy"}, "parent": {"uri": self.uri, "cid": "bafy"}}
        assert result.platform_comment_id.endswith("post/2")

    @patch.object(BlueskyProvider, "_request")
    def test_missing_post_is_a_permanent_error(self, request):
        from providers.exceptions import PublishError

        request.return_value = _json({"posts": []})
        with pytest.raises(PublishError) as raised:
            BlueskyProvider().repost_post("t", PlugTarget(self.uri))
        assert not raised.value.retryable


class TestX:
    @patch.object(XProvider, "_api")
    def test_counts_come_from_public_metrics(self, api):
        api.return_value = _json(
            {"data": {"public_metrics": {"like_count": 4, "retweet_count": 2, "impression_count": 900}}}
        )
        assert XProvider().plug_metrics("t", "17") == {"likes": 4, "reposts": 2, "views": 900}

    @patch.object(XProvider, "_api")
    def test_repost_uses_the_retweets_endpoint_of_the_acting_user(self, api):
        api.side_effect = [_json({"data": {"id": "42"}}), _json({"data": {"retweeted": True}})]
        XProvider().repost_post("t", PlugTarget("17"))
        method, path = api.call_args.args[:2]
        assert (method, path) == ("POST", "/2/users/42/retweets")
        assert api.call_args.kwargs["json"] == {"tweet_id": "17"}

    @patch.object(XProvider, "_api")
    def test_an_unconfirmed_repost_is_ambiguous(self, api):
        from providers.exceptions import APIError

        api.side_effect = [_json({"data": {"id": "42"}}), _json({"data": {"retweeted": False}})]
        with pytest.raises(APIError) as raised:
            XProvider().repost_post("t", PlugTarget("17"))
        assert raised.value.ambiguous_write


class TestMastodon:
    @patch.object(MastodonProvider, "_request")
    def test_own_post_is_boosted_by_id(self, request):
        request.return_value = _json({"id": "99", "url": "https://m.example/@me/99"})
        MastodonProvider({"instance_url": "https://m.example"}).repost_post("t", PlugTarget("5"))
        assert request.call_args.args[:2] == ("POST", "https://m.example/api/v1/statuses/5/reblog")

    @patch.object(MastodonProvider, "_request")
    def test_another_servers_post_is_resolved_first(self, request):
        request.side_effect = [_json({"statuses": [{"id": "777"}]}), _json({"id": "800"})]
        provider = MastodonProvider({"instance_url": "https://other.example"})
        provider.repost_post("t", PlugTarget("5", permalink="https://m.example/@me/5", cross_account=True))
        search, boost = request.call_args_list
        assert search.kwargs["params"]["q"] == "https://m.example/@me/5"
        assert search.kwargs["params"]["resolve"] == "true"
        assert boost.args[1].endswith("/api/v1/statuses/777/reblog")

    @patch.object(MastodonProvider, "_request")
    def test_cross_account_without_a_link_fails_permanently(self, request):
        from providers.exceptions import PublishError

        with pytest.raises(PublishError) as raised:
            MastodonProvider({"instance_url": "https://o.example"}).repost_post(
                "t", PlugTarget("5", cross_account=True)
            )
        assert not raised.value.retryable


class TestThreadsAndLinkedIn:
    @patch.object(ThreadsProvider, "_request")
    def test_threads_repost_endpoint(self, request):
        request.return_value = _json({"id": "r1"})
        result = ThreadsProvider().repost_post("t", PlugTarget("123"))
        assert request.call_args.args[:2][0] == "POST"
        assert request.call_args.args[1].endswith("/123/repost")
        assert result.platform_post_id == "r1"

    @patch.object(ThreadsProvider, "get_post_metrics")
    def test_threads_counts_map_to_triggers(self, metrics):
        from providers.types import PostMetrics

        metrics.return_value = PostMetrics(impressions=300, likes=7, shares=3)
        assert ThreadsProvider().plug_metrics("t", "1") == {"views": 300, "likes": 7, "reposts": 3}

    @patch.object(LinkedInCompanyProvider, "_request")
    def test_linkedin_reshare_names_the_page_as_author(self, request):
        request.return_value = _json({}, headers={"x-restli-id": "urn:li:share:9"})
        provider = LinkedInCompanyProvider({"author_urn": "urn:li:organization:5"})
        result = provider.repost_post("t", PlugTarget("urn:li:share:1", cross_account=True))
        body = request.call_args.kwargs["json"]
        assert body["author"] == "urn:li:organization:5"
        assert body["reshareContext"] == {"parent": "urn:li:share:1"}
        assert result.platform_post_id == "urn:li:share:9"

    def test_company_account_supplies_its_page_urn(self):
        account = MagicMock(account_platform_id="5")
        assert LinkedInCompanyProvider.credentials_for_account(account) == {"author_urn": "urn:li:organization:5"}


def test_trigger_names_are_the_ones_analytics_keys_use():
    assert {t.value for t in PlugTrigger} == {"likes", "reposts", "views"}
