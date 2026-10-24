"""YouTube's resumable upload: sessions are remembered and resumed, never restarted blindly."""

from unittest.mock import MagicMock, patch

import pytest

from providers.exceptions import APIError, PublishError
from providers.types import PostType, PublishContent
from providers.youtube import UPLOAD_CHUNK_SIZE, UPLOAD_SESSION_TTL, YouTubeProvider

SESSION = "https://upload.example/session/abc"


class MemoryCache:
    def __init__(self):
        self.store = {}
        self.sets = []

    def get(self, key):
        return self.store.get(key) or None

    def set(self, key, value, timeout):
        self.store[key] = value
        self.sets.append((key, value, timeout))


def _resp(status=200, headers=None, body=None):
    resp = MagicMock()
    resp.status_code = status
    resp.headers = headers or {}
    resp.json.return_value = body or {}
    return resp


def _content(path, cache=None, key="brightbean-1"):
    return PublishContent(
        text="t",
        title="t",
        media_files=[str(path)],
        post_type=PostType.VIDEO,
        idempotency_key=key,
        upload_cache=cache,
    )


@pytest.fixture
def video(tmp_path):
    path = tmp_path / "v.mp4"
    path.write_bytes(b"x" * (UPLOAD_CHUNK_SIZE + 1000))
    return path


@pytest.fixture(autouse=True)
def no_sleep():
    with patch("providers.youtube.time.sleep"):
        yield


def _range_header(r):
    return r.kwargs["headers"]["Content-Range"]


def test_happy_path_initiates_with_size_then_sends_chunks(video):
    calls = []

    def fake(self, method, url, **kwargs):
        calls.append((method, url, kwargs))
        if method == "POST":
            return _resp(headers={"Location": SESSION})
        rng = kwargs["headers"]["Content-Range"]
        if rng.startswith("bytes */"):
            return _resp(308)
        if rng.startswith("bytes 0-"):
            return _resp(308, {"Range": f"bytes=0-{UPLOAD_CHUNK_SIZE - 1}"})
        return _resp(201, body={"id": "vid1"})

    cache = MemoryCache()
    with patch.object(YouTubeProvider, "_request", fake):
        result = YouTubeProvider().publish_post("tok", _content(video, cache))

    assert result.platform_post_id == "vid1"
    init = calls[0]
    assert init[0] == "POST"
    assert init[2]["headers"]["X-Upload-Content-Length"] == str(video.stat().st_size)
    ranges = [c[2]["headers"]["Content-Range"] for c in calls[1:]]
    total = video.stat().st_size
    assert ranges == [
        f"bytes */{total}",
        f"bytes 0-{UPLOAD_CHUNK_SIZE - 1}/{total}",
        f"bytes {UPLOAD_CHUNK_SIZE}-{total - 1}/{total}",
    ]
    assert cache.sets and cache.sets[0][1] == SESSION and cache.sets[0][2] == UPLOAD_SESSION_TTL


def test_retry_resumes_the_saved_session_from_the_received_offset(video):
    total = video.stat().st_size
    cache = MemoryCache()
    cache.store[YouTubeProvider._upload_session_key("tok", "brightbean-1", str(video), total)] = SESSION
    calls = []

    def fake(self, method, url, **kwargs):
        calls.append((method, url, kwargs))
        rng = kwargs["headers"]["Content-Range"]
        if rng.startswith("bytes */"):
            return _resp(308, {"Range": f"bytes=0-{UPLOAD_CHUNK_SIZE - 1}"})
        return _resp(200, body={"id": "vid2"})

    with patch.object(YouTubeProvider, "_request", fake):
        result = YouTubeProvider().publish_post("tok", _content(video, cache))

    assert result.platform_post_id == "vid2"
    # No new session was opened, and only the missing tail was sent.
    assert all(c[0] == "PUT" and c[1] == SESSION for c in calls)
    assert len(calls) == 2
    assert calls[1][2]["headers"]["Content-Range"] == f"bytes {UPLOAD_CHUNK_SIZE}-{total - 1}/{total}"
    assert len(calls[1][2]["data"]) == 1000


