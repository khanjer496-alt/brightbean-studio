"""The Competitors page, the overview section, and the CSV exports."""

from __future__ import annotations

import csv
import io
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.analytics.tests.helpers import make_account, make_member
from apps.billing.tests.support import set_plan
from apps.competitors.models import Competitor, CompetitorSnapshot
from apps.members.models import WorkspaceMembership
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db

ROLE = WorkspaceMembership.WorkspaceRole


@pytest.fixture
def owner(client, workspace):
    user = make_member(workspace, "owner@example.com")
    client.force_login(user)
    return user


def url(name, workspace, **kw):
    return reverse(f"competitors:{name}", kwargs={"workspace_id": workspace.id, **kw})


@pytest.fixture
def tracked(workspace, bsky):
    today = timezone.now().date()
    comp = Competitor.objects.create(
        workspace=workspace,
        platform="bluesky",
        handle="rival.bsky.social",
        display_name="Rival Co",
        profile_url="https://bsky.app/profile/rival.bsky.social",
    )
    CompetitorSnapshot.objects.create(
        competitor=comp, date=today - timedelta(days=20), followers=1000, posts_per_week=2, avg_engagement=5
    )
    CompetitorSnapshot.objects.create(competitor=comp, date=today, followers=1100, posts_per_week=3, avg_engagement=6)
    return comp


class TestPage:
    def test_renders_the_comparison(self, client, owner, workspace, tracked):
        response = client.get(url("index", workspace))
        html = response.content.decode()
        assert response.status_code == 200
        assert "Rival Co" in html and "+10.0%" in html
        assert "Competitors</a>" in html  # the tab in the analytics nav
        assert 'aria-current="page"' in html
        assert "Not available" not in html  # only appears when a connected network is unsupported

    def test_lists_unsupported_networks_with_the_reason(self, client, owner, workspace, tracked):
        make_account(workspace, "linkedin_personal", "Me")
        html = client.get(url("index", workspace)).content.decode()
        assert "Not available" in html and "official way" in html

    def test_free_plan_sees_the_upgrade_state_not_the_form(self, client, owner, workspace):
        set_plan(workspace.organization, "free")
        html = client.get(url("index", workspace)).content.decode()
        assert "isn't in your plan" in html and url("add", workspace) not in html

    def test_viewer_sees_the_table_but_no_add_or_remove(self, client, workspace, tracked):
        client.force_login(make_member(workspace, "viewer@example.com", ROLE.VIEWER))
        html = client.get(url("index", workspace)).content.decode()
        assert "Rival Co" in html
        assert url("add", workspace) not in html and "/remove/" not in html

    def test_non_members_and_other_workspaces_are_refused(self, client, workspace, tracked):
        other_org = Organization.objects.create(name="Other")
        other_ws = Workspace.objects.create(organization=other_org, name="Other ws")
        client.force_login(make_member(other_ws, "stranger@example.com"))
        assert client.get(url("index", workspace)).status_code == 403
        assert client.get(url("export", workspace, kind="comparison")).status_code == 403
        assert client.post(url("add", workspace), {"platform": "bluesky", "handle": "x.bsky.social"}).status_code == 403

    def test_anonymous_is_redirected(self, client, workspace):
        assert client.get(url("index", workspace)).status_code == 302

    def test_chart_data_is_embedded_for_the_chart_script(self, client, owner, workspace):
        insta = make_account(workspace, "instagram", "IG", followers=500)
        from apps.analytics.models import AccountInsightsSnapshot

        today = timezone.now().date()
        for i, d in enumerate((today - timedelta(days=10), today)):
            AccountInsightsSnapshot.objects.create(
                social_account=insta, metric_key="followers", date=d, value=500 + i * 50
            )
        html = client.get(url("index", workspace) + "?network=instagram").content.decode()
        assert 'data-chart="competitors-trend"' in html and 'id="competitors-trend"' in html


