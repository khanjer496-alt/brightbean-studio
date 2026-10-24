"""The Instagram container flow: how long it waits, what a retry reuses, and the status-read quirk."""

from unittest.mock import MagicMock

import pytest

from providers import instagram_containers
from providers.exceptions import APIError, PublishError
from providers.instagram import InstagramProvider
from providers.instagram_login import InstagramLoginProvider
from providers.types import PostType, PublishContent

CREDS = {"client_id": "id", "client_secret": "secret"}
IMAGE = "https://cdn.example/pic.jpg?X-Amz-Signature=one"
VIDEO = "https://cdn.example/clip.mp4?X-Amz-Signature=one"

PROVIDERS = [
    pytest.param(InstagramProvider, "/ig-1/media", id="facebook-login"),
    pytest.param(InstagramLoginProvider, "/me/media", id="instagram-login"),
]


class DictCache:
    def __init__(self):
        self.data = {}

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value, timeout):
        self.data[key] = value


class FakeGraph:
    """A scripted Graph API behind ``provider._request``: containers, their statuses, and publishing."""

    def __init__(self):
        self.created = 0
        self.statuses = {}  # container id -> list of status answers, consumed in order, last one repeats
        self.log = []

    def __call__(self, method, url, **kwargs):
        self.log.append((method, url))
        if url.endswith("/media"):
            self.created += 1
            container = f"container-{self.created}"
            self.statuses.setdefault(container, [{"status_code": "FINISHED"}])
            return MagicMock(json=MagicMock(return_value={"id": container}))
        if url.endswith("/media_publish"):
            return MagicMock(json=MagicMock(return_value={"id": "media-1"}))
        container = url.rsplit("/", 1)[-1]
        if container in self.statuses:
            queue = self.statuses[container]
            answer = queue.pop(0) if len(queue) > 1 else queue[0]
            if isinstance(answer, Exception):
                raise answer
            return MagicMock(json=MagicMock(return_value=answer), status_code=200)
        return MagicMock(json=MagicMock(return_value={"permalink": "https://www.instagram.com/p/x/"}))

    def containers_created(self):
        return self.created


def quirk():
    return APIError(
        "Graph 100/33",
        status_code=400,
        platform="Instagram",
        raw_response={"error": {"code": 100, "error_subcode": 33, "message": "Unsupported get request"}},
    )


@pytest.fixture(autouse=True)
def fast_polling(monkeypatch):
    monkeypatch.setattr(instagram_containers, "CONTAINER_POLL_INTERVAL", 0)


def make(provider_cls, graph):
    provider = provider_cls(CREDS)
    provider._request = graph
    provider._permalink = MagicMock(return_value=None)
    return provider


def image_content(cache=None, key="publish-1", url=IMAGE, text="hello"):
    return PublishContent(
        text=text,
        media_urls=[url],
        media_types=["image"],
        post_type=PostType.IMAGE,
        extra={"ig_user_id": "ig-1"},
        idempotency_key=key,
        upload_cache=cache,
    )


def video_content(cache=None, key="publish-1"):
    return PublishContent(
        text="hello",
        media_urls=[VIDEO],
        media_types=["video"],
        post_type=PostType.REEL,
        extra={"ig_user_id": "ig-1"},
        idempotency_key=key,
        upload_cache=cache,
    )


# ----------------------------------------------------------------------
# Poll timeout
# ----------------------------------------------------------------------


def test_a_video_is_waited_on_longer_than_a_photo_by_default():
    assert instagram_containers.InstagramContainers._container_poll_timeout(is_video=False) == 120
    assert instagram_containers.InstagramContainers._container_poll_timeout(is_video=True) == 600


def test_the_timeouts_are_configurable(settings):
    settings.INSTAGRAM_CONTAINER_POLL_TIMEOUT = 30
    settings.INSTAGRAM_VIDEO_CONTAINER_POLL_TIMEOUT = 900
    assert instagram_containers.InstagramContainers._container_poll_timeout(is_video=False) == 30
    assert instagram_containers.InstagramContainers._container_poll_timeout(is_video=True) == 900


