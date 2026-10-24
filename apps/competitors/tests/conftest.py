"""A workspace with connected Bluesky and Instagram accounts, on the Team plan unless a test says otherwise."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from django.utils import timezone

from apps.analytics.tests.helpers import fill_days, make_account
from apps.billing.tests.support import set_plan
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace
from providers.types import PublicProfileMetrics


@pytest.fixture
def billing_on(settings):
    settings.BILLING_ENABLED = True
    return settings


@pytest.fixture
def workspace(db, billing_on):
    org = Organization.objects.create(name="Bright Agency")
    set_plan(org, "team")
    return Workspace.objects.create(organization=org, name="Acme Coffee", timezone="UTC")


@pytest.fixture
def bsky(workspace):
    return make_account(workspace, "bluesky", "Acme Bluesky", followers=1000)


def metrics(handle="rival.bsky.social", followers=5000, **kw):
    defaults = {
        "display_name": "Rival",
        "posts_count": 100,
        "avg_engagement": 12.0,
        "posts_per_week": 3.5,
        "sampled_posts": 20,
        "profile_url": f"https://bsky.app/profile/{handle}",
    }
    return PublicProfileMetrics(handle=handle, followers=followers, **{**defaults, **kw})


@pytest.fixture
def read(monkeypatch):
    """Replace the network read; ``read.queue[handle] = metrics(...)`` or an exception, ``read.calls`` lists handles."""

    class Reader:
        def __init__(self):
            self.calls: list[str] = []
            self.results: dict = {}

        def __call__(self, platform, handle, account):
            self.calls.append(handle)
            result = self.results.get(handle, metrics(handle))
            if isinstance(result, BaseException):
                raise result
            return result

    reader = Reader()
    monkeypatch.setattr("apps.competitors.services.fetch_profile", reader)
    return reader


def days_ago(n: int) -> date:
    return timezone.now().date() - timedelta(days=n)


__all__ = ["days_ago", "fill_days", "metrics"]
