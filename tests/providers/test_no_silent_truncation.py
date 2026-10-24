"""A provider never cuts what the user wrote: an over-long field fails the post with a sentence.

Each of these used to be sliced (``text[:limit]``) on the way to the platform, so the post went out
shorter than it was written and nobody was told. ``SocialProvider._require_fit`` refuses instead.
Fields the platform itself shortens for display (YouTube Shorts' "#Shorts" suffix aside) are not cut here.
"""

from unittest.mock import MagicMock

import pytest

from providers.devto import DevtoProvider
from providers.exceptions import PublishError
from providers.pinterest import PinterestProvider
from providers.threads import ThreadsProvider
from providers.tiktok import TikTokProvider
from providers.types import PostType, PublishContent
from providers.youtube import YouTubeProvider

CREDS = {"client_id": "id", "client_secret": "secret", "app_id": "id", "app_secret": "secret", "client_key": "k"}


def _refused(provider, call):
    with pytest.raises(PublishError) as raised:
        call()
    assert raised.value.retryable is False
    provider._request.assert_not_called()
    return str(raised.value)


def _mocked(cls):
    provider = cls(CREDS)
    provider._request = MagicMock()
    return provider


def test_threads_refuses_a_text_over_500_instead_of_cutting_it():
    provider = _mocked(ThreadsProvider)
    message = _refused(
        provider,
        lambda: provider._publish_single("token", "1", PublishContent(text="a" * 501, post_type=PostType.TEXT)),
    )
    assert "501" in message
    assert "500" in message


def test_threads_counts_emoji_by_their_bytes_before_refusing():
    provider = _mocked(ThreadsProvider)
    # 126 emoji are 126 characters but 504 bytes: over, though a plain length count says fine.
    _refused(
        provider,
        lambda: provider._publish_single("token", "1", PublishContent(text="😀" * 126, post_type=PostType.TEXT)),
    )


def test_threads_refuses_a_sixth_link():
    provider = _mocked(ThreadsProvider)
    text = " ".join(f"https://example.com/{n}" for n in range(6))
    message = _refused(
        provider, lambda: provider._publish_single("token", "1", PublishContent(text=text, post_type=PostType.TEXT))
    )
    assert "6 different links" in message


def test_threads_refuses_a_long_reply():
    provider = _mocked(ThreadsProvider)
    provider._request.return_value = MagicMock(json=MagicMock(return_value={"id": "1"}))
    with pytest.raises(PublishError):
        provider.publish_comment("token", "post-1", "a" * 501)
    provider._request.assert_called_once()  # only the user-id lookup; the reply container was never created


def test_pinterest_refuses_a_long_description_and_title():
    provider = _mocked(PinterestProvider)
    content = PublishContent(text="a" * 501, post_type=PostType.PIN, extra={"board_id": "b"})
    assert "500" in _refused(provider, lambda: provider.publish_post("token", content))
    titled = PublishContent(text="ok", title="t" * 101, post_type=PostType.PIN, extra={"board_id": "b"})
    assert "title" in _refused(provider, lambda: provider.publish_post("token", titled)).lower()


def test_tiktok_counts_utf16_so_a_caption_of_1100_emoji_is_too_long():
    provider = _mocked(TikTokProvider)
    info = PublishContent(text="😀" * 1101, post_type=PostType.VIDEO)
    assert "2202" in _refused(provider, lambda: provider._build_post_info(info, "SELF_ONLY"))
    fits = provider._build_post_info(PublishContent(text="😀" * 1100), "SELF_ONLY")
    assert fits["title"] == "😀" * 1100


def test_youtube_refuses_a_title_over_100_and_a_description_over_5000_bytes():
    provider = _mocked(YouTubeProvider)
    long_title = PublishContent(text="d", title="t" * 101, post_type=PostType.VIDEO)
    assert "title" in _refused(provider, lambda: provider.publish_post("token", long_title)).lower()
    # 1,700 CJK characters are 5,100 bytes.
    long_description = PublishContent(text="日" * 1700, title="ok", post_type=PostType.VIDEO)
    assert "description" in _refused(provider, lambda: provider.publish_post("token", long_description)).lower()


def test_youtube_names_the_shorts_suffix_when_it_pushes_the_title_over():
    provider = _mocked(YouTubeProvider)
    content = PublishContent(text="d", title="t" * 95, post_type=PostType.SHORT)
    assert "#Shorts" in _refused(provider, lambda: provider.publish_post("token", content))


def test_devto_refuses_a_long_title():
    provider = _mocked(DevtoProvider)
    content = PublishContent(text="body", title="t" * 129, post_type=PostType.TEXT)
    assert "title" in _refused(provider, lambda: provider.publish_post("token", content)).lower()


def test_youtube_shortens_a_title_made_from_the_caption_at_a_word():
    from providers.youtube import _derived_title

    caption = "word " * 40 + "\nsecond line"
    title = _derived_title(caption, 100)
    assert len(title) <= 100 and title.endswith("…") and "second" not in title
    assert _derived_title("Short caption\nmore", 92) == "Short caption"
