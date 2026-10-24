"""Dub, Short.io and Kutt with the customer's own key; HTTP is mocked, nothing leaves the machine."""

from __future__ import annotations

import json

import httpx
import pytest
from django.db import connection

from apps.links import services, shorteners
from apps.links.models import LinkSettings, ShortLink

from .conftest import configure

pytestmark = pytest.mark.django_db

DEST = "https://example.com/landing"


def body(request):
    return json.loads(request.content)


class TestDub:
    def test_creates_with_bearer_key_and_domain(self, workspace, mock_http):
        configure(workspace, provider="dub", api_key="dub_secret", provider_domain="go.example.com")
        mock_http.reply(
            lambda r: httpx.Response(200, json={"id": "l1", "key": "k1", "shortLink": "https://go.example.com/k1"})
        )
        link = services.create_short_link(workspace, DEST)
        request = mock_http.calls[0]
        assert (request.method, str(request.url)) == ("POST", "https://api.dub.co/links")
        assert request.headers["Authorization"] == "Bearer dub_secret"
        assert body(request) == {"url": DEST, "domain": "go.example.com"}
        assert (link.provider, link.short_url, link.external_id) == ("dub", "https://go.example.com/k1", "l1")

    def test_clicks_come_from_links_info(self, workspace, mock_http):
        configure(workspace, provider="dub", api_key="dub_secret")
        link = ShortLink.objects.create(
            workspace=workspace,
            code="k1",
            destination_url=DEST,
            short_url="https://dub.sh/k1",
            provider="dub",
            external_id="l1",
        )
        mock_http.reply(lambda r: httpx.Response(200, json={"clicks": 42}))
        assert shorteners.shortener_for(services.get_settings(workspace)).clicks(link) == 42
        assert str(mock_http.calls[0].url) == "https://api.dub.co/links/info?linkId=l1"


class TestShortio:
    def test_secret_key_goes_in_authorization_without_bearer(self, workspace, mock_http):
        configure(workspace, provider="shortio", api_key="sk_live", provider_domain="links.example.com")
        mock_http.reply(
            lambda r: httpx.Response(
                200, json={"idString": "abc", "path": "p1", "shortURL": "https://links.example.com/p1"}
            )
        )
        link = services.create_short_link(workspace, DEST)
        request = mock_http.calls[0]
        assert str(request.url) == "https://api.short.io/links" and request.headers["Authorization"] == "sk_live"
        assert body(request) == {"originalURL": DEST, "domain": "links.example.com"}
        assert (link.short_url, link.external_id, link.code) == ("https://links.example.com/p1", "abc", "p1")

    def test_a_domain_is_required(self, workspace):
        with pytest.raises(services.LinkError, match="domain"):
            configure(workspace, provider="shortio", api_key="sk_live")

    def test_clicks_come_from_the_statistics_service(self, workspace, mock_http):
        configure(workspace, provider="shortio", api_key="sk_live", provider_domain="links.example.com")
        link = ShortLink.objects.create(
            workspace=workspace,
            code="p1",
            destination_url=DEST,
            short_url="https://links.example.com/p1",
            provider="shortio",
            external_id="abc",
        )
        mock_http.reply(lambda r: httpx.Response(200, json={"totalClicks": 7}))
        assert shorteners.shortener_for(services.get_settings(workspace)).clicks(link) == 7
        assert str(mock_http.calls[0].url).startswith("https://statistics.short.io/statistics/link/abc")


class TestKutt:
    def test_api_key_header_and_default_host(self, workspace, mock_http):
        configure(workspace, provider="kutt", api_key="kutt_key")
        mock_http.reply(
            lambda r: httpx.Response(201, json={"id": "uuid-1", "link": "https://kutt.it/xyz", "address": "xyz"})
        )
        link = services.create_short_link(workspace, DEST)
        request = mock_http.calls[0]
        assert str(request.url) == "https://kutt.it/api/v2/links" and request.headers["X-API-KEY"] == "kutt_key"
        assert body(request) == {"target": DEST, "reuse": False}
        assert (link.short_url, link.external_id) == ("https://kutt.it/xyz", "uuid-1")

    def test_self_hosted_must_be_public_https(self, workspace, mock_http, monkeypatch):
        monkeypatch.setattr(shorteners, "is_safe_url", lambda url: "internal" not in url)
        configure(workspace, provider="kutt", api_key="k", provider_base_url="https://kutt.example.com/api/v2")
        mock_http.reply(lambda r: httpx.Response(201, json={"id": "1", "link": "https://kutt.example.com/a"}))
        assert services.create_short_link(workspace, DEST).short_url == "https://kutt.example.com/a"
        configure(workspace, provider="kutt", api_key="k", provider_base_url="https://internal.local/api/v2")
        with pytest.raises(shorteners.ShortenerError, match="public"):
            services.create_short_link(workspace, DEST)
        with pytest.raises(services.LinkError, match="https"):
            configure(workspace, provider="kutt", api_key="k", provider_base_url="http://kutt.example.com/api/v2")


class TestErrors:
    @pytest.mark.parametrize(
        ("status", "message"),
        [(401, "refused the API key"), (403, "refused the API key"), (429, "rate limiting"), (500, r"error \(500\)")],
    )
    def test_errors_are_readable_and_never_contain_the_key(self, workspace, mock_http, status, message):
        configure(workspace, provider="dub", api_key="super-secret-key")
        mock_http.reply(lambda r: httpx.Response(status, text="boom super-secret-key"))
        with pytest.raises(shorteners.ShortenerError, match=message) as caught:
            services.create_short_link(workspace, DEST)
        assert "super-secret-key" not in str(caught.value)
        assert not ShortLink.objects.exists()

    def test_unreachable_service(self, workspace, mock_http):
        configure(workspace, provider="dub", api_key="k")

        def boom(request):
            raise httpx.ConnectError("down")

        mock_http.reply(boom)
        with pytest.raises(shorteners.ShortenerError, match="could not be reached"):
            services.create_short_link(workspace, DEST)

    def test_a_reply_without_a_short_link_is_an_error(self, workspace, mock_http):
        configure(workspace, provider="dub", api_key="k")
        mock_http.reply(lambda r: httpx.Response(200, json={"id": "x"}))
        with pytest.raises(shorteners.ShortenerError, match="did not return"):
            services.create_short_link(workspace, DEST)

    def test_an_external_service_without_a_key_falls_back_to_the_built_in_one(self, workspace, mock_http):
        configure(workspace, provider="dub")
        link = services.create_short_link(workspace, DEST)
        assert link.provider == "builtin" and mock_http.calls == []


class TestKeyHandling:
    def test_key_is_encrypted_at_rest(self, workspace):
        configure(workspace, provider="dub", api_key="plain-text-key-123")
        with connection.cursor() as cursor:
            cursor.execute("select api_key from links_settings")
            stored = cursor.fetchone()[0]
        assert "plain-text-key-123" not in stored
        assert LinkSettings.objects.get().api_key == "plain-text-key-123"

    def test_blank_keeps_the_key_and_clear_removes_it(self, workspace):
        configure(workspace, provider="dub", api_key="k1")
        services.update_settings(workspace, api_key="", utm_medium="social")
        assert services.get_settings(workspace).api_key == "k1"
        services.update_settings(workspace, clear_api_key=True)
        assert services.get_settings(workspace).api_key == ""

    def test_generic_code_never_names_a_provider(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        for name in ("services.py", "views.py", "signals.py", "public_views.py"):
            source = (root / name).read_text().lower()
            for word in ("dub", "short.io", "shortio", "kutt"):
                assert word not in source, f"{name} mentions {word}"
