"""The public page: rendering, safety headers, no cookies, caching, click beacon, custom domains."""

from __future__ import annotations

import datetime as dt

import pytest
from django.test import Client
from django.utils import timezone

from apps.billing.tests.support import set_plan
from apps.composer.models import PlatformPost, Post, PostMedia
from apps.linkinbio import services
from apps.linkinbio.models import BioDailyStat
from apps.media_library.models import MediaAsset

pytestmark = pytest.mark.django_db

BROWSER = {"HTTP_USER_AGENT": "Mozilla/5.0 (X11; Linux) Firefox/130.0"}


def get(slug="acme", **extra):
    return Client().get(f"/l/{slug}/", **{**BROWSER, **extra})


class TestRender:
    def test_shows_title_bio_and_visible_links_only(self, page):
        now = timezone.now()
        services.add_link(page, title="Shop <b>now</b>", url="https://shop.example.com/?a=1&b=2", icon="shop")
        services.add_link(page, title="Soon", url="https://example.com/soon", starts_at=now + dt.timedelta(days=1))
        services.add_link(page, title="Off", url="https://example.com/off", is_active=False)
        html = get().content.decode()
        assert "Acme" in html and "Coffee, roasted daily." in html
        assert "Shop &lt;b&gt;now&lt;/b&gt;" in html and "<b>now</b>" not in html
        assert 'href="https://shop.example.com/?a=1&amp;b=2"' in html
        assert "Soon" not in html and "Off" not in html

    def test_outbound_links_are_marked_and_nothing_runs_inline(self, page, instagram):
        services.add_link(page, title="Shop", url="https://shop.example.com/")
        services.update_page(page, social_account_ids=[instagram.id])
        html = get().content.decode()
        assert html.count('rel="noopener nofollow ugc"') == 2
        assert "javascript:" not in html
        assert " style=" not in html and "<style" not in html
        assert " onclick=" not in html
        # Every script is an external file served by this site.
        assert "<script>" not in html and 'src="/static/js/linkinbio.js"' in html

    def test_theme_accent_comes_from_a_stylesheet(self, page):
        services.update_page(page, accent_color="#112233", theme="midnight")
        assert "bio-theme-midnight" in get().content.decode()
        css = Client().get("/l/acme/theme.css")
        assert css["Content-Type"].startswith("text/css")
        assert "--bio-accent:#112233" in css.content.decode() and "--bio-accent-ink:#ffffff" in css.content.decode()

    def test_dark_text_on_a_light_accent(self, page):
        services.update_page(page, accent_color="#FFEE00")
        assert "--bio-accent-ink:#111111" in Client().get("/l/acme/theme.css").content.decode()

    def test_unpublished_or_unknown_pages_are_404(self, page):
        assert get("nope").status_code == 404
        services.update_page(page, is_published=False)
        assert get().status_code == 404
        assert Client().get("/l/acme/theme.css").status_code == 404

    def test_social_icons_row(self, page, instagram):
        services.update_page(page, social_account_ids=[instagram.id])
        html = get().content.decode()
        assert 'href="https://www.instagram.com/acme.coffee/"' in html and "Instagram" in html

    def test_badge_follows_the_plan(self, page, settings, workspace):
        assert "Made with PostDelegate" not in get().content.decode()
        settings.BILLING_ENABLED = True
        set_plan(workspace.organization, "free")
        services.invalidate("acme")
        assert "Made with PostDelegate" in get().content.decode()
        set_plan(workspace.organization, "creator")
        services.invalidate("acme")
        assert "Made with PostDelegate" not in get().content.decode()

    def test_page_is_cached_briefly_but_views_still_count(self, page):
        get()
        cached = get().content
        services.add_link(page, title="late", url="https://example.com/late")
        # The edit cleared the cache, so the next request sees it.
        fresh = get().content
        assert b"late" in fresh and b"late" not in cached
        assert BioDailyStat.objects.get(kind="view").count == 3

    def test_cached_request_does_no_render_queries(self, page, django_assert_max_num_queries):
        get()
        with django_assert_max_num_queries(6):
            get()


