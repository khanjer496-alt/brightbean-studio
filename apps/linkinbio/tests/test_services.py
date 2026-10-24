"""Validation, scheduling visibility, ordering, plan limits and the privacy-friendly counters."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from django.core.cache import cache
from django.test import RequestFactory
from django.utils import timezone

from apps.billing.entitlements import PlanLimitError
from apps.billing.tests.support import set_plan
from apps.linkinbio import services
from apps.linkinbio.models import NIL_TARGET, BioDailyStat, BioLink, BioPage

pytestmark = pytest.mark.django_db


def visitor(ip="203.0.113.7", agent="Mozilla/5.0 (X11; Linux) Firefox/130.0", method="get"):
    request = getattr(RequestFactory(), method)("/l/acme/", HTTP_USER_AGENT=agent, REMOTE_ADDR=ip)
    return request


class TestUrlValidation:
    @pytest.mark.parametrize(
        "url",
        [
            "javascript:alert(1)",
            "JaVaScRiPt:alert(1)",
            "data:text/html,<script>alert(1)</script>",
            "vbscript:msgbox(1)",
            "file:///etc/passwd",
            "mailto:a@b.co",
            "//evil.example/x",
            "/relative",
            "https://",
            "https://user:pw@example.com/",
            "https://exa mple.com/",
            "https://example.com/\nSet-Cookie: x=1",
            "https://example.com:notaport/",
            "https://example.com/" + "a" * 2000,
        ],
    )
    def test_refuses_anything_but_http_urls(self, page, url):
        with pytest.raises(services.BioError):
            services.add_link(page, title="Bad", url=url)
        assert page.links.count() == 0

    @pytest.mark.parametrize("url", ["http://example.com", "https://example.com/a?b=c#d", "HTTPS://Example.com/"])
    def test_accepts_http_and_https(self, page, url):
        assert services.add_link(page, title="Ok", url=url).url == url

    def test_a_blank_title_is_refused(self, page):
        with pytest.raises(services.BioError) as exc:
            services.add_link(page, title="  ", url="https://example.com")
        assert exc.value.field == "title"

    def test_link_window_must_be_ordered(self, page):
        now = timezone.now()
        with pytest.raises(services.BioError):
            services.add_link(page, title="x", url="https://example.com", starts_at=now, ends_at=now)

    def test_a_page_has_a_link_cap(self, page, monkeypatch):
        monkeypatch.setattr(services, "MAX_LINKS", 2)
        services.add_link(page, title="1", url="https://example.com/1")
        services.add_link(page, title="2", url="https://example.com/2")
        with pytest.raises(services.BioError):
            services.add_link(page, title="3", url="https://example.com/3")


class TestSlugs:
    @pytest.mark.parametrize(
        "slug",
        [
            "admin",
            "api",
            "static",
            "login",
            "postdelegate",
            "instagram-help",
            "www",
            "ab",
            "-abc",
            "abc-",
            "a--b",
            "UPPER space",
            "a/b",
        ],
    )
    def test_bad_or_reserved_slugs_are_refused(self, workspace, slug):
        with pytest.raises(services.BioError) as exc:
            services.create_page(workspace, title="T", slug=slug)
        assert exc.value.field == "slug"

    def test_slug_is_lowercased_and_unique_across_workspaces(self, workspace, page):
        other = type(workspace).objects.create(organization=workspace.organization, name="Other")
        set_plan(workspace.organization, "ultimate")
        with pytest.raises(services.BioError):
            services.create_page(other, title="Dup", slug="ACME")


class TestVisibility:
    def test_links_show_inside_their_window_only(self, page):
        now = timezone.now()
        services.add_link(page, title="always", url="https://example.com/a")
        services.add_link(page, title="future", url="https://example.com/b", starts_at=now + dt.timedelta(hours=1))
        services.add_link(page, title="expired", url="https://example.com/c", ends_at=now - dt.timedelta(hours=1))
        services.add_link(
            page,
            title="now",
            url="https://example.com/d",
            starts_at=now - dt.timedelta(hours=1),
            ends_at=now + dt.timedelta(hours=1),
        )
        services.add_link(page, title="off", url="https://example.com/e", is_active=False)
        assert [link.title for link in services.visible_links(page, now)] == ["always", "now"]
        later = now + dt.timedelta(hours=2)
        assert [link.title for link in services.visible_links(page, later)] == ["always", "future"]

    def test_end_is_exclusive_and_start_inclusive(self, page):
        moment = timezone.now()
        link = services.add_link(
            page, title="w", url="https://example.com", starts_at=moment, ends_at=moment + dt.timedelta(hours=1)
        )
        assert link.is_visible(moment) and not link.is_visible(moment + dt.timedelta(hours=1))


class TestOrdering:
    def test_move_swaps_with_neighbour_and_stops_at_the_ends(self, page):
        a, b, c = (services.add_link(page, title=t, url=f"https://example.com/{t}") for t in "abc")
        services.move_link(c, "up")
        assert [x.title for x in page.links.all()] == ["a", "c", "b"]
        services.move_link(a, "up")
        assert [x.title for x in page.links.all()] == ["a", "c", "b"]
        services.move_link(a, "down")
        assert [x.title for x in page.links.all()] == ["c", "a", "b"]

    def test_deleting_closes_the_gap(self, page):
        a, b, c = (services.add_link(page, title=t, url=f"https://example.com/{t}") for t in "abc")
        services.delete_link(b)
        assert list(page.links.values_list("position", flat=True)) == [0, 1]


class TestSocialIcons:
    def test_only_connected_accounts_with_a_public_profile(self, page, instagram, workspace):
        from apps.analytics.tests.helpers import make_account

        telegram = make_account(workspace, "telegram", "Chat")
        broken = make_account(workspace, "instagram", "Broken IG", status="disconnected")
        services.update_page(page, social_account_ids=[instagram.id, telegram.id, broken.id])
        [row] = services.social_links(page)
        assert row["label"] == "Instagram" and row["url"] == "https://www.instagram.com/acme.coffee/"

    def test_a_handle_with_markup_gives_no_link(self, page, instagram):
        instagram.account_handle = 'x"><script>'
        instagram.save()
        services.update_page(page, social_account_ids=[instagram.id])
        assert services.social_links(page) == []

    def test_accounts_of_another_workspace_are_refused(self, page, workspace):
        from apps.analytics.tests.helpers import make_account

        other = type(workspace).objects.create(organization=workspace.organization, name="Other")
        foreign = make_account(other, "instagram", "Foreign")
        with pytest.raises(services.BioError):
            services.update_page(page, social_account_ids=[foreign.id])


class TestMedia:
    def test_avatar_must_be_an_image_in_this_workspace(self, page, image, workspace):
        from apps.media_library.models import MediaAsset

        services.update_page(page, avatar=image.id)
        assert page.avatar_id == image.id
        other = type(workspace).objects.create(organization=workspace.organization, name="Other")
        foreign = MediaAsset.objects.create(
            organization=workspace.organization, workspace=other, file="x.jpg", filename="x", media_type="image"
        )
        video = MediaAsset.objects.create(
            organization=workspace.organization, workspace=workspace, file="v.mp4", filename="v", media_type="video"
        )
        for bad in (foreign.id, video.id, "not-a-uuid"):
            with pytest.raises(services.BioError):
                services.update_page(page, avatar=bad)


class TestEntitlements:
    def test_free_plan_has_one_page_with_a_badge(self, settings, workspace):
        settings.BILLING_ENABLED = True
        set_plan(workspace.organization, "free")
        page = services.create_page(workspace, title="One", slug="one")
        assert services.shows_badge(page)
        with pytest.raises(PlanLimitError) as exc:
            services.create_page(workspace, title="Two", slug="two")
        assert exc.value.code == "bio_page_limit"

    def test_paid_plans_have_more_pages_and_no_badge(self, settings, workspace):
        settings.BILLING_ENABLED = True
        set_plan(workspace.organization, "starter")
        pages = [services.create_page(workspace, title=f"P{i}", slug=f"page-{i}") for i in range(3)]
        assert not services.shows_badge(pages[0])
        with pytest.raises(PlanLimitError):
            services.create_page(workspace, title="Four", slug="page-4")
        set_plan(workspace.organization, "ultimate")
        services.create_page(workspace, title="Four", slug="page-4")

    def test_downgrade_keeps_existing_pages(self, settings, workspace):
        set_plan(workspace.organization, "ultimate")
        settings.BILLING_ENABLED = True
        for i in range(3):
            services.create_page(workspace, title=f"P{i}", slug=f"page-{i}")
        set_plan(workspace.organization, "free")
        assert BioPage.objects.filter(workspace=workspace).count() == 3
        with pytest.raises(PlanLimitError):
            services.create_page(workspace, title="More", slug="more")

    def test_self_hosted_has_no_limit_and_no_badge(self, workspace):
        for i in range(12):
            page = services.create_page(workspace, title=f"P{i}", slug=f"page-{i}")
        assert not services.shows_badge(page)


class TestCounting:
    def test_views_are_counted_per_day_without_storing_the_visitor(self, page):
        assert services.record_view(page, visitor())
        assert services.record_view(page, visitor())
        assert services.record_view(page, visitor(ip="198.51.100.9"))
        [row] = BioDailyStat.objects.filter(page=page, kind="view")
        assert (row.count, row.uniques, row.target_id) == (3, 2, NIL_TARGET)
        # Nothing in the table can identify a visitor.
        stored = {f.name for f in BioDailyStat._meta.get_fields()}
        assert not stored & {"ip", "ip_address", "user_agent", "visitor", "token", "fingerprint"}

    def test_the_token_changes_every_day(self):
        request = visitor()
        day = dt.date(2026, 10, 1)
        assert services._visitor_token(request, day) != services._visitor_token(request, day + dt.timedelta(days=1))
        assert services._visitor_token(request, day) == services._visitor_token(request, day)

    def test_bots_and_non_get_requests_are_not_counted(self, page):
        assert not services.record_view(page, visitor(agent="Googlebot/2.1"))
        assert not services.record_view(page, visitor(agent=""))
        assert not services.record_view(page, visitor(method="head"))
        assert not BioDailyStat.objects.exists()

    def test_clicks_count_per_link(self, page):
        a = services.add_link(page, title="a", url="https://example.com/a")
        b = services.add_link(page, title="b", url="https://example.com/b")
        for link in (a, a, b):
            assert services.record_click(page, link.id, visitor()) == "counted"
        counts = dict(BioDailyStat.objects.filter(kind="link").values_list("target_id", "count"))
        assert counts == {a.id: 2, b.id: 1}
        assert BioDailyStat.objects.get(target_id=a.id).uniques == 1

    def test_unknown_hidden_or_foreign_targets_are_ignored(self, page, workspace):
        hidden = services.add_link(page, title="h", url="https://example.com/h", is_active=False)
        assert services.record_click(page, hidden.id, visitor()) == "ignored"
        assert services.record_click(page, uuid.uuid4(), visitor()) == "ignored"
        other = services.create_page(
            type(workspace).objects.create(organization=workspace.organization, name="O"), title="O", slug="other"
        )
        mine = services.add_link(page, title="m", url="https://example.com/m")
        assert services.record_click(other, mine.id, visitor()) == "ignored"
        assert not BioDailyStat.objects.exists()

    def test_click_rate_limit_per_visitor(self, page, monkeypatch):
        monkeypatch.setattr(services, "CLICKS_PER_VISITOR_PER_MINUTE", 3)
        link = services.add_link(page, title="a", url="https://example.com/a")
        results = [services.record_click(page, link.id, visitor()) for _ in range(5)]
        assert results == ["counted"] * 3 + ["limited"] * 2
        assert BioDailyStat.objects.get(target_id=link.id).count == 3
        # Another visitor is not held back by it.
        assert services.record_click(page, link.id, visitor(ip="198.51.100.1")) == "counted"

    def test_page_wide_rate_limit(self, page, monkeypatch):
        monkeypatch.setattr(services, "EVENTS_PER_PAGE_PER_MINUTE", 2)
        link = services.add_link(page, title="a", url="https://example.com/a")
        results = [services.record_click(page, link.id, visitor(ip=f"198.51.100.{i}")) for i in range(1, 5)]
        assert results == ["counted", "counted", "limited", "limited"]

    def test_stats_summary(self, page):
        link = services.add_link(page, title="a", url="https://example.com/a")
        services.add_link(page, title="unclicked", url="https://example.com/u")
        services.record_view(page, visitor())
        services.record_click(page, link.id, visitor())
        today = timezone.now().astimezone(dt.UTC).date()
        BioDailyStat.objects.create(page=page, day=today - dt.timedelta(days=100), kind="view", count=50)
        summary = services.stats_summary(page, 30)
        assert summary["totals"] == {"views": 1, "unique_visitors": 1, "clicks": 1}
        assert [r["title"] for r in summary["links"]] == ["a", "unclicked"]
        assert len(summary["series"]) == 30 and summary["series"][-1]["views"] == 1
        assert services.stats_summary(page, 500)["days"] == services.STATS_DAYS_MAX

    def test_deleted_links_keep_their_history(self, page):
        link = services.add_link(page, title="gone", url="https://example.com/g")
        services.record_click(page, link.id, visitor())
        services.delete_link(link)
        assert services.stats_summary(page)["links"][0]["title"] == "Deleted link"


def test_cache_is_cleared_by_edits(page):
    cache.set(services.page_cache_key("acme"), "stale")
    services.add_link(page, title="a", url="https://example.com/a")
    assert cache.get(services.page_cache_key("acme")) is None


def test_link_model_validates_through_full_clean(page):
    link = BioLink(page=page, title="x", url="javascript:alert(1)")
    from django.core.exceptions import ValidationError

    with pytest.raises(ValidationError):
        link.full_clean()