def test_a_dropped_chunk_is_recovered_by_querying_the_range(video):
    state = {"chunk_calls": 0}

    def fake(self, method, url, **kwargs):
        if method == "POST":
            return _resp(headers={"Location": SESSION})
        rng = kwargs["headers"]["Content-Range"]
        if rng.startswith("bytes */"):
            # Nothing before the drop; after it, the first chunk is held.
            if state["chunk_calls"] == 0:
                return _resp(308)
            return _resp(308, {"Range": f"bytes=0-{UPLOAD_CHUNK_SIZE - 1}"})
        state["chunk_calls"] += 1
        if state["chunk_calls"] == 1:
            # Transport error mid-chunk: base marks it ambiguous, but the loop recovers.
            raise APIError("lost", status_code=None, ambiguous_write=True)
        return _resp(201, body={"id": "vid3"})

    with patch.object(YouTubeProvider, "_request", fake):
        result = YouTubeProvider().publish_post("tok", _content(video, MemoryCache()))
    assert result.platform_post_id == "vid3"


def test_an_expired_session_starts_a_new_one(video):
    total = video.stat().st_size
    cache = MemoryCache()
    cache.store[YouTubeProvider._upload_session_key("tok", "brightbean-1", str(video), total)] = "https://old/session"
    posts = []

    def fake(self, method, url, **kwargs):
        if url == "https://old/session":
            raise APIError("gone", status_code=404)
        if method == "POST":
            posts.append(url)
            return _resp(headers={"Location": SESSION})
        rng = kwargs["headers"]["Content-Range"]
        if rng.startswith("bytes */"):
            return _resp(308)
        if rng.startswith("bytes 0-"):
            return _resp(308, {"Range": f"bytes=0-{UPLOAD_CHUNK_SIZE - 1}"})
        return _resp(201, body={"id": "vid4"})

    with patch.object(YouTubeProvider, "_request", fake):
        result = YouTubeProvider().publish_post("tok", _content(video, cache))
    assert result.platform_post_id == "vid4"
    assert len(posts) == 1


def test_exhausted_attempts_raise_non_ambiguous_so_the_engine_retries(video):
    def fake(self, method, url, **kwargs):
        if method == "POST":
            return _resp(headers={"Location": SESSION})
        raise APIError("boom", status_code=503, ambiguous_write=True)

    with patch.object(YouTubeProvider, "_request", fake), pytest.raises(APIError) as caught:
        YouTubeProvider().publish_post("tok", _content(video, MemoryCache()))
    assert caught.value.ambiguous_write is False


def test_a_permanent_rejection_is_not_retried(video):
    attempts = []

    def fake(self, method, url, **kwargs):
        if method == "POST":
            return _resp(headers={"Location": SESSION})
        attempts.append(1)
        raise APIError("bad", status_code=400)

    with patch.object(YouTubeProvider, "_request", fake), pytest.raises(APIError):
        YouTubeProvider().publish_post("tok", _content(video, MemoryCache()))
    assert len(attempts) == 1


def test_a_finished_upload_without_a_video_id_is_not_a_success(video):
    def fake(self, method, url, **kwargs):
        if method == "POST":
            return _resp(headers={"Location": SESSION})
        rng = kwargs["headers"]["Content-Range"]
        if rng.startswith("bytes */"):
            return _resp(308)
        return (
            _resp(201, body={})
            if "-" in rng and rng.endswith(f"{video.stat().st_size - 1}/{video.stat().st_size}")
            else _resp(308, {"Range": f"bytes=0-{UPLOAD_CHUNK_SIZE - 1}"})
        )

    with patch.object(YouTubeProvider, "_request", fake), pytest.raises(APIError) as caught:
        YouTubeProvider().publish_post("tok", _content(video, MemoryCache()))
    assert caught.value.ambiguous_write is True


def test_missing_session_location_is_a_publish_error(video):
    def fake(self, method, url, **kwargs):
        return _resp(headers={})

    with patch.object(YouTubeProvider, "_request", fake), pytest.raises(PublishError):
        YouTubeProvider().publish_post("tok", _content(video, MemoryCache()))


def test_no_cache_still_uploads(video):
    def fake(self, method, url, **kwargs):
        if method == "POST":
            return _resp(headers={"Location": SESSION})
        rng = kwargs["headers"]["Content-Range"]
        if rng.startswith("bytes */"):
            return _resp(308)
        if rng.startswith("bytes 0-"):
            return _resp(308, {"Range": f"bytes=0-{UPLOAD_CHUNK_SIZE - 1}"})
        return _resp(201, body={"id": "vid5"})

    with patch.object(YouTubeProvider, "_request", fake):
        content = _content(video, None, key=None)
        assert YouTubeProvider().publish_post("tok", content).platform_post_id == "vid5"