@pytest.mark.parametrize(("provider_cls", "edge"), PROVIDERS)
def test_a_container_still_processing_at_the_deadline_is_a_retryable_timeout(provider_cls, edge, settings):
    settings.INSTAGRAM_VIDEO_CONTAINER_POLL_TIMEOUT = 0
    graph = FakeGraph()
    graph.statuses["container-1"] = [{"status_code": "IN_PROGRESS"}]
    provider = make(provider_cls, graph)

    with pytest.raises(PublishError) as caught:
        provider.publish_post("tok", video_content())

    assert caught.value.retryable is True
    assert "timed out" in str(caught.value)


def test_a_video_container_is_polled_past_the_old_two_minute_limit(monkeypatch, settings):
    """A slow Reel finishes after many polls; the wait is bounded by the video timeout, not 120s."""
    graph = FakeGraph()
    graph.statuses["container-1"] = [{"status_code": "IN_PROGRESS"}] * 5 + [{"status_code": "FINISHED"}]
    provider = make(InstagramProvider, graph)

    result = provider.publish_post("tok", video_content())

    assert result.platform_post_id == "media-1"
    assert sum(1 for method, url in graph.log if method == "GET" and "container-1" in url) == 6


# ----------------------------------------------------------------------
# Reuse across retries
# ----------------------------------------------------------------------


@pytest.mark.parametrize(("provider_cls", "edge"), PROVIDERS)
def test_a_retry_resumes_the_container_an_earlier_attempt_created(provider_cls, edge, settings):
    settings.INSTAGRAM_VIDEO_CONTAINER_POLL_TIMEOUT = 0
    cache = DictCache()
    graph = FakeGraph()
    graph.statuses["container-1"] = [{"status_code": "IN_PROGRESS"}]
    provider = make(provider_cls, graph)

    with pytest.raises(PublishError):
        provider.publish_post("tok", video_content(cache))
    assert graph.containers_created() == 1

    # Meta has finished by the time the retry runs.
    graph.statuses["container-1"] = [{"status_code": "FINISHED"}]
    settings.INSTAGRAM_VIDEO_CONTAINER_POLL_TIMEOUT = 600
    result = provider.publish_post("tok", video_content(cache))

    assert result.platform_post_id == "media-1"
    assert graph.containers_created() == 1, "the retry must not build a second container"
    publish_calls = [url for method, url in graph.log if url.endswith("/media_publish")]
    assert len(publish_calls) == 1


def test_a_reused_container_is_found_even_though_the_media_url_is_presigned_afresh():
    cache = DictCache()
    graph = FakeGraph()
    provider = make(InstagramProvider, graph)
    provider.publish_post("tok", image_content(cache, url=IMAGE))
    provider.publish_post("tok", image_content(cache, url=IMAGE.replace("one", "two")))

    assert graph.containers_created() == 1


def test_an_edited_post_does_not_inherit_the_old_container():
    cache = DictCache()
    graph = FakeGraph()
    provider = make(InstagramProvider, graph)
    provider.publish_post("tok", image_content(cache, text="first"))
    provider.publish_post("tok", image_content(cache, text="edited"))

    assert graph.containers_created() == 2


def test_a_different_publish_does_not_share_a_container():
    cache = DictCache()
    graph = FakeGraph()
    provider = make(InstagramProvider, graph)
    provider.publish_post("tok", image_content(cache, key="publish-1"))
    provider.publish_post("tok", image_content(cache, key="publish-2"))

    assert graph.containers_created() == 2


def test_a_dead_remembered_container_is_replaced():
    cache = DictCache()
    graph = FakeGraph()
    provider = make(InstagramProvider, graph)
    provider.publish_post("tok", image_content(cache))
    graph.statuses["container-1"] = [{"status_code": "EXPIRED"}]

    provider.publish_post("tok", image_content(cache))

    assert graph.containers_created() == 2