class TestAddRemove:
    def test_add_redirects_with_a_message(self, client, owner, workspace, bsky, read):
        response = client.post(
            url("add", workspace), {"platform": "bluesky", "handle": "rival.bsky.social"}, follow=True
        )
        assert Competitor.objects.filter(handle="rival.bsky.social").exists()
        assert "Now tracking" in response.content.decode()

    def test_a_refusal_is_shown_and_nothing_is_saved(self, client, owner, workspace, read):
        response = client.post(
            url("add", workspace), {"platform": "bluesky", "handle": "rival.bsky.social"}, follow=True
        )
        assert not Competitor.objects.exists()
        assert "Connect a Bluesky account" in response.content.decode()

    def test_viewer_cannot_add_or_remove(self, client, workspace, bsky, tracked, read):
        client.force_login(make_member(workspace, "viewer@example.com", ROLE.VIEWER))
        assert client.post(url("add", workspace), {"platform": "bluesky", "handle": "x.bsky.social"}).status_code == 403
        assert client.post(url("remove", workspace, competitor_id=tracked.id)).status_code == 403
        assert Competitor.objects.count() == 1

    def test_remove(self, client, owner, workspace, tracked):
        client.post(url("remove", workspace, competitor_id=tracked.id))
        assert not Competitor.objects.exists() and not CompetitorSnapshot.objects.exists()

    def test_remove_in_another_workspace_is_a_404(self, client, owner, workspace, tracked):
        other = Workspace.objects.create(organization=workspace.organization, name="Other")
        make_member(other, "o2@example.com")
        assert client.post(url("remove", other, competitor_id=tracked.id)).status_code in (403, 404)
        assert Competitor.objects.filter(pk=tracked.pk).exists()

    def test_add_needs_post(self, client, owner, workspace):
        assert client.get(url("add", workspace)).status_code == 405


class TestExport:
    def rows(self, response):
        return list(csv.reader(io.StringIO(b"".join(response.streaming_content).decode())))

    def test_comparison_csv(self, client, owner, workspace, tracked):
        response = client.get(url("export", workspace, kind="comparison"))
        assert response["Content-Type"].startswith("text/csv")
        assert "competitors-comparison" in response["Content-Disposition"]
        rows = self.rows(response)
        assert rows[0][:5] == ["network", "who", "name", "handle", "followers"]
        by_who = {r[1]: r for r in rows[1:]}
        assert by_who["competitor"][2:6] == ["Rival Co", "rival.bsky.social", "1100", "10"]
        assert by_who["you"][2] == "Acme Bluesky"

    def test_history_csv_lists_every_snapshot_in_range(self, client, owner, workspace, tracked):
        rows = self.rows(client.get(url("export", workspace, kind="history")))
        assert rows[0][0] == "date" and len(rows) == 3
        assert {r[2] for r in rows[1:]} == {"rival.bsky.social"}

    def test_formula_like_values_are_neutralised(self, client, owner, workspace, tracked):
        Competitor.objects.filter(pk=tracked.pk).update(display_name='=HYPERLINK("x")')
        rows = self.rows(client.get(url("export", workspace, kind="comparison")))
        assert any(r[2] == '\'=HYPERLINK("x")' for r in rows)

    def test_only_this_workspaces_competitors(self, client, owner, workspace, tracked):
        other = Workspace.objects.create(organization=workspace.organization, name="Other")
        Competitor.objects.create(workspace=other, platform="bluesky", handle="secret.bsky.social")
        text = b"".join(client.get(url("export", workspace, kind="history")).streaming_content).decode()
        assert "secret" not in text

    def test_unknown_kind(self, client, owner, workspace):
        assert client.get(url("export", workspace, kind="nope")).status_code == 404


class TestOverviewSection:
    def test_overview_shows_the_block_when_tracking(self, client, owner, workspace, tracked):
        html = client.get(reverse("analytics:index", kwargs={"workspace_id": workspace.id})).content.decode()
        assert 'aria-labelledby="competitors-heading"' in html and "Rival Co" in html
        assert "Open benchmarking" in html

    def test_overview_invites_the_first_competitor(self, client, owner, workspace, bsky):
        html = client.get(reverse("analytics:index", kwargs={"workspace_id": workspace.id})).content.decode()
        assert "Track your first competitor" in html

    def test_overview_hides_the_block_on_a_plan_without_it(self, client, owner, workspace, bsky):
        set_plan(workspace.organization, "free")
        html = client.get(reverse("analytics:index", kwargs={"workspace_id": workspace.id})).content.decode()
        assert "competitors-heading" not in html
