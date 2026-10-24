"""Link clicks in the analytics overview, the post detail drawer, and the stats by post / account / link."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.analytics.ranges import local_today, workspace_tzinfo
from apps.analytics.tests.helpers import fill_days, make_account, make_post
from apps.links import services
from apps.links.models import LinkDailyStat

from .conftest import BROWSER

pytestmark = pytest.mark.django_db


def click(link, times=1, **extra):
    path = "/s/" + link.code
    for i in range(times):
        Client().get(path, **{**BROWSER, "REMOTE_ADDR": f"203.0.113.{i + 1}", **extra})


@pytest.fixture
def seeded(workspace):
    today = local_today(workspace_tzinfo(workspace))
    ig = make_account(workspace, "instagram", "IG Main", followers=1200)
    fill_days(ig, "views", today - timedelta(days=13), today, 100)
    published = make_post(workspace, ig, timezone.now() - timedelta(days=2), "A post")
    return ig, published


class TestStatsByPostAccountLink:
    def test_clicks_split_by_post_account_and_link(self, workspace, seeded):
        ig, published = seeded
        a = services.create_short_link(
            workspace, "https://example.com/a", post=published.post, platform_post=published, social_account=ig
        )
        b = services.create_short_link(workspace, "https://example.com/b")
        click(a, 3)
        click(b, 1)
        click(a, 1, HTTP_USER_AGENT="Twitterbot/1.0")
        summary = services.stats_summary(workspace, 7)
        assert summary["totals"] == {"clicks": 4, "unique_visitors": 4, "previews": 1}
        assert [(row["short_url"], row["clicks"]) for row in summary["links"]] == [(a.short_url, 3), (b.short_url, 1)]
        assert summary["posts"] == [{"post_id": published.post_id, "clicks": 3, "unique_clicks": 3}]
        assert summary["accounts"][0]["social_account_id"] == ig.id and summary["accounts"][0]["clicks"] == 3
        only_post = services.stats_summary(workspace, 7, post_ids=[published.post_id])
        assert only_post["totals"]["clicks"] == 3

    def test_old_days_fall_outside_the_range(self, workspace):
        link = services.create_short_link(workspace, "https://example.com/a")
        LinkDailyStat.objects.create(
            link=link, day=timezone.now().date() - timedelta(days=40), kind="click", count=9, uniques=9
        )
        assert services.stats_summary(workspace, 30)["totals"]["clicks"] == 0
        assert services.stats_summary(workspace, 90)["totals"]["clicks"] == 9


class TestOverviewAndPostDetail:
    def test_overview_has_a_link_clicks_section_only_once_links_exist(self, workspace, owner, seeded):
        client = Client()
        client.force_login(owner)
        url = reverse("analytics:index", kwargs={"workspace_id": workspace.id}) + "?range=7"
        assert "Link clicks" not in client.get(url).content.decode()
        ig, published = seeded
        link = services.create_short_link(
            workspace, "https://example.com/a", post=published.post, platform_post=published, social_account=ig
        )
        click(link, 2)
        html = client.get(url).content.decode()
        assert 'id="link-clicks-heading"' in html and link.short_url in html and "IG Main" in html
        assert "Link-preview fetches" in html

    def test_post_detail_shows_clicks_for_that_accounts_links(self, workspace, owner, seeded):
        ig, published = seeded
        client = Client()
        client.force_login(owner)
        url = reverse("analytics:post_detail", kwargs={"workspace_id": workspace.id, "post_id": published.id})
        assert "Link clicks" not in client.get(url).content.decode()
        link = services.create_short_link(
            workspace, "https://example.com/a", post=published.post, platform_post=published, social_account=ig
        )
        click(link, 3)
        click(link, 1, HTTP_USER_AGENT="LinkedInBot/1.0")
        html = client.get(url).content.decode()
        assert "Link clicks" in html and link.short_url in html and "1 link-preview fetch" in html
        data = services.platform_post_clicks(published)
        assert data["clicks"] == 3 and data["previews"] == 1 and data["links"][0]["clicks"] == 3
