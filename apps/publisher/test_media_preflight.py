"""The public-URL preflight for networks that download media from our links."""

import time
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import httpx
import pytest

from apps.publisher.media_preflight import check_public_media_urls
from providers.exceptions import PublishError

URL = "https://cdn.example.com/media/a.jpg"


@pytest.fixture(autouse=True)
def preflight_on(settings):
    settings.PUBLISH_MEDIA_PREFLIGHT = True


def _resp(status=200, content_type="image/jpeg"):
    resp = MagicMock()
    resp.status_code = status
    resp.headers = {"content-type": content_type}
    return resp


def _check(url=URL, kind="image", response=None, public=True):
    with (
        patch("apps.publisher.media_preflight.is_safe_url", return_value=public),
        patch("apps.publisher.media_preflight.httpx.get", return_value=response or _resp()) as get,
    ):
        check_public_media_urls([url], [kind], platform="Instagram")
    return get


def test_a_reachable_image_passes_and_uses_a_ranged_get():
    get = _check()
    assert get.call_args.kwargs["headers"] == {"Range": "bytes=0-0"}
    assert get.call_args.kwargs["follow_redirects"] is False


def test_partial_content_and_generic_object_store_types_pass():
    _check(response=_resp(206))
    _check(response=_resp(200, "application/octet-stream"))
    _check(kind="video", response=_resp(200, "video/mp4"))


def test_private_address_fails_with_the_app_url_hint():
    with pytest.raises(PublishError) as caught:
        _check(url="http://localhost:8000/media/a.jpg", public=False)
    assert "APP_URL" in str(caught.value)
    assert caught.value.retryable is False


def test_a_missing_file_is_a_permanent_failure():
    with pytest.raises(PublishError) as caught:
        _check(response=_resp(404))
    assert "404" in str(caught.value)
    assert caught.value.retryable is False


def test_a_server_error_is_retryable():
    with pytest.raises(PublishError) as caught:
        _check(response=_resp(503))
    assert caught.value.retryable is True


def test_an_unreachable_host_is_retryable():
    with (
        patch("apps.publisher.media_preflight.is_safe_url", return_value=True),
        patch("apps.publisher.media_preflight.httpx.get", side_effect=httpx.ConnectTimeout("t")),
        pytest.raises(PublishError) as caught,
    ):
        check_public_media_urls([URL], ["image"], platform="Instagram")
    assert caught.value.retryable is True


def test_html_served_for_an_image_is_refused():
    with pytest.raises(PublishError) as caught:
        _check(response=_resp(200, "text/html; charset=utf-8"))
    assert "text/html" in str(caught.value)


def test_redirects_are_left_to_the_platform():
    _check(response=_resp(302, "text/html"))


def _signed(seconds_left):
    issued = datetime.fromtimestamp(time.time(), UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{URL}?X-Amz-Date={issued}&X-Amz-Expires={seconds_left}&X-Amz-Signature=abc"


def test_a_signed_link_about_to_expire_is_refused_without_a_request():
    with (
        patch("apps.publisher.media_preflight.is_safe_url", return_value=True),
        patch("apps.publisher.media_preflight.httpx.get") as get,
        pytest.raises(PublishError) as caught,
    ):
        check_public_media_urls([_signed(120)], ["image"], platform="Instagram")
    assert "expires" in str(caught.value)
    get.assert_not_called()


def test_a_signed_link_with_time_left_passes():
    _check(url=_signed(3000))


def test_names_the_failing_item_in_a_carousel():
    with (
        patch("apps.publisher.media_preflight.is_safe_url", return_value=True),
        patch("apps.publisher.media_preflight.httpx.get", side_effect=[_resp(), _resp(403)]),
        pytest.raises(PublishError) as caught,
    ):
        check_public_media_urls([URL, URL + "2"], ["image", "image"], platform="Instagram")
    assert "Media item 2 of 2" in str(caught.value)


def test_can_be_switched_off(settings):
    settings.PUBLISH_MEDIA_PREFLIGHT = False
    with patch("apps.publisher.media_preflight.httpx.get") as get:
        check_public_media_urls(["http://localhost/x.jpg"], ["image"], platform="Instagram")
    get.assert_not_called()
