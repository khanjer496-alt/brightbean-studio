"""``/s/<code>``: redirect, counting without storing anything about the visitor, bots, previews, limits."""

from __future__ import annotations

import pytest
from django.core.cache import cache
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from apps.links import services
from apps.links.models import LinkDailyStat, ShortLink

from .conftest import BROWSER

pytestmark = pytest.mark.django_db


@pytest.fixture
def link(workspace):
    return services.create_short_link(workspace, "https://example.com/landing?a=1")


def visit(code, **extra):
    return Client().get(f"/s/{code}", **{**BROWSER, **extra})


def stat(link, kind="click"):
    return LinkDailyStat.objects.filter(link=link, kind=kind).first()


class TestRedirect:
    def test_permanent_redirect_to_the_destination_and_nothing_cached(self, link):
        response = visit(link.code)
        assert response.status_code == 301
        assert response["Location"] == "https://example.com/landing?a=1"
        assert response["Cache-Control"] == "no-store"
        assert response["Referrer-Policy"] == "no-referrer"
        assert not response.cookies

    def test_short_url_lives_on_the_app_address(self, link, settings):
        assert link.short_url == f"{settings.APP_URL.rstrip('/')}/s/{link.code}"
        assert link.provider == "builtin" and len(link.code) == 7

    def test_unknown_and_disabled_links_are_404(self, link):
        assert visit("nope123").status_code == 404
        services.set_active(link, False)
        assert visit(link.code).status_code == 404
        assert stat(link) is None

    def test_external_links_are_not_served_here(self, workspace):
        other = ShortLink.objects.create(
            workspace=workspace,
            code="abc1234",
            destination_url="https://example.com/",
            short_url="https://dub.sh/abc1234",
            provider="dub",
        )
        assert visit(other.code).status_code == 404

    def test_only_web_addresses_are_ever_followed(self, workspace):
        bad = ShortLink.objects.create(
            workspace=workspace,
            code="evil123",
            destination_url="javascript:alert(1)",
            short_url="x",
            provider="builtin",
        )
        assert visit(bad.code).status_code == 404

    def test_destinations_must_be_web_addresses_at_creation(self, workspace):
        for bad in (
            "javascript:alert(1)",
            "ftp://example.com/x",
            "//example.com",
            "https://u:p@example.com/",
            "not a url",
        ):
            with pytest.raises(services.LinkError):
                services.create_short_link(workspace, bad)

    def test_a_short_link_cannot_point_at_another_short_link(self, workspace, link):
        with pytest.raises(services.LinkError):
            services.create_short_link(workspace, link.short_url)

    def test_post_is_not_allowed(self, link):
        assert Client().post(f"/s/{link.code}", **BROWSER).status_code == 405


