"""Tests for the RSS feed URL validator (SSRF guard + redirect handling + size cap)."""

from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from apps.composer.views import FEED_MAX_BYTES, _safe_fetch_feed, _validate_rss_url

_VALID_RSS = b"""<?xml version='1.0'?><rss version='2.0'><channel>
<title>Sample</title><link>https://example.com</link></channel></rss>"""


def _public_addrinfo(*_args, **_kwargs):
    return [(0, 0, 0, "", ("8.8.8.8", 0))]


def _private_addrinfo(*_args, **_kwargs):
    return [(0, 0, 0, "", ("10.0.0.1", 0))]


def _streamed(*, status_code, headers, content):
    """A streamed response, as ``httpx.stream(...)`` yields it inside its context manager."""
    response = MagicMock()
    response.status_code = status_code
    response.headers = headers
    response.iter_bytes = MagicMock(return_value=[content] if content else [])
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=response)
    cm.__exit__ = MagicMock(return_value=False)
    return cm


class ValidateRssUrlTests(SimpleTestCase):
    def test_rejects_private_ip_url(self):
        with patch("socket.getaddrinfo", side_effect=_private_addrinfo):
            ok, err, meta = _validate_rss_url("http://internal.example.com/feed")
        self.assertFalse(ok)
        self.assertIn("Could not reach", err)
        self.assertEqual(meta, {})

    def test_rejects_file_scheme_url(self):
        ok, err, _ = _validate_rss_url("file:///etc/passwd")
        self.assertFalse(ok)
        self.assertIn("Could not reach", err)

    def test_follows_absolute_redirect_to_public_host(self):
        with patch("socket.getaddrinfo", side_effect=_public_addrinfo):
            responses = [
                _streamed(status_code=302, headers={"Location": "https://feeds.example.com/rss.xml"}, content=b""),
                _streamed(status_code=200, headers={}, content=_VALID_RSS),
            ]
            with patch("httpx.stream", side_effect=responses) as httpx_get:
                ok, err, _ = _validate_rss_url("https://example.com/feed")
            self.assertTrue(ok, msg=err)
            self.assertEqual(httpx_get.call_count, 2)

    def test_resolves_relative_redirect_location(self):
        """A redirect with `Location: /feed.xml` must resolve against the request URL."""
        with patch("socket.getaddrinfo", side_effect=_public_addrinfo):
            responses = [
                _streamed(status_code=302, headers={"Location": "/feed.xml"}, content=b""),
                _streamed(status_code=200, headers={}, content=_VALID_RSS),
            ]
            with patch("httpx.stream", side_effect=responses) as httpx_get:
                ok, err, _ = _validate_rss_url("https://example.com/blog")
            self.assertTrue(ok, msg=err)
            # Second call must use the absolute resolved URL.
            second_call_url = httpx_get.call_args_list[1].args[1]
            self.assertEqual(second_call_url, "https://example.com/feed.xml")

    def test_resolves_scheme_relative_redirect_location(self):
        """`Location: //feeds.example.com/rss.xml` must resolve via the original scheme."""
        with patch("socket.getaddrinfo", side_effect=_public_addrinfo):
            responses = [
                _streamed(status_code=301, headers={"Location": "//feeds.example.com/rss.xml"}, content=b""),
                _streamed(status_code=200, headers={}, content=_VALID_RSS),
            ]
            with patch("httpx.stream", side_effect=responses) as httpx_get:
                ok, err, _ = _validate_rss_url("https://example.com/feed")
            self.assertTrue(ok, msg=err)
            second_call_url = httpx_get.call_args_list[1].args[1]
            self.assertEqual(second_call_url, "https://feeds.example.com/rss.xml")

    def test_rejects_redirect_to_private_ip(self):
        """A 302 → http://127.0.0.1 must be blocked at the validator."""
        public_then_private = iter(
            [
                [(0, 0, 0, "", ("8.8.8.8", 0))],  # initial public host
                [(0, 0, 0, "", ("127.0.0.1", 0))],  # redirect target
            ]
        )
        with patch("socket.getaddrinfo", side_effect=lambda *a, **k: next(public_then_private)):
            response = _streamed(status_code=302, headers={"Location": "http://localhost/admin"}, content=b"")
            with patch("httpx.stream", return_value=response):
                ok, err, _ = _validate_rss_url("https://example.com/feed")
        self.assertFalse(ok)
        self.assertIn("Could not reach", err)


class SafeFetchSizeAndTypeTests(SimpleTestCase):
    def setUp(self):
        patcher = patch("socket.getaddrinfo", side_effect=_public_addrinfo)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_feeds_up_to_ten_megabytes_are_read(self):
        body = b"x" * (FEED_MAX_BYTES - 1)
        with patch("httpx.stream", return_value=_streamed(status_code=200, headers={}, content=body)):
            response, _ = _safe_fetch_feed("https://example.com/feed", {})
        self.assertEqual(len(response.content), FEED_MAX_BYTES - 1)

    def test_body_over_the_cap_is_refused(self):
        with patch("httpx.stream", return_value=_streamed(status_code=200, headers={}, content=b"x" * 101)):
            self.assertEqual(_safe_fetch_feed("https://example.com/feed", {}, max_bytes=100), (None, None))

    def test_declared_length_over_the_cap_is_refused_before_reading(self):
        cm = _streamed(status_code=200, headers={"content-length": "5000"}, content=b"x")
        with patch("httpx.stream", return_value=cm):
            self.assertEqual(_safe_fetch_feed("https://example.com/feed", {}, max_bytes=100), (None, None))
        cm.__enter__.return_value.iter_bytes.assert_not_called()

    def test_content_type_outside_the_allowed_set_is_refused_before_reading(self):
        cm = _streamed(status_code=200, headers={"content-type": "video/mp4"}, content=b"x")
        with patch("httpx.stream", return_value=cm):
            result = _safe_fetch_feed("https://example.com/v", {}, content_types={"text/html"})
        self.assertEqual(result, (None, None))
        cm.__enter__.return_value.iter_bytes.assert_not_called()

    def test_allowed_content_type_with_charset(self):
        cm = _streamed(status_code=200, headers={"content-type": "text/html; charset=utf-8"}, content=b"<p>hi</p>")
        with patch("httpx.stream", return_value=cm):
            response, _ = _safe_fetch_feed("https://example.com/", {}, content_types={"text/html"})
        self.assertEqual(response.text, "<p>hi</p>")
