"""Overview, compare and export pages: rendering, scoping, permissions, plan gating."""

import csv
import io
from datetime import timedelta
from pathlib import Path

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.analytics.ranges import local_today
from apps.billing.tests.support import set_plan
from apps.members.models import WorkspaceMembership
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace

from .helpers import fill_days, make_account, make_member, make_post


@pytest.fixture
def workspace(db):
    return Workspace.objects.create(
        organization=Organization.objects.create(name="Org"), name="Main WS", timezone="UTC"
    )


@pytest.fixture
def owner_client(client, workspace):
    client.force_login(make_member(workspace, "owner@example.com"))
    return client


@pytest.fixture
def seeded(workspace):
    today = local_today(workspace_tz(workspace))
    start = today - timedelta(days=13)
    ig = make_account(workspace, "instagram", "IG Main", followers=1200)
    li = make_account(workspace, "linkedin_company", "LI Page", followers=300)
    bs = make_account(workspace, "bluesky", "Sky")
    fill_days(ig, "views", start, today, 100)
    fill_days(ig, "likes", start, today, 8)
    fill_days(li, "impressions", start, today, 40)
    make_post(workspace, ig, timezone.now() - timedelta(days=2), '=HYPERLINK("http://evil")', snapshots={})
    return ig, li, bs


def workspace_tz(workspace):
    from apps.analytics.ranges import workspace_tzinfo

    return workspace_tzinfo(workspace)


def url(name, workspace, **kwargs):
    return reverse(f"analytics:{name}", kwargs={"workspace_id": workspace.id, **kwargs})


@pytest.mark.django_db
def test_overview_shows_totals_and_not_available_for_missing_metrics(owner_client, workspace, seeded):
    response = owner_client.get(url("index", workspace) + "?range=7")
    body = response.content.decode()

    assert response.status_code == 200
    kpis = {k["key"]: k for k in response.context["kpis"]}
    assert kpis["exposure"]["value"] == 7 * 140
    assert kpis["engagements"]["reporting"] == 1 and kpis["engagements"]["total"] == 3
    assert "1 of 3 accounts report this." in body
    assert response.context["range"].days == 7
    assert 'data-chart="overview-trend"' in body and "View data as a table" in body
    assert "Bluesky" in body  # unavailable networks stay listed with a reason


@pytest.mark.django_db
def test_overview_range_is_url_addressable(owner_client, workspace, seeded):
    today = local_today(workspace_tz(workspace))
    start = today - timedelta(days=4)
    response = owner_client.get(
        url("index", workspace) + f"?range=custom&start={start}&end={today}&compare=none&metric=engagements"
    )
    rng = response.context["range"]
    assert (rng.start, rng.end, rng.compare) == (start, today, False)
    assert response.context["trend"]["key"] == "engagements"
    assert len(response.context["trend"]["datasets"]) == 1


@pytest.mark.django_db
def test_accounts_tab_still_lands_on_an_account(owner_client, workspace, seeded):
    response = owner_client.get(url("accounts", workspace) + "?range=7", follow=True)
    assert response.context["active_account"].platform in {"instagram", "linkedin_company", "bluesky"}
    assert response.context["range"].days == 7


@pytest.mark.django_db
def test_compare_needs_two_accounts_then_overlays_them(owner_client, workspace, seeded):
    ig, li, _ = seeded
    one = owner_client.get(url("compare", workspace) + f"?accounts={ig.id}")
    assert one.context["ready"] is False

    both = owner_client.get(
        url("compare", workspace) + f"?range=7&accounts={ig.id}&accounts={li.id}&accounts=not-a-uuid"
    )
    assert both.context["ready"] is True
    assert [d["label"] for d in both.context["trend"]["datasets"]] == ["IG Main", "LI Page"]
    matrix = {m["key"]: m for m in both.context["matrix"]}
    assert matrix["exposure"]["cells"][0]["value"] == 700 and matrix["exposure"]["cells"][1]["value"] == 280
    assert matrix["engagements"]["cells"][1]["value"] is None


@pytest.mark.django_db
def test_compare_ignores_other_workspaces_and_caps_at_four(owner_client, workspace, seeded):
    other_ws = Workspace.objects.create(organization=workspace.organization, name="Other")
    foreign = make_account(other_ws, "instagram", "Foreign")
    mine = [make_account(workspace, "facebook", f"FB{i}") for i in range(5)]
    qs = "&".join(f"accounts={a.id}" for a in [foreign, *mine])
    response = owner_client.get(url("compare", workspace) + "?" + qs)
    assert foreign not in response.context["picked"]
    assert len(response.context["picked"]) == 4


def csv_rows(response):
    return list(csv.reader(io.StringIO(b"".join(response.streaming_content).decode())))