def test_a_remembered_container_that_meta_no_longer_knows_is_replaced():
    cache = DictCache()
    graph = FakeGraph()
    provider = make(InstagramProvider, graph)
    provider.publish_post("tok", image_content(cache))
    graph.statuses["container-1"] = [quirk()]

    provider.publish_post("tok", image_content(cache))

    assert graph.containers_created() == 2


def test_without_a_cache_every_attempt_builds_its_own_container():
    graph = FakeGraph()
    provider = make(InstagramProvider, graph)
    provider.publish_post("tok", image_content(None))
    provider.publish_post("tok", image_content(None))

    assert graph.containers_created() == 2


def test_a_carousel_reuses_its_children_and_parent():
    cache = DictCache()
    graph = FakeGraph()
    provider = make(InstagramProvider, graph)
    content = PublishContent(
        media_urls=[IMAGE, VIDEO],
        media_types=["image", "video"],
        post_type=PostType.CAROUSEL,
        extra={"ig_user_id": "ig-1"},
        idempotency_key="publish-1",
        upload_cache=cache,
    )
    provider.publish_post("tok", content)
    assert graph.containers_created() == 3

    provider.publish_post("tok", content)
    assert graph.containers_created() == 3


# ----------------------------------------------------------------------
# The code 100 / subcode 33 quirk
# ----------------------------------------------------------------------


@pytest.mark.parametrize(("provider_cls", "edge"), PROVIDERS)
def test_the_status_read_quirk_keeps_polling(provider_cls, edge):
    graph = FakeGraph()
    graph.statuses["container-1"] = [quirk(), quirk(), {"status_code": "FINISHED"}]
    provider = make(provider_cls, graph)

    result = provider.publish_post("tok", image_content())

    assert result.platform_post_id == "media-1"


def test_a_status_read_that_keeps_failing_with_the_quirk_is_eventually_believed():
    graph = FakeGraph()
    graph.statuses["container-1"] = [quirk()]
    provider = make(InstagramProvider, graph)

    with pytest.raises(APIError):
        provider.publish_post("tok", image_content())


def test_any_other_status_read_failure_is_raised_at_once():
    graph = FakeGraph()
    graph.statuses["container-1"] = [APIError("denied", status_code=403, platform="Instagram")]
    provider = make(InstagramProvider, graph)

    with pytest.raises(APIError) as caught:
        provider.publish_post("tok", image_content())

    assert caught.value.status_code == 403
    assert sum(1 for method, url in graph.log if method == "GET" and "container-1" in url) == 1


# ----------------------------------------------------------------------
# Container errors
# ----------------------------------------------------------------------


def test_a_container_that_fails_for_a_known_reason_is_not_retried():
    graph = FakeGraph()
    graph.statuses["container-1"] = [{"status_code": "ERROR", "status": "Error: failed with error code 2207026"}]
    provider = make(InstagramProvider, graph)

    with pytest.raises(PublishError) as caught:
        provider.publish_post("tok", image_content())

    assert caught.value.retryable is False
    assert "format" in str(caught.value).lower()


def test_a_container_that_fails_for_an_unknown_reason_keeps_its_retries():
    graph = FakeGraph()
    graph.statuses["container-1"] = [{"status_code": "ERROR", "status": "Error: no idea"}]
    provider = make(InstagramProvider, graph)

    with pytest.raises(PublishError) as caught:
        provider.publish_post("tok", image_content())

    assert caught.value.retryable is True


def test_the_worker_stopping_interrupts_the_wait():
    graph = FakeGraph()
    graph.statuses["container-1"] = [{"status_code": "IN_PROGRESS"}]
    provider = make(InstagramProvider, graph)
    content = image_content()
    content.should_stop = lambda: True

    with pytest.raises(PublishError) as caught:
        provider.publish_post("tok", content)

    assert caught.value.retryable is True
