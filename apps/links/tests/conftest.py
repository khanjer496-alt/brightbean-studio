"""A workspace with people of each role, a few accounts, and helpers to schedule a post."""

from __future__ import annotations

import datetime as dt

import httpx
import pytest
from django.utils import timezone

from apps.analytics.tests.helpers import make_account, make_member
from apps.composer.models import PlatformPost, Post
from apps.links import services, shorteners
from apps.members.models import WorkspaceMembership
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace

BROWSER = {"HTTP_USER_AGENT": "Mozilla/5.0 (X11; Linux) Firefox/130.0"}


@pytest.fixture
def workspace(db):
    org = Organization.objects.create(name="Bright Agency")
    return Workspace.objects.create(organization=org, name="Acme Coffee", timezone="UTC")


@pytest.fixture
def owner(workspace):
    return make_member(workspace, "owner@example.com")


@pytest.fixture
def viewer(workspace):
    return make_member(workspace, "viewer@example.com", role=WorkspaceMembership.WorkspaceRole.VIEWER)


@pytest.fixture
def linkedin(workspace):
    return make_account(workspace, "linkedin", "Acme LinkedIn")


@pytest.fixture
def x_account(workspace):
    return make_account(workspace, "x", "Acme X")


@pytest.fixture
def bluesky(workspace):
    return make_account(workspace, "bluesky", "Acme Sky")


@pytest.fixture
def mock_http(monkeypatch):
    """Route the shortener services' HTTP through a handler list; ``calls`` records every request."""
    calls: list[httpx.Request] = []
    state = {"handler": lambda request: httpx.Response(500)}

    def transport_handler(request):
        calls.append(request)
        return state["handler"](request)

    monkeypatch.setattr(shorteners, "_TRANSPORT", httpx.MockTransport(transport_handler))

    class Mock:
        def __init__(self):
            self.calls = calls

        def reply(self, handler):
            state["handler"] = handler

    return Mock()


def configure(workspace, **values):
    values.setdefault("shorten_mode", "always")
    return services.update_settings(workspace, **values)


def schedule_post(workspace, accounts, caption, *, shorten=None, status="draft", **post_fields):
    """A post with one platform post per account, then scheduled (so the link hook runs on the transition)."""
    post = Post.objects.create(workspace=workspace, caption=caption, shorten_links=shorten, **post_fields)
    children = [PlatformPost.objects.create(post=post, social_account=a, status="draft") for a in accounts]
    if status == "scheduled":
        when = timezone.now() + dt.timedelta(days=1)
        for child in children:
            child.transition_to("scheduled")
            child.scheduled_at = when
            child.save(update_fields=[*PlatformPost.TRANSITION_FIELDS, "scheduled_at"])
    return post, [PlatformPost.objects.get(pk=c.pk) for c in children]
