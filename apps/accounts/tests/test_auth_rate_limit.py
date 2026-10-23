"""AuthRateLimitMiddleware has to count the client, not whatever the client claims to be.

It used to key on the first ``X-Forwarded-For`` value, which the client
writes itself: a new value per attempt and the limit never applied. It now
uses the Agent API's proxy-aware IP (``BB_TRUSTED_PROXIES``).
"""

import logging

import pytest
from django.urls import reverse

from apps.accounts.apps import AccountsConfig
from apps.accounts.middleware import AUTH_RATE_LIMIT


def _attempt(client, **meta):
    return client.post(reverse("account_login"), {"login": "nobody@example.com", "password": "wrong"}, **meta)


@pytest.mark.django_db
class TestClientIp:
    def test_rotating_forwarded_for_does_not_escape_the_limit(self, settings, client):
        settings.BB_TRUSTED_PROXIES = ()
        for n in range(AUTH_RATE_LIMIT):
            assert (
                _attempt(client, REMOTE_ADDR="203.0.113.7", HTTP_X_FORWARDED_FOR=f"198.51.100.{n}").status_code != 429
            )

        response = _attempt(client, REMOTE_ADDR="203.0.113.7", HTTP_X_FORWARDED_FOR="198.51.100.250")

        assert response.status_code == 429

    def test_behind_a_trusted_proxy_the_hop_it_appended_counts(self, settings, client):
        """The client can prepend values to the header; the proxy's own entry is the last one."""
        settings.BB_TRUSTED_PROXIES = ("10.0.0.1",)
        for n in range(AUTH_RATE_LIMIT):
            _attempt(client, REMOTE_ADDR="10.0.0.1", HTTP_X_FORWARDED_FOR=f"198.51.100.{n}, 203.0.113.7")

        response = _attempt(client, REMOTE_ADDR="10.0.0.1", HTTP_X_FORWARDED_FOR="198.51.100.250, 203.0.113.7")

        assert response.status_code == 429

    def test_behind_a_trusted_proxy_clients_get_their_own_limit(self, settings, client):
        settings.BB_TRUSTED_PROXIES = ("10.0.0.1",)
        for _ in range(AUTH_RATE_LIMIT):
            _attempt(client, REMOTE_ADDR="10.0.0.1", HTTP_X_FORWARDED_FOR="203.0.113.7")

        response = _attempt(client, REMOTE_ADDR="10.0.0.1", HTTP_X_FORWARDED_FOR="203.0.113.8")

        assert response.status_code != 429

    def test_behind_cloudflare_and_caddy_each_visitor_counts_separately(self, settings, client):
        """Cloudflare -> Caddy -> app: Caddy's Docker address changes and the edge hop varies, so both are ranges."""
        settings.BB_TRUSTED_PROXIES = ("172.16.0.0/12", "104.16.0.0/13")
        for _ in range(AUTH_RATE_LIMIT):
            _attempt(client, REMOTE_ADDR="172.18.0.5", HTTP_X_FORWARDED_FOR="203.0.113.7, 104.16.1.1")

        other_visitor = _attempt(client, REMOTE_ADDR="172.18.0.9", HTTP_X_FORWARDED_FOR="203.0.113.8, 104.23.9.9")
        same_visitor = _attempt(client, REMOTE_ADDR="172.18.0.9", HTTP_X_FORWARDED_FOR="203.0.113.7, 104.23.9.9")

        assert other_visitor.status_code != 429
        assert same_visitor.status_code == 429

    def test_malformed_trusted_entry_is_ignored(self, settings, client):
        settings.BB_TRUSTED_PROXIES = ("not-an-ip", "10.0.0.0/8")
        for _ in range(AUTH_RATE_LIMIT):
            _attempt(client, REMOTE_ADDR="10.0.0.1", HTTP_X_FORWARDED_FOR="203.0.113.7")

        response = _attempt(client, REMOTE_ADDR="10.0.0.1", HTTP_X_FORWARDED_FOR="203.0.113.8")

        assert response.status_code != 429


class TestProcessLocalWarning:
    def test_warns_when_the_cache_is_per_process(self, settings, caplog):
        settings.RATE_LIMIT_CACHE_IS_PROCESS_LOCAL = True

        with caplog.at_level(logging.WARNING, logger="apps.accounts.apps"):
            AccountsConfig._warn_if_rate_limits_are_process_local()

        assert "REDIS_URL is not set" in caplog.text

    def test_quiet_when_the_cache_is_shared(self, settings, caplog):
        settings.RATE_LIMIT_CACHE_IS_PROCESS_LOCAL = False

        with caplog.at_level(logging.WARNING, logger="apps.accounts.apps"):
            AccountsConfig._warn_if_rate_limits_are_process_local()

        assert caplog.text == ""
