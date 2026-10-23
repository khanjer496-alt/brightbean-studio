"""What each provider declares about threads, and how a part reaches its API."""

from unittest.mock import MagicMock, patch

import pytest

from providers import PROVIDER_REGISTRY, thread_capability_for
from providers.bluesky import BlueskyProvider
from providers.exceptions import PublishError
from providers.facebook import FacebookProvider
from providers.mastodon import MastodonProvider
from providers.threads import ThreadsProvider
from providers.tiktok import TikTokProvider
from providers.types import CommentResult, PostType, PublishContent, PublishResult, ThreadMode

ROOT = PublishResult(platform_post_id="root-1")


def _response(body):
    return MagicMock(json=MagicMock(return_value=body))


class TestDeclarations:
    @pytest.mark.parametrize(
        ("platform", "mode"),
        [
            ("bluesky", ThreadMode.REPLY_CHAIN),
            ("threads", ThreadMode.REPLY_CHAIN),
            ("mastodon", ThreadMode.REPLY_CHAIN),
            ("facebook", ThreadMode.COMMENTS),
            ("instagram", ThreadMode.COMMENTS),
            ("instagram_login", ThreadMode.COMMENTS),
            ("linkedin_personal", ThreadMode.COMMENTS),
            ("linkedin_company", ThreadMode.COMMENTS),
            ("youtube", ThreadMode.COMMENTS),
            ("tiktok", ThreadMode.NONE),
            ("pinterest", ThreadMode.NONE),
            ("google_business", ThreadMode.NONE),
            ("devto", ThreadMode.NONE),
            ("telegram", ThreadMode.REPLY_CHAIN),
            ("discord", ThreadMode.REPLY_CHAIN),
            ("slack", ThreadMode.REPLY_CHAIN),
            ("reddit", ThreadMode.COMMENTS),
        ],
    )
    def test_every_platform_declares_its_mode(self, platform, mode):
        assert thread_capability_for(platform).mode is mode

    def test_unknown_platform_takes_no_thread(self):
        assert not thread_capability_for("myspace").supports_threads

    def test_comments_are_text_only_and_reply_chains_carry_media(self):
        for platform, provider_cls in PROVIDER_REGISTRY.items():
            thread = provider_cls.thread
            if thread.mode is ThreadMode.COMMENTS:
                assert not thread.media_per_part, platform
            if thread.mode is ThreadMode.REPLY_CHAIN:
                assert thread.media_per_part, platform

    def test_a_platform_that_takes_threads_takes_more_than_one_part(self):
        for platform, provider_cls in PROVIDER_REGISTRY.items():
            if provider_cls.thread.supports_threads:
                assert provider_cls.thread.max_parts > 1, platform


class TestBaseBehaviour:
    def test_a_platform_without_threads_refuses_a_part(self):
        with pytest.raises(NotImplementedError):
            TikTokProvider().publish_thread_part("t", PublishContent(text="x"), root=ROOT, parent=ROOT)

    def test_comments_mode_comments_on_the_root(self):
        provider = FacebookProvider()
        parent = PublishResult(platform_post_id="c-2")
        with patch.object(
            FacebookProvider, "publish_comment", return_value=CommentResult(platform_comment_id="c-3")
        ) as comment:
            result = provider.publish_thread_part("t", PublishContent(text="three"), root=ROOT, parent=parent)
        comment.assert_called_once_with("t", "root-1", "three")
        assert result.platform_post_id == "c-3"

    def test_comments_mode_reconciles_through_find_own_comment(self):
        with patch.object(FacebookProvider, "find_own_comment", return_value="c-2") as find:
            found = FacebookProvider().find_thread_part("t", root=ROOT, parent=ROOT, text="two")
        find.assert_called_once_with("t", "root-1", "two")
        assert found == "c-2"

    def test_a_reply_chain_without_a_lookup_cannot_reconcile(self):
        with pytest.raises(NotImplementedError):
            MastodonProvider(credentials={}).find_thread_part("t", root=ROOT, parent=ROOT, text="x")


class TestThreadsReplies:
    @patch.object(ThreadsProvider, "_request")
    def test_a_carousel_part_is_a_reply_too(self, mock_request):
        mock_request.side_effect = [
            _response({"id": "item-1"}),
            _response({"id": "item-2"}),
            _response({"id": "carousel"}),
            _response({"status": "FINISHED"}),
            _response({"id": "t-2"}),
        ]
        content = PublishContent(
            text="two",
            media_urls=["https://cdn/a.jpg", "https://cdn/b.jpg"],
            media_types=["image", "image"],
            post_type=PostType.CAROUSEL,
            extra={"user_id": "u"},
        )
        result = ThreadsProvider().publish_thread_part(
            "t", content, root=ROOT, parent=PublishResult(platform_post_id="t-1")
        )
        carousel_payload = mock_request.call_args_list[2].kwargs["data"]
        assert carousel_payload["reply_to_id"] == "t-1"
        assert result.platform_post_id == "t-2"
        # The caller's content is not mutated: the next part gets its own parent.
        assert "reply_to_id" not in content.extra


class TestBlueskyReplies:
    def test_a_parent_it_cannot_find_is_a_permanent_failure(self):
        provider = BlueskyProvider()
        with (
            patch.object(BlueskyProvider, "_request", return_value=_response({})),
            pytest.raises(PublishError) as excinfo,
        ):
            provider._strong_ref("t", PublishResult(platform_post_id="at://did:plc:me/app.bsky.feed.post/x"))
        assert excinfo.value.retryable is False


class TestMastodonReplies:
    @patch.object(MastodonProvider, "_request")
    def test_a_part_replies_with_its_own_idempotency_key(self, mock_request):
        mock_request.return_value = _response({"id": "s-2", "url": "https://m/@a/2"})
        provider = MastodonProvider(credentials={"instance_url": "https://m"})
        result = provider.publish_thread_part(
            "t",
            PublishContent(text="two", idempotency_key="key-part-2"),
            root=ROOT,
            parent=PublishResult(platform_post_id="s-1"),
        )
        kwargs = mock_request.call_args.kwargs
        assert kwargs["data"]["in_reply_to_id"] == "s-1"
        assert kwargs["headers"]["Idempotency-Key"]
        assert result.url == "https://m/@a/2"
