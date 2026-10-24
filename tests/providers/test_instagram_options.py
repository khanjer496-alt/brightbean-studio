"""Instagram per-post options (format, collaborators, trial Reels, placement, cover frame) with HTTP mocked."""

from unittest.mock import MagicMock

import pytest

from providers import post_settings_error_for, post_settings_for
from providers.exceptions import PublishError
from providers.instagram import InstagramProvider
from providers.instagram_login import InstagramLoginProvider
from providers.types import PostType, PublishContent

CREDS = {"client_id": "id", "client_secret": "secret"}


def _resp(data):
    return MagicMock(json=MagicMock(return_value=data))


def _provider(cls):
    provider = cls(CREDS)
    provider._wait_for_container = MagicMock()
    provider._permalink = MagicMock(return_value="https://www.instagram.com/p/x/")
    provider._request = MagicMock(side_effect=lambda *a, **k: _resp({"id": "c1"}))
    return provider


def _first_payload(provider):
    return provider._request.call_args_list[0].kwargs["json"]


def _content(post_type, extra=None, media=("https://m/x.mp4",), types=("video",)):
    return PublishContent(
        text="hi",
        media_urls=list(media),
        media_types=list(types),
        post_type=post_type,
        extra={"ig_user_id": "1784", **(extra or {})},
    )


@pytest.mark.parametrize("platform", ["instagram", "instagram_login"])
def test_declares_format_and_reel_options_for_the_composer(platform):
    keys = [s.key for s in post_settings_for(platform)]
    assert keys == ["post_type", "collaborators", "trial_reel", "share_to_feed", "thumb_offset"]


@pytest.mark.parametrize("cls", [InstagramProvider, InstagramLoginProvider])
def test_story_publishes_as_stories_media_type(cls):
    provider = _provider(cls)
    provider.publish_post(
        "tok", _content(PostType.STORY, {"post_type": "story"}, media=("https://m/x.jpg",), types=("image",))
    )
    payload = _first_payload(provider)
    assert payload["media_type"] == "STORIES"
    assert payload["image_url"] == "https://m/x.jpg"
    assert "collaborators" not in payload


@pytest.mark.parametrize("cls", [InstagramProvider, InstagramLoginProvider])
def test_reel_sends_trial_placement_cover_and_collaborators(cls):
    provider = _provider(cls)
    extra = {
        "post_type": "reel",
        "trial_reel": "SS_PERFORMANCE",
        "share_to_feed": "false",
        "thumb_offset": "1500",
        "collaborators": "@alice, bob.b",
    }
    provider.publish_post("tok", _content(PostType.REEL, extra))
    payload = _first_payload(provider)
    assert payload["media_type"] == "REELS"
    assert payload["trial_params"] == {"graduation_strategy": "SS_PERFORMANCE"}
    assert payload["share_to_feed"] is False
    assert payload["thumb_offset"] == 1500
    assert payload["collaborators"] == ["alice", "bob.b"]


def test_feed_image_takes_collaborators_but_not_reel_options():
    provider = _provider(InstagramProvider)
    provider.publish_post(
        "tok",
        _content(PostType.IMAGE, {"collaborators": "alice"}, media=("https://m/x.jpg",), types=("image",)),
    )
    assert _first_payload(provider)["collaborators"] == ["alice"]
    with pytest.raises(PublishError) as exc:
        provider.publish_post(
            "tok",
            _content(PostType.IMAGE, {"trial_reel": "MANUAL"}, media=("https://m/x.jpg",), types=("image",)),
        )
    assert exc.value.retryable is False


def test_carousel_collaborators_go_on_the_parent_container():
    provider = _provider(InstagramProvider)
    media = ("https://m/1.jpg", "https://m/2.jpg")
    provider.publish_post(
        "tok", _content(PostType.CAROUSEL, {"collaborators": "alice"}, media=media, types=("image", "image"))
    )
    payloads = [c.kwargs["json"] for c in provider._request.call_args_list if "json" in c.kwargs]
    parent = next(p for p in payloads if p.get("media_type") == "CAROUSEL")
    assert parent["collaborators"] == ["alice"]
    assert all("collaborators" not in p for p in payloads if p is not parent)


@pytest.mark.parametrize(
    "settings, fragment",
    [
        ({"collaborators": "a, b, c, d"}, "at most 3"),
        ({"collaborators": "bad name!"}, "valid Instagram username"),
        ({"post_type": "story", "collaborators": "alice"}, "Stories"),
        ({"post_type": "story", "trial_reel": "MANUAL"}, "need Reel as the post format"),
        ({"trial_reel": "MANUAL"}, "need Reel as the post format"),
        ({"trial_reel": "WHENEVER"}, "unsupported"),
        ({"post_type": "reel", "thumb_offset": "-5"}, "milliseconds"),
        ({"share_to_feed": "maybe"}, "unsupported"),
    ],
)
def test_validation_refuses_what_the_api_would(settings, fragment):
    assert fragment in post_settings_error_for("instagram", settings)
    assert fragment in post_settings_error_for("instagram_login", settings)


def test_valid_settings_pass():
    ok = {"post_type": "reel", "collaborators": "a,b,c", "trial_reel": "MANUAL", "thumb_offset": "0"}
    assert post_settings_error_for("instagram", ok) == ""
