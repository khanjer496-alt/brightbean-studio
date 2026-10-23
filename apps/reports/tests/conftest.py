"""A workspace with data to report on, plus the people who see (or don't see) it."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.ai_writer.tests.fakes import patch_anthropic
from apps.analytics.ranges import local_today, workspace_tzinfo
from apps.analytics.tests.helpers import fill_days, make_account, make_member, make_post
from apps.members.models import WorkspaceMembership
from apps.organizations.models import Organization
from apps.reports.models import ReportDefinition
from apps.workspaces.models import Workspace


@pytest.fixture(autouse=True)
def fake_claude():
    with patch_anthropic() as fake:
        yield fake


@pytest.fixture
def workspace(db):
    org = Organization.objects.create(name="Bright Agency")
    return Workspace.objects.create(organization=org, name="Acme Coffee", timezone="UTC", primary_color="#aa2255")


@pytest.fixture
def seeded(workspace):
    today = local_today(workspace_tzinfo(workspace))
    start = today - timedelta(days=70)
    ig = make_account(workspace, "instagram", "IG Main", followers=1200)
    li = make_account(workspace, "linkedin_company", "LI Page", followers=300)
    fill_days(ig, "views", start, today, 100)
    fill_days(ig, "likes", start, today, 8)
    fill_days(li, "impressions", start, today, 40)
    published = timezone.now() - timedelta(days=3)
    make_post(
        workspace,
        ig,
        published,
        "Fresh beans just landed",
        snapshots={
            "views": [(published.date(), 500, published + timedelta(days=1))],
            "likes": [(published.date(), 40, published + timedelta(days=1))],
        },
    )
    return ig, li


@pytest.fixture
def owner(workspace):
    return make_member(workspace, "owner@example.com")


@pytest.fixture
def viewer(workspace):
    return make_member(workspace, "viewer@example.com", role=WorkspaceMembership.WorkspaceRole.VIEWER)


@pytest.fixture
def client_user(workspace):
    return make_member(workspace, "client@example.com", role=WorkspaceMembership.WorkspaceRole.CLIENT)


@pytest.fixture
def definition(workspace, owner):
    return ReportDefinition.objects.create(
        workspace=workspace,
        name="Monthly report",
        range_type=ReportDefinition.RangeType.LAST_30,
        recipients=["boss@client.test"],
        created_by=owner,
    )
