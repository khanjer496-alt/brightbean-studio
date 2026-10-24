"""Bluesky post text: the limits, the rich-text facets, and the link card.

Bluesky counts graphemes (and 3,000 UTF-8 bytes), never cuts, detects links, mentions and hashtags
the way the reference client does, and does not unfurl links itself, so a post that is only a link
gets an ``app.bsky.embed.external`` card built from the page's own metadata.
"""

from unittest.mock import MagicMock, patch

import httpx
import pytest

from apps.autopost import feeds
from providers.bluesky import BlueskyProvider
from providers.exceptions import PublishError
from providers.types import PostType, PublishContent

PAGE = """<html><head><title>Fallback title</title>
<meta property="og:title" content="The &amp; Title">
<meta property="og:description" content="A short description.">
<meta property="og:image" content="/thumb.jpg"></head></html>"""


def _provider():
    provider = BlueskyProvider({})
    provider.resolve_handle = lambda handle: f"did:plc:{handle}"
    return provider


def _facets(text):
    provider = _provider()
    raw = text.encode()
    return [
        (
            raw[f["index"]["byteStart"] : f["index"]["byteEnd"]].decode(),
            next(iter(v for k, v in f["features"][0].items() if k != "$type")),
        )
        for f in provider._parse_facets(text, "token")
    ]


class TestLength:
    def test_300_graphemes_fit_however_many_code_points_they_are(self):
        _provider()._check_length("👨‍👩‍👧‍👦" * 100)  # 700 code points, 100 graphemes
        _provider()._check_length("é" * 300)

    def test_301_graphemes_are_refused_not_cut(self):
        with pytest.raises(PublishError, match="301") as raised:
            _provider()._check_length("a" * 301)
        assert raised.value.retryable is False

    def test_the_3000_byte_cap_applies_too(self):
        with pytest.raises(PublishError, match="bytes"):
            _provider()._check_length("👨‍👩‍👧‍👦" * 150)


class TestFacets:
    def test_sentence_punctuation_is_not_part_of_a_link(self):
        found = _facets("Read https://example.com/a, then https://example.com/b. Or (https://example.com/c)!")
        assert [uri for _text, uri in found] == [
            "https://example.com/a",
            "https://example.com/b",
            "https://example.com/c",
        ]

    def test_a_balanced_parenthesis_stays_in_the_link(self):
        assert (
            _facets("see https://en.wikipedia.org/wiki/Foo_(bar).")[0][1] == "https://en.wikipedia.org/wiki/Foo_(bar)"
        )

    def test_offsets_are_utf8_bytes_after_multibyte_text(self):
        (text, uri), *_ = _facets("日本語 😀 https://example.com/x")
        assert text == uri == "https://example.com/x"

    def test_hashtags_need_a_letter_and_lose_trailing_punctuation(self):
        found = _facets("#launch, #2026 #日本語. C# #ok!")
        assert found == [("#launch", "launch"), ("#日本語", "日本語"), ("#ok", "ok")]

    def test_a_hashtag_over_64_characters_is_left_as_text(self):
        assert _facets("#" + "a" * 65) == []

    def test_mentions_resolve_and_a_trailing_dot_is_dropped(self):
        assert _facets("hi @bob.bsky.social.") == [("@bob.bsky.social", "did:plc:bob.bsky.social")]

    def test_an_unresolvable_mention_is_plain_text(self):
        provider = _provider()
        provider.resolve_handle = MagicMock(side_effect=PublishError("no such handle"))
        assert provider._parse_facets("hi @nobody.example", "token") == []

    def test_an_email_address_is_not_a_mention(self):
        assert _facets("write to me@example.com") == []


def _http(status=200, content_type="text/html", text="", content=b""):
    return httpx.Response(
        status,
        headers={"content-type": content_type},
        text=text if not content else None,
        content=content or None,
    )


@pytest.fixture
def pages(monkeypatch):
    """Serve ``PAGE`` and a small JPEG through the autoposter's SSRF-safe fetch."""
    requested = []

    def fake_safe_fetch(url, headers, *, max_bytes, content_types=None):
        requested.append(url)
        if url.endswith("/thumb.jpg"):
            return _http(content_type="image/jpeg", content=b"\xff\xd8jpeg"), url
        if "broken" in url:
            return None, None
        return _http(text=PAGE), url

    monkeypatch.setattr(feeds, "safe_fetch", fake_safe_fetch)
    return requested


class TestLinkCard:
    def _record(self, provider, content):
        provider._request = MagicMock(
            side_effect=[
                MagicMock(json=MagicMock(return_value={"did": "did:plc:me", "handle": "me.bsky.social"})),
                MagicMock(json=MagicMock(return_value={"uri": "at://did:plc:me/app.bsky.feed.post/abc"})),
            ]
        )
        provider._upload_blob = MagicMock(return_value={"$type": "blob", "ref": {"$link": "bafk"}})
        provider._create_post("token", content)
        return provider._request.call_args_list[-1].kwargs["json"]["record"]

    def test_a_link_only_post_gets_a_card_with_title_description_and_thumbnail(self, pages):
        record = self._record(_provider(), PublishContent(text="Worth a read https://example.com/post."))
        card = record["embed"]
        assert card["$type"] == "app.bsky.embed.external"
        assert card["external"]["uri"] == "https://example.com/post"
        assert card["external"]["title"] == "The & Title"
        assert card["external"]["description"] == "A short description."
        assert card["external"]["thumb"]["$type"] == "blob"
        assert pages == ["https://example.com/post", "https://example.com/thumb.jpg"]

    def test_a_post_with_images_keeps_its_images_and_fetches_nothing(self, pages):
        provider = _provider()
        provider._build_embed = MagicMock(return_value={"$type": "app.bsky.embed.images", "images": []})
        record = self._record(provider, PublishContent(text="https://example.com/post", post_type=PostType.IMAGE))
        assert record["embed"]["$type"] == "app.bsky.embed.images"
        assert pages == []

    def test_a_post_without_a_link_has_no_card(self, pages):
        assert "embed" not in self._record(_provider(), PublishContent(text="just words"))

    def test_an_unreadable_page_still_publishes_with_no_card(self, pages):
        record = self._record(_provider(), PublishContent(text="https://broken.example/x"))
        # No page metadata: the card falls back to the address as its title and carries no thumbnail.
        assert record["embed"]["external"]["title"] == "broken.example"
        assert "thumb" not in record["embed"]["external"]

    def test_a_thumbnail_over_one_megabyte_is_left_off(self, pages, monkeypatch):
        monkeypatch.setattr(feeds, "download_image", lambda url: (b"x" * 1_000_001, "image/jpeg"))
        record = self._record(_provider(), PublishContent(text="https://example.com/post"))
        assert "thumb" not in record["embed"]["external"]

    def test_a_failure_building_the_card_never_blocks_the_post(self, pages, monkeypatch):
        monkeypatch.setattr(feeds, "link_preview", MagicMock(side_effect=RuntimeError("boom")))
        assert "embed" not in self._record(_provider(), PublishContent(text="https://example.com/post"))


class TestTheFetchIsSsrfSafe:
    def test_a_private_address_is_never_requested(self):
        with patch("apps.composer.views._fetch_hop") as hop:
            assert feeds.link_preview("http://127.0.0.1:8000/admin") == {
                "title": "",
                "description": "",
                "image_url": "",
            }
            assert feeds.link_preview("http://169.254.169.254/latest/meta-data") == {
                "title": "",
                "description": "",
                "image_url": "",
            }
        hop.assert_not_called()
