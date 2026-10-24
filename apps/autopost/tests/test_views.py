"""The Feeds page: permissions, workspace scoping, saving settings and the item history."""

from __future__ import annotations

import pytest
from django.urls import reverse

from apps.autopost import services
from apps.autopost.models import FeedAutopost, FeedItem
from apps.composer.models import Feed
from apps.workspaces.models import Workspace

from .conftest import item, make_account, rss

pytestmark = pytest.mark.django_db

SETTINGS = {
    "mode": "link",
    "destination": "queue",
    "delay_minutes": "60",
    "max_posts_per_day": "3",
    "include": "",
    "exclude": "",
}


def url(name, workspace, **kwargs):
    return reverse(f"autopost:{name}", kwargs={"workspace_id": workspace.id, **kwargs})


class TestAccess:
    def test_viewer_cannot_open_or_change_anything(self, viewer_client, workspace, feed, account):
        assert viewer_client.get(url("home", workspace)).status_code == 403
        assert (
            viewer_client.post(
                url("save", workspace, feed_id=feed.id), {"accounts": [str(account.id)], **SETTINGS}
            ).status_code
            == 403
        )
        assert not FeedAutopost.objects.exists()

    def test_another_workspaces_feed_is_a_404(self, owner_client, owner, workspace, organization):
        from apps.members.models import WorkspaceMembership

        other = Workspace.objects.create(organization=organization, name="Other")
        WorkspaceMembership.objects.create(
            user=owner, workspace=other, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
        )
        theirs = Feed.objects.create(workspace=other, name="Theirs", url="https://x.example/f")
        assert owner_client.get(url("detail", workspace, feed_id=theirs.id)).status_code == 404
        assert owner_client.post(url("save", workspace, feed_id=theirs.id), SETTINGS).status_code == 404


class TestFollowing:
    def test_add_checks_the_feed_then_opens_its_settings(self, owner_client, workspace, web):
        web.feed(rss(item("One", "https://blog.example/1")))
        response = owner_client.post(url("add", workspace), {"url": "https://blog.example/feed.xml"})
        feed = Feed.objects.get()
        assert response.status_code == 302 and feed.name == "Blog"

    def test_an_address_that_is_not_a_feed_is_refused_in_place(self, owner_client, workspace, web):
        web.feed("<html></html>")
        response = owner_client.post(url("add", workspace), {"url": "https://blog.example/feed.xml"})
        assert response.status_code == 422 and "valid RSS" in response.content.decode()
        assert not Feed.objects.exists()

    def test_following_twice_reuses_the_feed(self, owner_client, workspace, feed):
        owner_client.post(url("add", workspace), {"url": feed.url})
        assert Feed.objects.count() == 1


class TestSettings:
    def test_save_turns_autoposting_on(self, owner_client, workspace, feed, account):
        response = owner_client.post(
            url("save", workspace, feed_id=feed.id), {"accounts": [str(account.id)], **SETTINGS}
        )
        assert response.status_code == 302
        autopost = FeedAutopost.objects.get()
        assert list(autopost.accounts.all()) == [account] and autopost.max_posts_per_day == 3

    def test_errors_are_shown_with_the_form_kept(self, owner_client, workspace, feed, account):
        response = owner_client.post(
            url("save", workspace, feed_id=feed.id),
            {"accounts": [str(account.id)], **SETTINGS, "max_posts_per_day": "0"},
        )
        page = response.content.decode()
        assert response.status_code == 422 and "daily limit" in page and f'value="{account.id}"' in page

    def test_other_workspaces_accounts_are_ignored(self, owner_client, workspace, feed, organization):
        foreign = make_account(Workspace.objects.create(organization=organization, name="Other"), "bluesky", "Far")
        response = owner_client.post(
            url("save", workspace, feed_id=feed.id), {"accounts": [str(foreign.id)], **SETTINGS}
        )
        assert response.status_code == 422 and not FeedAutopost.objects.exists()

    def test_pause_check_and_stop(self, owner_client, workspace, feed, account, web):
        autopost = services.enable(
            workspace, feed, None, accounts=[account], mode="link", destination="draft", delay_minutes=60,
            max_posts_per_day=3, include="", exclude="",
        )  # fmt: skip
        owner_client.post(url("toggle", workspace, feed_id=feed.id))
        autopost.refresh_from_db()
        assert autopost.is_active is False
        owner_client.post(url("toggle", workspace, feed_id=feed.id))
        web.feed(rss(item("One", "https://blog.example/1", hours_ago=-1)))
        owner_client.post(url("check", workspace, feed_id=feed.id))
        autopost.refresh_from_db()
        assert autopost.last_checked_at is not None
        assert (
            owner_client.post(url("check", workspace, feed_id=feed.id)).status_code == 302
        )  # cooldown, no second fetch
        owner_client.post(url("stop", workspace, feed_id=feed.id))
        assert not FeedAutopost.objects.exists() and Feed.objects.exists()

    def test_history_page(self, owner_client, workspace, feed, account, web, full_auto):
        autopost = services.enable(
            workspace, feed, None, accounts=[account], mode="link", destination="draft", delay_minutes=60,
            max_posts_per_day=3, include="", exclude="",
        )  # fmt: skip
        FeedItem.objects.create(
            autopost=autopost,
            key="k",
            title="Shipped it",
            link="https://blog.example/1",
            status="draft",
            reason="Sky: Saved as a draft.",
        )
        page = owner_client.get(url("detail", workspace, feed_id=feed.id)).content.decode()
        assert "Shipped it" in page and "Saved as a draft" in page

    def test_pages_render_for_a_feed_without_autoposting(self, owner_client, workspace, feed):
        assert "Start auto-posting" in owner_client.get(url("detail", workspace, feed_id=feed.id)).content.decode()
        assert feed.name in owner_client.get(url("home", workspace)).content.decode()