class TestCounting:
    def test_counts_clicks_and_estimates_uniques(self, link):
        visit(link.code)
        visit(link.code)
        visit(link.code, HTTP_USER_AGENT="Mozilla/5.0 (Macintosh) Safari/605")
        row = stat(link)
        assert row.count == 3 and row.uniques == 2

    def test_head_requests_do_not_count(self, link):
        Client().head(f"/s/{link.code}", **BROWSER)
        assert stat(link) is None

    def test_crawlers_and_scripts_are_not_counted_but_still_redirected(self, link):
        for agent in (
            "Googlebot/2.1 (+http://www.google.com/bot.html)",
            "curl/8.0",
            "python-requests/2.31",
            "AhrefsBot",
            "",
        ):
            response = visit(link.code, HTTP_USER_AGENT=agent)
            assert response.status_code == 301
        assert not LinkDailyStat.objects.exists()

    def test_platform_link_previews_are_counted_apart(self, link):
        for agent in (
            "facebookexternalhit/1.1",
            "Twitterbot/1.0",
            "LinkedInBot/1.0 (compatible; Mozilla/5.0)",
            "Slackbot-LinkExpanding 1.0",
            "Mozilla/5.0 (compatible; Discordbot/2.0)",
            "WhatsApp/2.23",
        ):
            assert visit(link.code, HTTP_USER_AGENT=agent).status_code == 301
        assert stat(link, "preview").count == 6
        assert stat(link) is None

    def test_no_address_or_browser_string_reaches_the_database(self, link):
        visit(link.code, REMOTE_ADDR="203.0.113.77", HTTP_USER_AGENT="Mozilla/5.0 UniqueAgentMarker/9")
        with connection.cursor() as cursor:
            cursor.execute("select * from links_daily_stat")
            columns = [c.name for c in cursor.description]
            rows = cursor.fetchall()
        assert columns == ["id", "day", "kind", "count", "uniques", "link_id"]
        flat = repr(rows) + repr(list(ShortLink.objects.values()))
        assert "203.0.113.77" not in flat and "UniqueAgentMarker" not in flat

    def test_unique_estimate_does_not_survive_the_day_salt(self, link):
        visit(link.code, REMOTE_ADDR="203.0.113.5")
        cache.clear()  # the day's salt is gone: the same visitor can't be matched to yesterday's token
        visit(link.code, REMOTE_ADDR="203.0.113.5")
        assert stat(link).uniques == 2

    def test_counting_is_a_single_cheap_write(self, link):
        visit(link.code)
        with CaptureQueriesContext(connection) as queries:
            visit(link.code)
        assert len(queries) <= 6

    def test_stats_summary_reports_clicks_uniques_and_previews(self, workspace, link):
        visit(link.code)
        visit(link.code, HTTP_USER_AGENT="Twitterbot/1.0")
        summary = services.stats_summary(workspace, 7)
        assert summary["totals"] == {"clicks": 1, "unique_visitors": 1, "previews": 1}
        assert summary["links"][0]["link_id"] == link.pk and summary["links"][0]["clicks"] == 1
        assert len(summary["series"]) == 7 and summary["series"][-1]["clicks"] == 1


class TestRateLimits:
    def test_counting_stops_for_a_visitor_but_the_redirect_still_works(self, link, monkeypatch):
        monkeypatch.setattr(services, "CLICKS_PER_VISITOR_PER_MINUTE", 3)
        for _ in range(6):
            assert visit(link.code).status_code == 301
        assert stat(link).count == 3

    def test_a_script_hammering_one_link_gets_429(self, link, monkeypatch):
        monkeypatch.setattr(services, "HARD_LIMIT_PER_VISITOR_PER_MINUTE", 5)
        codes = [visit(link.code).status_code for _ in range(7)]
        assert codes[:5] == [301] * 5 and codes[5:] == [429, 429]

    def test_the_link_as_a_whole_has_a_ceiling(self, link, monkeypatch):
        monkeypatch.setattr(services, "EVENTS_PER_LINK_PER_MINUTE", 2)
        for i in range(5):
            visit(link.code, REMOTE_ADDR=f"203.0.113.{i}", HTTP_USER_AGENT=f"Mozilla/5.0 Browser{i}")
        assert stat(link).count == 2


class TestCustomShortDomain:
    def test_off_by_default_and_served_only_when_enabled(self, workspace, link, settings):
        settings.ALLOWED_HOSTS = ["*"]
        services.update_settings(workspace, custom_short_domain="go.example.com")
        assert Client().get(f"/{link.code}", HTTP_HOST="go.example.com", **BROWSER).status_code == 404
        settings.LINKS_CUSTOM_DOMAINS_ENABLED = True
        cache.clear()
        response = Client().get(f"/{link.code}", HTTP_HOST="go.example.com", **BROWSER)
        assert response.status_code == 301 and response["Location"] == "https://example.com/landing?a=1"
        # nothing else of the app is reachable there
        assert Client().get("/accounts/login/", HTTP_HOST="go.example.com", **BROWSER).status_code == 404

    def test_built_in_links_use_the_custom_domain_when_set(self, workspace):
        services.update_settings(workspace, custom_short_domain="go.example.com")
        made = services.create_short_link(workspace, "https://example.com/x")
        assert made.short_url == f"https://go.example.com/s/{made.code}"
