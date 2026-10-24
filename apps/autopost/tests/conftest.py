"""Fixtures for feed auto-posting: a workspace with accounts and queues, a fake web, and a fake Claude."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from xml.sax.saxutils import escape

import httpx
import pytest
from django.test import Client
from django.utils import timezone

from apps.ai_writer.tests.fakes import patch_anthropic
from apps.autonomy.models import AutonomyPolicy
from apps.calendar.services import create_default_queue_and_slots
from apps.composer.models import Feed
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace


@pytest.fixture(autouse=True)
def fake_claude():
    with patch_anthropic() as fake:
        yield fake


@pytest.fixture
def ai_key(settings):
    settings.ANTHROPIC_API_KEY = "sk-ant-platform-key-for-tests"


@pytest.fixture
def workspace(db, organization):
    return Workspace.objects.create(organization=organization, name="Main")


def make_account(workspace, platform="bluesky", name="Sky"):
    account = SocialAccount.objects.create(
        workspace=workspace,
        platform=platform,
        account_platform_id=f"{platform}-{name}",
        account_name=name,
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )
    create_default_queue_and_slots(account)
    return account


@pytest.fixture
def account(workspace):
    return make_account(workspace)


@pytest.fixture
def second(workspace):
    return make_account(workspace, "mastodon", "Toot")


@pytest.fixture
def feed(workspace):
    return Feed.objects.create(workspace=workspace, name="Blog", url="https://blog.example/feed.xml")


def set_policy(workspace, **fields):
    policy, _ = AutonomyPolicy.objects.update_or_create(workspace=workspace, defaults=fields)
    return policy


@pytest.fixture
def full_auto(workspace):
    return set_policy(workspace, publish_level="full_auto", draft_level="full_auto", undo_delay_minutes=0)


def item(title, link, *, hours_ago=1, guid=None, summary="A short summary of the article.", image=None):
    when = format_datetime(datetime.now(UTC) - timedelta(hours=hours_ago)) if hours_ago is not None else ""
    date = f"<pubDate>{when}</pubDate>" if when else ""
    enclosure = f'<enclosure url="{image}" type="image/jpeg"/>' if image else ""
    return (
        f"<item><title>{escape(title)}</title><link>{link}</link><guid>{guid or link}</guid>"
        f"<description>{escape(summary)}</description>{date}{enclosure}</item>"
    )


def rss(*items):
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>Blog</title><link>https://blog.example</link>{"".join(items)}</channel></rss>'


class FakeWeb:
    """Replaces the SSRF-safe fetch: serves the pages registered in ``pages`` and records every request."""

    def __init__(self):
        self.pages: dict[str, httpx.Response | None] = {}
        self.requests: list[tuple[str, dict]] = []

    def feed(self, body, *, etag='"v1"', status=200, url="https://blog.example/feed.xml"):
        headers = {
            "ETag": etag,
            "Last-Modified": "Mon, 01 Jan 2026 00:00:00 GMT",
            "content-type": "application/rss+xml",
        }
        self.pages[url] = httpx.Response(status, content=body.encode(), headers=headers)

    def html(self, url, body):
        self.pages[url] = httpx.Response(200, content=body.encode(), headers={"content-type": "text/html"})

    def image(self, url, content=b"\x89PNG\r\n\x1a\n" + b"0" * 64, content_type="image/png"):
        self.pages[url] = httpx.Response(200, content=content, headers={"content-type": content_type})

    def __call__(self, url, headers, *, max_bytes, content_types=None):
        self.requests.append((url, dict(headers)))
        response = self.pages.get(url)
        if response is None:
            return None, None
        if headers.get("If-None-Match") and headers["If-None-Match"] == response.headers.get("ETag"):
            return httpx.Response(304), url
        return response, url


@pytest.fixture
def web(monkeypatch):
    fake = FakeWeb()
    monkeypatch.setattr("apps.autopost.feeds.safe_fetch", fake)
    return fake


@pytest.fixture
def owner(org_owner, organization, workspace):
    OrgMembership.objects.filter(user=org_owner).exclude(organization=organization).delete()
    WorkspaceMembership.objects.create(
        user=org_owner, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
    )
    return org_owner


@pytest.fixture
def owner_client(owner):
    client = Client()
    client.force_login(owner)
    return client


@pytest.fixture
def viewer_client(db, organization, workspace):
    from apps.accounts.models import User

    user = User.objects.create_user(email="v@example.com", password="x", name="V", tos_accepted_at=timezone.now())
    OrgMembership.objects.filter(user=user).exclude(organization=organization).delete()
    OrgMembership.objects.create(user=user, organization=organization, org_role=OrgMembership.OrgRole.MEMBER)
    WorkspaceMembership.objects.create(
        user=user, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.VIEWER
    )
    client = Client()
    client.force_login(user)
    return client
