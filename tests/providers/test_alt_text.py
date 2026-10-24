"""Per-media alt text reaches every network whose API takes it, and only those."""

from unittest.mock import MagicMock, patch

import pytest

from providers import PROVIDER_REGISTRY
from providers.bluesky import BlueskyProvider
from providers.facebook import FacebookProvider
from providers.instagram import InstagramProvider
from providers.instagram_login import InstagramLoginProvider
from providers.lemmy import LemmyProvider
from providers.linkedin import LinkedInProvider
from providers.mastodon import MastodonProvider
from providers.pinterest import PinterestProvider
from providers.threads import ThreadsProvider
from providers.tumblr import TumblrProvider
from providers.types import PostType, PublishContent
from providers.x import XProvider

# Networks whose publish API documents a per-image description and that send it today.
ALT_TEXT_NETWORKS = {
    "bluesky",
    "facebook",
    "instagram",
    "instagram_login",
    "lemmy",
    "linkedin_company",
    "linkedin_personal",
    "mastodon",
    "pinterest",
    "threads",
    "tumblr",
    "x",
}


def _resp(body=None, status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = body or {}
    resp.headers = {}
    return resp


def _content(post_type=PostType.IMAGE, alts=("one", "two"), **kwargs):
    return PublishContent(
        text="hello",
        title="t",
        post_type=post_type,
        media_urls=[f"https://cdn.example/{i}.jpg" for i in range(len(alts))],
        media_files=[f"/tmp/{i}.jpg" for i in range(len(alts))],
        media_types=["image"] * len(alts),
        media_alt=list(alts),
        **kwargs,
    )


def test_capability_is_declared_on_exactly_the_networks_that_send_alt_text():
    declared = {name for name, cls in PROVIDER_REGISTRY.items() if cls.supports_alt_text}
    assert declared == ALT_TEXT_NETWORKS


def test_alt_text_accessor_trims_and_defaults():
    content = _content(alts=("  spaced  ",))
    assert content.alt_text(0) == "spaced"
    assert content.alt_text(3) == ""
    assert PublishContent().alt_text() == ""


def test_bluesky_sends_one_alt_per_image_with_aspect_ratio():
    content = _content(media_dimensions=[(400, 300), (0, 0)])
    with patch.object(BlueskyProvider, "_upload_blob", side_effect=[{"ref": 1}, {"ref": 2}]):
        embed = BlueskyProvider()._build_embed("tok", content)
    assert embed["images"] == [
        {"alt": "one", "image": {"ref": 1}, "aspectRatio": {"width": 400, "height": 300}},
        # Unknown size: no aspect ratio rather than a wrong one.
        {"alt": "two", "image": {"ref": 2}},
    ]


def test_bluesky_video_gets_aspect_ratio_and_alt():
    content = _content(PostType.VIDEO, alts=("a clip",), media_dimensions=[(1080, 1920)])
    with patch.object(BlueskyProvider, "_upload_blob", return_value={"ref": 9}):
        embed = BlueskyProvider()._build_embed("tok", content)
    assert embed == {
        "$type": "app.bsky.embed.video",
        "video": {"ref": 9},
        "aspectRatio": {"width": 1080, "height": 1920},
        "alt": "a clip",
    }


def test_bluesky_falls_back_to_the_old_single_alt_string():
    content = _content(alts=("",), extra={"alt_text": "legacy"})
    with patch.object(BlueskyProvider, "_upload_blob", return_value={"ref": 1}):
        embed = BlueskyProvider()._build_embed("tok", content)
    assert embed["images"][0]["alt"] == "legacy"


@pytest.mark.parametrize("cls", [InstagramProvider, InstagramLoginProvider])
def test_instagram_sends_alt_on_images_not_reels_or_stories(cls):
    provider = cls({})
    captured = []

    def create(self, *args):
        captured.append(args[-1])
        return "c1"

    args = ("tok", "ig1") if cls is InstagramProvider else ("tok",)
    with (
        patch.object(cls, "_create_container", create),
        patch.object(cls, "_wait_for_container"),
        patch.object(cls, "_publish_container"),
    ):
        provider._publish_single(*args, _content(alts=("a cat",)))
        provider._publish_single(*args, _content(PostType.STORY, alts=("a cat",)))
        reel = _content(PostType.REEL, alts=("a cat",))
        reel.media_types = ["video"]
        provider._publish_single(*args, reel)
        provider._publish_carousel(*args, _content(PostType.CAROUSEL, alts=("first", "second")))
    feed, story, reel_payload, child_one, child_two, _carousel = captured
    assert feed["alt_text"] == "a cat"
    assert "alt_text" not in story
    assert "alt_text" not in reel_payload
    assert child_one["alt_text"] == "first" and child_two["alt_text"] == "second"


def test_instagram_alt_is_cut_to_a_thousand_characters():
    provider = InstagramProvider({})
    captured = []
    with (
        patch.object(InstagramProvider, "_create_container", lambda self, *a: captured.append(a[-1]) or "c"),
        patch.object(InstagramProvider, "_wait_for_container"),
        patch.object(InstagramProvider, "_publish_container"),
    ):
        provider._publish_single("tok", "ig1", _content(alts=("x" * 1500,)))
    assert len(captured[0]["alt_text"]) == 1000


def test_threads_sends_alt_text_on_single_and_carousel_items():
    provider = ThreadsProvider({})
    calls = []

    def request(method, url, **kwargs):
        calls.append(kwargs.get("data"))
        return _resp({"id": "x"})

    with (
        patch.object(provider, "_request", side_effect=request),
        patch.object(provider, "_wait_for_container"),
    ):
        provider._publish_single("tok", "u1", _content(alts=("a dog",)))
        provider._publish_carousel("tok", "u1", _content(PostType.CAROUSEL, alts=("c1", "")))
    assert calls[0]["alt_text"] == "a dog"
    assert calls[2]["alt_text"] == "c1"
    assert "alt_text" not in calls[3]


def test_facebook_photo_and_staged_photos_carry_alt_text_custom():
    provider = FacebookProvider({})
    calls = []

    def request(method, url, **kwargs):
        calls.append(kwargs.get("json"))
        return _resp({"id": "p1", "post_id": "1_2"})

    with patch.object(provider, "_request", side_effect=request):
        provider._publish_photo("tok", "page", _content(alts=("sunset",)))
        provider._publish_multi_photo("tok", "page", _content(alts=("left", "")))
    assert calls[0]["alt_text_custom"] == "sunset"
    staged = [c for c in calls[1:] if isinstance(c, dict) and c.get("published") is False]
    assert staged[0]["alt_text_custom"] == "left"
    assert "alt_text_custom" not in staged[1]


def test_x_sets_metadata_for_images_and_skips_video_and_empty():
    provider = XProvider()
    calls = []
    with (
        patch.object(provider, "_api", side_effect=lambda *a, **k: calls.append((a, k)) or _resp()),
        patch.object(provider, "_upload_file", return_value=("m1", None)),
        patch.object(provider, "_file_digest", return_value="d"),
    ):
        content = _content(alts=("a chart", ""), idempotency_key=None)
        provider._upload_media("tok", content, [("/tmp/0.jpg", "image"), ("/tmp/1.jpg", "image")])
        provider._upload_media("tok", _content(alts=("clip",)), [("/tmp/0.mp4", "video")])
    assert len(calls) == 1
    (method, path, token), kwargs = calls[0]
    assert (method, path) == ("POST", "/2/media/metadata")
    assert kwargs["json"] == {"id": "m1", "metadata": {"alt_text": {"text": "a chart"}}}


def test_x_alt_refusal_does_not_fail_the_post():
    from providers.exceptions import PublishError

    provider = XProvider()
    with patch.object(provider, "_api", side_effect=PublishError("no", platform="X", retryable=False)):
        provider._set_alt_text("tok", "m1", "text")


def test_mastodon_uploads_with_a_description():
    provider = MastodonProvider({"instance_url": "https://masto.example"})
    seen = {}

    def request(method, url, **kwargs):
        seen.update(kwargs)
        return _resp({"id": "9"}, status=200)

    with patch.object(provider, "_request", side_effect=request), patch("builtins.open", MagicMock()):
        provider._upload_media("tok", "/tmp/0.jpg", "a heron")
    assert seen["data"] == {"description": "a heron"}


def test_mastodon_without_alt_sends_no_description():
    provider = MastodonProvider({"instance_url": "https://masto.example"})
    seen = {}
    with (
        patch.object(provider, "_request", side_effect=lambda m, u, **k: seen.update(k) or _resp({"id": "9"})),
        patch("builtins.open", MagicMock()),
    ):
        provider._upload_media("tok", "/tmp/0.jpg")
    assert seen["data"] is None


def test_linkedin_image_post_carries_alt_text():
    provider = LinkedInProvider({})
    bodies = []

    def request(method, url, **kwargs):
        if "images" in url:
            return _resp({"value": {"uploadUrl": "https://up", "image": "urn:li:image:1"}})
        bodies.append(kwargs.get("json"))
        return _resp()

    with patch.object(provider, "_request", side_effect=request), patch.object(provider, "_upload_binary"):
        provider._publish_image_post("tok", "urn:li:person:1", _content(alts=("team photo",)))
    assert bodies[-1]["content"]["media"] == {"id": "urn:li:image:1", "altText": "team photo"}


def test_pinterest_uses_the_media_alt_unless_the_pin_sets_its_own():
    provider = PinterestProvider({})
    bodies = []

    def request(method, url, **kwargs):
        bodies.append(kwargs.get("json"))
        return _resp({"id": "pin1"})

    content = _content(alts=("from media",), extra={"board_id": "b1"})
    with patch.object(provider, "_request", side_effect=request):
        provider.publish_post("tok", content)
        content.extra["alt_text"] = "pin level"
        provider.publish_post("tok", content)
    assert bodies[0]["alt_text"] == "from media"
    assert bodies[1]["alt_text"] == "pin level"


def test_tumblr_alt_is_per_image():
    provider = TumblrProvider({})
    post = provider._build_post(
        _content(alts=("first", "second"), extra={"alt_text": "ignored"}),
        [("a", "image", "image/jpeg"), ("b", "image", "image/jpeg")],
    )
    assert [b["alt_text"] for b in post["content"] if b["type"] == "image"] == ["first", "second"]


def test_lemmy_alt_applies_when_the_picture_is_the_link():
    provider = LemmyProvider({"instance_url": "https://lemmy.example"})
    bodies = []

    def api(method, path, **kwargs):
        if kwargs.get("json") and "community_id" in kwargs["json"]:
            bodies.append(kwargs["json"])
            return _resp({"post_view": {"post": {"id": 5, "ap_id": "x"}}})
        return _resp({"community_view": {"community": {"id": 3}}})

    content = _content(alts=("meme",), extra={"community": "memes@lemmy.example"})
    with (
        patch.object(provider, "_api", side_effect=api),
        patch.object(provider, "_community_id", return_value=3),
    ):
        provider.publish_post("tok", content)
        content.link_url = "https://example.com/article"
        provider.publish_post("tok", content)
    assert bodies[0]["alt_text"] == "meme"
    assert "alt_text" not in bodies[1]