@pytest.mark.django_db
def test_exports_are_streamed_csv(owner_client, workspace, seeded):
    response = owner_client.get(url("export", workspace, kind="overview") + "?range=7&compare=none")
    assert response.status_code == 200 and response.streaming
    assert response["Content-Type"].startswith("text/csv")
    assert "attachment" in response["Content-Disposition"] and ".csv" in response["Content-Disposition"]
    rows = csv_rows(response)
    assert rows[0] == ["date", "Views & impressions", "Engagements", "New followers", "Posts published"]
    assert len(rows) == 8
    assert rows[1][1] == "140" and rows[1][2] == "8" and rows[1][3] == ""  # new followers: nobody reports it


@pytest.mark.django_db
def test_accounts_export_leaves_missing_figures_blank(owner_client, workspace, seeded):
    rows = csv_rows(owner_client.get(url("export", workspace, kind="accounts") + "?range=7&compare=none"))
    header = rows[0]
    by_name = {r[0]: dict(zip(header, r, strict=True)) for r in rows[1:]}
    assert by_name["IG Main"]["Engagements"] == "56"
    assert by_name["LI Page"]["Engagements"] == ""
    assert by_name["Sky"]["analytics_status"] != "ok" and by_name["Sky"]["Views & impressions"] == ""


@pytest.mark.django_db
def test_posts_export_is_scoped_and_neutralises_formulas(owner_client, workspace, seeded):
    other_ws = Workspace.objects.create(organization=workspace.organization, name="Other")
    foreign = make_account(other_ws, "instagram", "Foreign IG")
    make_post(other_ws, foreign, timezone.now() - timedelta(days=1), "secret caption")

    rows = csv_rows(owner_client.get(url("export", workspace, kind="posts") + "?range=7"))
    text = "\n".join(",".join(r) for r in rows)
    assert "secret caption" not in text and "Foreign IG" not in text
    assert len(rows) == 2
    caption = rows[1][rows[0].index("caption")]
    assert caption.startswith("'=") and not caption.startswith("=")


@pytest.mark.django_db
def test_unknown_export_kind_is_a_404(owner_client, workspace, seeded):
    assert owner_client.get(url("export", workspace, kind="secrets")).status_code == 404


@pytest.mark.django_db
def test_export_requires_membership_and_view_analytics(client, workspace, seeded):
    stranger_ws = Workspace.objects.create(organization=Organization.objects.create(name="Elsewhere"), name="Elsewhere")
    client.force_login(make_member(stranger_ws, "stranger@example.com"))
    assert client.get(url("export", workspace, kind="overview")).status_code == 403
    assert client.get(url("index", workspace)).status_code == 403

    contributor = make_member(workspace, "contrib@example.com", WorkspaceMembership.WorkspaceRole.CONTRIBUTOR)
    client.force_login(contributor)
    assert client.get(url("export", workspace, kind="posts")).status_code == 403
    assert client.get(url("compare", workspace)).status_code == 403


@pytest.mark.django_db
def test_sidebar_hides_analytics_from_roles_without_view_analytics(client, workspace, seeded):
    contributor = make_member(workspace, "contrib@example.com", WorkspaceMembership.WorkspaceRole.CONTRIBUTOR)
    client.force_login(contributor)
    sidebar_link = f'href="{url("index", workspace)}"'
    assert (
        sidebar_link
        not in client.get(reverse("calendar:calendar", kwargs={"workspace_id": workspace.id})).content.decode()
    )

    viewer = make_member(workspace, "viewer@example.com", WorkspaceMembership.WorkspaceRole.VIEWER)
    client.force_login(viewer)
    assert sidebar_link in client.get(url("index", workspace)).content.decode()


@pytest.mark.django_db
def test_anonymous_users_are_sent_to_login(client, workspace):
    assert client.get(url("export", workspace, kind="overview")).status_code == 302


@pytest.mark.django_db
def test_new_pages_are_gated_by_the_plan(settings, owner_client, workspace, seeded):
    settings.BILLING_ENABLED = True
    set_plan(workspace.organization, "free")
    for target in (url("index", workspace), url("compare", workspace), url("export", workspace, kind="overview")):
        assert owner_client.get(target).status_code == 402
    set_plan(workspace.organization, "starter")
    assert owner_client.get(url("export", workspace, kind="overview")).status_code == 200


def test_analytics_templates_carry_no_inline_styles():
    root = Path(__file__).resolve().parents[3] / "templates" / "analytics"
    offenders = [p.name for p in root.glob("*.html") if 'style="' in p.read_text()]
    assert offenders == []


@pytest.mark.django_db
def test_pages_do_not_load_chart_js_from_a_cdn(owner_client, workspace, seeded):
    body = owner_client.get(url("index", workspace)).content.decode()
    assert "js/vendor/chart.umd.min.js" in body
    assert "cdn.jsdelivr.net" not in body  # flatpickr and Sortable are self-hosted too
