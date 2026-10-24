"""Which failed writes may be retried, and which must be parked as 'outcome unknown'.

A mutating request that times out or answers 5xx used to be flagged
``ambiguous_write`` wherever it was made, and the engine fails an ambiguous post
for good because the write may have gone live. That is right for the call that
publishes and wrong for everything before it: opening an upload, sending a chunk,
creating a container, refreshing a token. Each provider now says so per call.
"""

import httpx
import pytest

from providers.bluesky import BlueskyProvider
from providers.exceptions import APIError
from providers.facebook import FacebookProvider
from providers.google_business import GoogleBusinessProvider
from providers.instagram import InstagramProvider
from providers.instagram_login import InstagramLoginProvider
from providers.kick import KickProvider
from providers.linkedin import LinkedInProvider
from providers.mastodon import MastodonProvider
from providers.pinterest import PinterestProvider
from providers.reddit import RedditProvider
from providers.threads import ThreadsProvider
from providers.tiktok import TikTokProvider
from providers.tumblr import TumblrProvider
from providers.twitch import TwitchProvider
from providers.types import PostType, PublishContent
from providers.wordpress import WordPressProvider
from providers.youtube import YouTubeProvider

CREDS = {
    "client_id": "id",
    "client_secret": "secret",
    "client_key": "key",
    "site_url": "https://wp.example.com",
    "username": "u",
    "instance_url": "https://mastodon.example",
    "pds_url": "https://bsky.example",
}

OK = "ok"
FAIL_5XX = "5xx"
TIMEOUT = "timeout"


@pytest.fixture
def script(monkeypatch, tmp_path):
    """Replace ``httpx.Client.request`` with a scripted sequence. The last step repeats once the list runs out."""

    def install(*steps):
        queue = list(steps)
        calls = []

        def fake(self, method, url, **kwargs):
            calls.append((method, url))
            step = queue.pop(0) if len(queue) > 1 else queue[0]
            request = httpx.Request(method, url)
            if step == TIMEOUT:
                raise httpx.ReadTimeout("timed out", request=request)
            if step == FAIL_5XX:
                return httpx.Response(503, request=request, json={"error": {"code": "unavailable", "message": "down"}})
            return httpx.Response(200, request=request, json=step)

        monkeypatch.setattr(httpx.Client, "request", fake)
        return calls

    return install


@pytest.fixture
def media_file(tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"0123456789")
    return str(path)


def _ambiguous(call):
    with pytest.raises(APIError) as caught:
        call()
    return caught.value.ambiguous_write


FAILURES = pytest.mark.parametrize("failure", [FAIL_5XX, TIMEOUT])


# ----------------------------------------------------------------------
# The base rule
# ----------------------------------------------------------------------


@FAILURES
def test_a_mutating_call_is_ambiguous_by_default(script, failure):
    script(failure)
    provider = FacebookProvider(CREDS)
    assert _ambiguous(lambda: provider._request("POST", "https://graph.facebook.com/x")) is True


@FAILURES
def test_a_call_declared_not_to_commit_is_an_ordinary_failure(script, failure):
    script(failure)
    provider = FacebookProvider(CREDS)
    assert (
        _ambiguous(lambda: provider._request("POST", "https://graph.facebook.com/x", ambiguous_on_failure=False))
        is False
    )


def test_a_declared_safe_call_still_fails_loudly_on_a_4xx(monkeypatch):
    def fake(self, method, url, **kwargs):
        return httpx.Response(400, request=httpx.Request(method, url), json={"error": "bad"})

    monkeypatch.setattr(httpx.Client, "request", fake)
    with pytest.raises(APIError) as caught:
        FacebookProvider(CREDS)._request("POST", "https://graph.facebook.com/x", ambiguous_on_failure=False)
    assert caught.value.status_code == 400
    assert caught.value.ambiguous_write is False


# ----------------------------------------------------------------------
# Meta: containers and upload sessions are preparatory, the publish call commits
# ----------------------------------------------------------------------


@FAILURES
def test_instagram_container_creation_is_retryable_and_media_publish_is_not(script, failure):
    provider = InstagramProvider(CREDS)
    script(failure)
    assert _ambiguous(lambda: provider._create_container("t", "ig1", {"image_url": "u"})) is False
    assert _ambiguous(lambda: provider._publish_container("t", "ig1", "c1")) is True


@FAILURES
def test_instagram_login_container_creation_is_retryable_and_media_publish_is_not(script, failure):
    provider = InstagramLoginProvider(CREDS)
    script(failure)
    assert _ambiguous(lambda: provider._create_container("t", {"image_url": "u"})) is False
    assert _ambiguous(lambda: provider._publish_container("t", "c1")) is True


@FAILURES
def test_threads_container_is_retryable_and_threads_publish_is_not(script, failure):
    provider = ThreadsProvider(CREDS)
    content = PublishContent(text="hi", post_type=PostType.TEXT, extra={"user_id": "u1"})

    script(failure)
    assert _ambiguous(lambda: provider.publish_post("t", content)) is False

    script({"id": "container-1"}, failure)
    assert _ambiguous(lambda: provider.publish_post("t", content)) is True


