"""The workspace editor: permissions, plan limit, forms that change pages and links."""

from __future__ import annotations

import pytest
from django.urls import reverse

from apps.analytics.tests.helpers import make_member
from apps.billing.tests.support import set_plan
from apps.linkinbio import services
from apps.linkinbio.models import BioLink, BioPage
from apps.members.models import WorkspaceMembership

pytestmark = pytest.mark.django_db


def url(name, workspace, page=None, **kwargs):
    kw = {"workspace_id": workspace.id, **kwargs}
    if page is not None:
        kw["page_id"] = page.id
    return reverse(f"linkinbio:{name}", kwargs=kw)


class TestPermissions:
    def test_anonymous_is_redirected(self, client, workspace):
        assert client.get(url("list", workspace)).status_code == 302

    def test_outsider_and_contributor_are_forbidden(self, client, workspace, page):
        outsider = make_member(
            type(workspace).objects.create(organization=workspace.organization, name="O"), "o@x.test"
        )
        contributor = make_member(workspace, "c@x.test", role=WorkspaceMembership.WorkspaceRole.CONTRIBUTOR)
        for user in (outsider, contributor):
            client.force_login(user)
            assert client.get(url("list", workspace)).status_code == 403
            assert client.get(url("edit", workspace, page)).status_code == 403

    def test_viewer_reads_but_cannot_change(self, client, workspace, page, viewer):
        client.force_login(viewer)
        response = client.get(url("edit", workspace, page))
        assert response.status_code == 200
        assert b"Add a link" not in response.content and b"Delete this page" not in response.content
        link = services.add_link(page, title="a", url="https://example.com/a")
        for name, kwargs in (
            ("save", {}),
            ("delete", {}),
            ("link_add", {}),
            ("link_update", {"link_id": link.id}),
            ("link_delete", {"link_id": link.id}),
            ("link_move", {"link_id": link.id}),
        ):
            assert (
                client.post(url(name, workspace, page, **kwargs), {"title": "x", "url": "https://e.co"}).status_code
                == 403
            )
        assert client.post(url("create", workspace), {"title": "T", "slug": "tt"}).status_code == 403
        assert BioPage.objects.count() == 1 and BioLink.objects.get().title == "a"

    def test_other_workspace_pages_are_404(self, client, workspace, owner, page):
        other = type(workspace).objects.create(organization=workspace.organization, name="Other")
        make_member_other = WorkspaceMembership.objects.create(
            user=owner, workspace=other, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
        )
        assert make_member_other
        client.force_login(owner)
        assert client.get(url("edit", other, page)).status_code == 404
        assert client.post(url("delete", other, page)).status_code == 404
        assert BioPage.objects.filter(pk=page.pk).exists()