class TestLatestPostsGrid:
    def make_post(self, workspace, account, image, caption, minutes_ago):
        post = Post.objects.create(workspace=workspace, caption=caption)
        PlatformPost.objects.create(
            post=post,
            social_account=account,
            status=PlatformPost.Status.PUBLISHED,
            published_at=timezone.now() - dt.timedelta(minutes=minutes_ago),
        )
        PostMedia.objects.create(post=post, media_asset=image, position=0, alt_text=f"alt {caption}")
        return post

    def test_grid_is_newest_first_and_links_only_when_attached(self, page, workspace, instagram, image):
        first = self.make_post(workspace, instagram, image, "older", 60)
        self.make_post(workspace, instagram, image, "newer", 5)
        text_only = Post.objects.create(workspace=workspace, caption="no media")
        PlatformPost.objects.create(
            post=text_only, social_account=instagram, status="published", published_at=timezone.now()
        )
        services.set_post_link(workspace, first.id, "https://shop.example.com/older")
        services.update_page(page, show_latest_posts=True)
        tiles = services.latest_posts(page)
        assert [t["alt"] for t in tiles] == ["alt newer", "alt older"]
        assert tiles[0]["url"] == "" and tiles[1]["url"] == "https://shop.example.com/older"
        html = get().content.decode()
        assert html.count('class="bio-tile"') == 2 and html.count('rel="noopener nofollow ugc"') == 1

    def test_hidden_unless_switched_on_and_filtered_by_account(self, page, workspace, instagram, image):
        from apps.analytics.tests.helpers import make_account

        other = make_account(workspace, "instagram", "Other IG")
        self.make_post(workspace, other, image, "other", 5)
        assert "bio-grid" not in get().content.decode()
        services.update_page(page, show_latest_posts=True, posts_account=instagram.id)
        assert services.latest_posts(page) == []

    def test_post_link_must_be_http_and_in_workspace(self, page, workspace, instagram, image):
        post = self.make_post(workspace, instagram, image, "p", 5)
        with pytest.raises(services.BioError):
            services.set_post_link(workspace, post.id, "javascript:alert(1)")
        other = type(workspace).objects.create(organization=workspace.organization, name="Other")
        with pytest.raises(services.BioError):
            services.set_post_link(other, post.id, "https://example.com")
        services.set_post_link(workspace, post.id, "https://example.com/x")
        assert services.set_post_link(workspace, post.id, "") is None
        assert not MediaAsset.objects.none().exists() and not hasattr(Post.objects.get(pk=post.id), "bio_link")


class TestPrivacy:
    def test_no_cookies_are_set_and_no_address_is_kept(self, page):
        response = get(REMOTE_ADDR="203.0.113.50")
        assert response.status_code == 200
        assert not response.cookies
        assert "Set-Cookie" not in response
        from apps.linkinbio.models import BioDailyStat as Stat

        values = [str(v) for row in Stat.objects.values() for v in row.values()]
        assert not any("203.0.113.50" in v or "Firefox" in v for v in values)

    def test_the_click_beacon_sets_no_cookie_either(self, page):
        link = services.add_link(page, title="a", url="https://example.com/a")
        response = Client().post("/l/acme/click/", {"l": str(link.id)}, **BROWSER)
        assert response.status_code == 204 and not response.cookies


class TestClickBeacon:
    def test_counts_a_click_without_csrf(self, page):
        link = services.add_link(page, title="a", url="https://example.com/a")
        client = Client(enforce_csrf_checks=True)
        assert client.post("/l/acme/click/", {"l": str(link.id)}, **BROWSER).status_code == 204
        assert BioDailyStat.objects.get(kind="link").count == 1

    def test_rejects_get_and_garbage(self, page):
        assert Client().get("/l/acme/click/").status_code == 405
        assert Client().post("/l/acme/click/", {"l": "nope"}, **BROWSER).status_code == 404

    def test_is_rate_limited(self, page, monkeypatch):
        monkeypatch.setattr(services, "CLICKS_PER_VISITOR_PER_MINUTE", 2)
        link = services.add_link(page, title="a", url="https://example.com/a")
        codes = [Client().post("/l/acme/click/", {"l": str(link.id)}, **BROWSER).status_code for _ in range(4)]
        assert codes == [204, 204, 429, 429]


class TestCustomDomains:
    def test_off_by_default(self, page):
        services.update_page(page, custom_domain="links.example.com")
        assert b"Coffee, roasted daily." not in Client().get("/", HTTP_HOST="links.example.com").content

    def test_routes_the_domain_to_its_page_when_enabled(self, page, settings):
        settings.LINKINBIO_CUSTOM_DOMAINS_ENABLED = True
        settings.ALLOWED_HOSTS = ["*"]
        services.update_page(page, custom_domain="links.example.com")
        response = Client().get("/", HTTP_HOST="links.example.com", **BROWSER)
        assert response.status_code == 200 and b"Coffee, roasted daily." in response.content
        link = services.add_link(page, title="a", url="https://example.com/a")
        assert (
            Client().post("/click/", {"l": str(link.id)}, HTTP_HOST="links.example.com", **BROWSER).status_code == 204
        )

    def test_unpublished_page_and_unknown_host_are_not_served(self, page, settings):
        settings.LINKINBIO_CUSTOM_DOMAINS_ENABLED = True
        settings.ALLOWED_HOSTS = ["*"]
        services.update_page(page, custom_domain="links.example.com", is_published=False)
        assert b"Coffee" not in Client().get("/", HTTP_HOST="links.example.com").content
        assert b"Coffee" not in Client().get("/", HTTP_HOST="other.example.com").content

    def test_domain_must_look_like_a_hostname_and_be_unique(self, page, workspace):
        for bad in ("localhost", "http://x.com", "a b.com", "-x.com"):
            with pytest.raises(services.BioError):
                services.update_page(page, custom_domain=bad)
        services.update_page(page, custom_domain="Links.Example.com.")
        assert page.custom_domain == "links.example.com"
        set_plan(workspace.organization, "agency")
        second = services.create_page(workspace, title="Two", slug="two")
        with pytest.raises(services.BioError):
            services.update_page(second, custom_domain="links.example.com")