@FAILURES
def test_facebook_staged_photos_are_retryable_and_the_feed_post_is_not(script, failure):
    provider = FacebookProvider(CREDS)
    content = PublishContent(text="hi", media_urls=["https://m/1.jpg", "https://m/2.jpg"], post_type=PostType.IMAGE)

    script(failure)
    assert _ambiguous(lambda: provider._publish_multi_photo("t", "p1", content)) is False

    script({"id": "photo-1"}, {"id": "photo-2"}, failure)
    assert _ambiguous(lambda: provider._publish_multi_photo("t", "p1", content)) is True


@FAILURES
def test_facebook_reel_start_and_upload_are_retryable_and_finish_is_not(script, failure):
    provider = FacebookProvider(CREDS)
    content = PublishContent(text="hi", media_urls=["https://m/r.mp4"], post_type=PostType.REEL)
    start = {"video_id": "v1", "upload_url": "https://rupload.facebook.com/v1"}

    script(failure)
    assert _ambiguous(lambda: provider._publish_reel("t", "p1", content)) is False
    script(start, failure)
    assert _ambiguous(lambda: provider._publish_reel("t", "p1", content)) is False
    script(start, {"success": True}, failure)
    assert _ambiguous(lambda: provider._publish_reel("t", "p1", content)) is True


@FAILURES
def test_facebook_a_plain_feed_post_stays_ambiguous(script, failure):
    script(failure)
    provider = FacebookProvider(CREDS)
    content = PublishContent(text="hi", post_type=PostType.TEXT)
    assert _ambiguous(lambda: provider._publish_text_or_link("t", "p1", content)) is True


# ----------------------------------------------------------------------
# Other networks with a staging step
# ----------------------------------------------------------------------


@FAILURES
def test_linkedin_upload_init_is_retryable_and_the_post_is_not(script, failure, media_file):
    provider = LinkedInProvider(CREDS)
    image = PublishContent(text="hi", media_files=[media_file], post_type=PostType.IMAGE)
    script(failure)
    assert _ambiguous(lambda: provider._publish_image_post("t", "urn:li:person:1", image)) is False

    article = PublishContent(text="hi", link_url="https://example.com", post_type=PostType.LINK)
    script(failure)
    assert _ambiguous(lambda: provider._publish_article_post("t", "urn:li:person:1", article)) is True


@FAILURES
def test_tiktok_file_upload_session_is_retryable_but_the_video_upload_and_url_pull_are_not(script, failure, media_file):
    provider = TikTokProvider(CREDS)
    content = PublishContent(text="hi", title="t", media_files=[media_file], media_urls=["https://m/v.mp4"])

    script(failure)
    assert _ambiguous(lambda: provider._publish_file_upload("t", content, "SELF_ONLY")) is False

    # The single-chunk PUT is what hands TikTok the video to publish.
    init = {"data": {"upload_url": "https://upload.tiktok/u", "publish_id": "p1"}}
    script(init, failure)
    assert _ambiguous(lambda: provider._publish_file_upload("t", content, "SELF_ONLY")) is True

    # A pull-from-URL init is the publish itself.
    script(failure)
    assert _ambiguous(lambda: provider._publish_pull_from_url("t", content, "SELF_ONLY")) is True


@FAILURES
def test_pinterest_media_registration_and_upload_are_retryable_and_the_pin_is_not(script, failure, media_file):
    provider = PinterestProvider(CREDS)
    content = PublishContent(media_files=[media_file], post_type=PostType.PIN, extra={"board_id": "b"})
    register = {"media_id": "m1", "upload_url": "https://upload.pinterest/u"}

    script(failure)
    assert _ambiguous(lambda: provider._publish_video_pin("t", content, {})) is False
    script(register, failure)
    assert _ambiguous(lambda: provider._publish_video_pin("t", content, {})) is False
    script(register, {}, failure)
    assert _ambiguous(lambda: provider._publish_video_pin("t", content, {})) is True


@FAILURES
def test_mastodon_media_upload_is_retryable_and_posting_the_status_is_not(script, failure, media_file):
    provider = MastodonProvider(CREDS)
    script(failure)
    assert _ambiguous(lambda: provider._upload_media("t", media_file)) is False

    script(failure)
    assert _ambiguous(lambda: provider.publish_comment("t", "9", "hi")) is True


@FAILURES
def test_bluesky_blob_upload_is_retryable_and_creating_the_record_is_not(script, failure, media_file):
    provider = BlueskyProvider(CREDS)
    script(failure)
    assert _ambiguous(lambda: provider._upload_blob("t", media_file)) is False


@FAILURES
def test_wordpress_media_upload_is_retryable_and_creating_the_post_is_not(script, failure, media_file):
    provider = WordPressProvider(CREDS)
    script(failure)
    assert _ambiguous(lambda: provider._upload_image("pw", media_file)) is False

    script(failure)
    assert _ambiguous(lambda: provider._wp("POST", "/wp/v2/posts", "pw", json={})) is True


# ----------------------------------------------------------------------
# Token refreshes never create anything
# ----------------------------------------------------------------------


@FAILURES
@pytest.mark.parametrize(
    "provider_cls",
    [
        LinkedInProvider,
        PinterestProvider,
        TikTokProvider,
        GoogleBusinessProvider,
        YouTubeProvider,
        RedditProvider,
        TwitchProvider,
        KickProvider,
        TumblrProvider,
    ],
)
def test_a_token_refresh_is_never_ambiguous(script, failure, provider_cls):
    provider = provider_cls(CREDS)
    script(failure)
    assert _ambiguous(lambda: provider.refresh_token("refresh")) is False