class TestEditing:
    @pytest.fixture(autouse=True)
    def login(self, client, owner):
        client.force_login(owner)

    def test_create_page(self, client, workspace):
        response = client.post(url("create", workspace), {"title": "Shop", "slug": "shop-page"})
        created = BioPage.objects.get(slug="shop-page")
        assert response["Location"].startswith(url("edit", workspace, created))

    def test_create_with_bad_slug_shows_the_reason(self, client, workspace):
        response = client.post(url("create", workspace), {"title": "Shop", "slug": "admin"}, follow=True)
        assert b"reserved" in response.content and not BioPage.objects.exists()

    def test_create_over_the_plan_limit(self, client, workspace, page, settings):
        settings.BILLING_ENABLED = True
        set_plan(workspace.organization, "free")
        response = client.post(url("create", workspace), {"title": "Two", "slug": "two"}, follow=True)
        assert b"link-in-bio" in response.content and BioPage.objects.count() == 1

    def test_save_page(self, client, workspace, page, image, instagram):
        response = client.post(
            url("save", workspace, page),
            {
                "title": "New title",
                "slug": "new-slug",
                "bio": "Hello",
                "theme": "forest",
                "accent_color": "#AA2255",
                "avatar": str(image.id),
                "social_accounts": [str(instagram.id)],
                "show_latest_posts": "on",
                "is_published": "on",
                "custom_domain": "",
            },
        )
        assert response.status_code == 302
        page.refresh_from_db()
        assert (page.title, page.slug, page.theme, page.accent_color) == ("New title", "new-slug", "forest", "#AA2255")
        assert page.avatar_id == image.id and page.show_latest_posts and page.is_published
        assert list(page.social_accounts.all()) == [instagram]
        assert page.custom_domain is None

    def test_save_rejects_a_bad_colour_and_keeps_the_page(self, client, workspace, page):
        response = client.post(
            url("save", workspace, page), {"title": "T", "slug": "acme", "accent_color": "red"}, follow=True
        )
        page.refresh_from_db()
        assert page.accent_color == "" and response.status_code == 200

    def test_unchecked_boxes_switch_things_off(self, client, workspace, page):
        client.post(url("save", workspace, page), {"title": "Acme", "slug": "acme", "theme": "clean"})
        page.refresh_from_db()
        assert not page.is_published and not page.show_latest_posts

    def test_add_edit_move_delete_links(self, client, workspace, page):
        client.post(url("link_add", workspace, page), {"title": "One", "url": "https://example.com/1", "icon": "shop"})
        client.post(url("link_add", workspace, page), {"title": "Two", "url": "https://example.com/2"})
        one, two = page.links.all()
        client.post(
            url("link_update", workspace, page, link_id=one.id),
            {
                "title": "Uno",
                "url": "https://example.com/uno",
                "icon": "star",
                "starts_at": "2030-01-01T09:00",
                "is_active": "on",
            },
        )
        one.refresh_from_db()
        assert (one.title, one.icon) == ("Uno", "star") and one.starts_at.hour == 9
        client.post(url("link_move", workspace, page, link_id=two.id), {"direction": "up"})
        assert [x.title for x in page.links.all()] == ["Two", "Uno"]
        client.post(url("link_delete", workspace, page, link_id=two.id))
        assert [x.title for x in page.links.all()] == ["Uno"]

    def test_javascript_links_are_refused_in_the_form(self, client, workspace, page):
        response = client.post(
            url("link_add", workspace, page), {"title": "x", "url": "javascript:alert(1)"}, follow=True
        )
        assert not page.links.exists() and b"http" in response.content

    def test_a_link_of_another_page_is_404(self, client, workspace, page):
        other = services.create_page(workspace, title="Other", slug="other-page")
        link = services.add_link(other, title="x", url="https://example.com")
        assert client.post(url("link_delete", workspace, page, link_id=link.id)).status_code == 404

    def test_post_link_form(self, client, workspace, page, instagram):
        from django.utils import timezone

        from apps.composer.models import PlatformPost, Post

        post = Post.objects.create(workspace=workspace, caption="Fresh beans")
        PlatformPost.objects.create(
            post=post, social_account=instagram, status="published", published_at=timezone.now()
        )
        client.post(url("post_link", workspace, page, post_id=post.id), {"url": "https://shop.example.com/beans"})
        assert post.bio_link.url == "https://shop.example.com/beans"
        assert b"Fresh beans" in client.get(url("edit", workspace, page)).content

    def test_editor_renders_stats_and_domain_help(self, client, workspace, page):
        link = services.add_link(page, title="Shop", url="https://example.com")
        from django.test import RequestFactory

        services.record_click(page, link.id, RequestFactory().get("/", HTTP_USER_AGENT="Firefox/130"))
        html = client.get(url("edit", workspace, page) + "?days=7").content.decode()
        assert "CNAME" in html and "Clicks per link" in html and "Shop" in html
        assert "not switched on" in html

    def test_delete_page(self, client, workspace, page):
        client.post(url("delete", workspace, page))
        assert not BioPage.objects.exists()
