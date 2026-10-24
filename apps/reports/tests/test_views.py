"""Pages: permissions, plan gating, the editor and its preview, and the client portal's scoping."""

from __future__ import annotations

import pytest
from django.urls import reverse

from apps.billing.tests.support import set_plan
from apps.organizations.models import Organization
from apps.reports import services
from apps.reports.models import ReportDefinition, ReportRun
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db


def url(name, workspace, **kwargs):
    return reverse(f"reports:{name}", kwargs={"workspace_id": workspace.id, **kwargs})


def form_data(**overrides):
    data = {
        "name": "Weekly wrap",
        "range_type": "last_7",
        "sections": ["summary", "trends"],
        "compare": "on",
        "accent_color": "#112233",
        "recipients": "a@client.test\nb@client.test",
        "frequency": "weekly",
        "weekday": "1",
        "day_of_month": "1",
        "send_time": "08:30",
        "is_active": "on",
    }
    data.update(overrides)
    return data


@pytest.fixture
def ready_run(definition, seeded):
    return services.execute_run(services.create_run(definition, trigger="manual").id)


class TestPermissions:
    def test_anonymous_is_redirected(self, client, workspace):
        assert client.get(url("list", workspace)).status_code == 302

    def test_outsider_is_forbidden(self, client, workspace, db):
        from apps.analytics.tests.helpers import make_member

        other = Workspace.objects.create(organization=Organization.objects.create(name="Other"), name="Other")
        client.force_login(make_member(other, "other@example.com"))
        assert client.get(url("list", workspace)).status_code == 403

    def test_viewer_can_read_but_not_change(self, client, workspace, viewer, definition, ready_run):
        client.force_login(viewer)
        assert client.get(url("list", workspace)).status_code == 200
        assert client.get(url("history", workspace, report_id=definition.id)).status_code == 200
        assert client.get(url("run_detail", workspace, run_id=ready_run.id)).status_code == 200
        assert client.get(url("create", workspace)).status_code == 403
        assert client.post(url("run", workspace, report_id=definition.id)).status_code == 403
        assert client.post(url("delete", workspace, report_id=definition.id)).status_code == 403
        assert client.post(url("run_share", workspace, run_id=ready_run.id)).status_code == 403
        assert client.post(url("revoke_links", workspace, report_id=definition.id)).status_code == 403

    def test_viewer_sees_no_manage_buttons(self, client, workspace, viewer, definition):
        client.force_login(viewer)
        page = client.get(url("list", workspace)).content.decode()
        assert "Run now" not in page and "New report" not in page

    def test_cross_workspace_ids_are_404(self, client, workspace, owner, definition):
        other = Workspace.objects.create(organization=workspace.organization, name="Second")
        from apps.analytics.tests.helpers import make_member

        client.force_login(make_member(other, "second@example.com"))
        assert client.get(url("history", other, report_id=definition.id)).status_code == 404

    def test_plan_without_reports(self, client, workspace, owner, definition, settings):
        settings.BILLING_ENABLED = True
        set_plan(workspace.organization, "free")
        client.force_login(owner)
        assert client.get(url("list", workspace)).status_code == 402
        assert client.post(url("run", workspace, report_id=definition.id)).status_code == 402
        set_plan(workspace.organization, "starter")
        assert client.get(url("list", workspace)).status_code == 200


class TestEditor:
    def test_create_saves_definition_and_starts_schedule_clock(self, client, workspace, owner, seeded):
        client.force_login(owner)
        ig, _ = seeded
        response = client.post(url("create", workspace), form_data(social_accounts=[str(ig.id)]))
        assert response.status_code == 302
        d = ReportDefinition.objects.get(name="Weekly wrap")
        assert d.recipients == ["a@client.test", "b@client.test"] and d.created_by == owner
        assert list(d.social_accounts.all()) == [ig] and d.schedule_anchor is not None

    def test_blank_accent_starts_from_the_workspace_colour(self, client, workspace, owner, definition):
        # A colour input can't be blank: it would show and submit black.
        client.force_login(owner)
        page = client.get(url("edit", workspace, report_id=definition.id)).content.decode()
        assert 'value="#aa2255"' in page

    def test_invalid_form_shows_errors(self, client, workspace, owner):
        client.force_login(owner)
        response = client.post(url("create", workspace), form_data(recipients="nope", sections=[]))
        assert response.status_code == 400
        assert b"valid email" in response.content and b"Choose at least one section" in response.content

    def test_cannot_pick_another_workspaces_account(self, client, workspace, owner):
        from apps.analytics.tests.helpers import make_account

        other = Workspace.objects.create(organization=workspace.organization, name="Second")
        foreign = make_account(other, "instagram", "Foreign")
        client.force_login(owner)
        assert client.post(url("create", workspace), form_data(social_accounts=[str(foreign.id)])).status_code == 400

    def test_editing_unrelated_field_keeps_schedule_anchor(self, client, workspace, owner, definition):
        definition.frequency = "weekly"
        definition.save()
        client.force_login(owner)
        client.post(url("edit", workspace, report_id=definition.id), form_data(name="Renamed", frequency="monthly"))
        definition.refresh_from_db()
        assert definition.name == "Renamed" and definition.schedule_anchor is not None
        anchor = definition.schedule_anchor
        client.post(url("edit", workspace, report_id=definition.id), form_data(name="Again", frequency="monthly"))
        definition.refresh_from_db()
        assert definition.schedule_anchor == anchor

    def test_preview_renders_unsaved_form_without_ai(self, client, workspace, owner, seeded, fake_claude):
        client.force_login(owner)
        response = client.post(
            url("preview", workspace), form_data(sections=["summary", "ai_summary"], frequency="none")
        )
        assert response.status_code == 200 and b"Weekly wrap" in response.content
        assert b"written when the report is generated" in response.content
        assert fake_claude.calls == [] and not ReportDefinition.objects.filter(name="Weekly wrap").exists()

    def test_preview_reports_form_errors(self, client, workspace, owner):
        client.force_login(owner)
        body = client.post(url("preview", workspace), form_data(name="")).content.decode()
        assert "needs a few fixes" in body
        # one plain line per problem, not Django's generic message next to ours
        assert body.count("Give the report a name.") == 1 and "This field is required." not in body

    def test_form_page_has_no_inline_styles(self, client, workspace, owner, definition):
        client.force_login(owner)
        page = client.get(url("edit", workspace, report_id=definition.id)).content.decode()
        start = page.index('id="report-form"')
        ours = page[start : page.index("</form>", start)]
        assert " style=" not in ours and "Live preview" in page


class TestRunsAndDownloads:
    def test_run_now_queues_and_redirects_to_history(self, client, workspace, owner, definition):
        client.force_login(owner)
        response = client.post(url("run", workspace, report_id=definition.id), {"deliver": "on"})
        assert response.status_code == 302
        run = ReportRun.objects.get()
        assert run.status == "pending" and run.deliver and run.requested_by == owner

    def test_history_polls_while_building(self, client, workspace, owner, definition):
        services.create_run(definition, trigger="manual")
        client.force_login(owner)
        assert b'hx-trigger="every 4s"' in client.get(url("history", workspace, report_id=definition.id)).content

    def test_pdf_download(self, client, workspace, viewer, ready_run):
        client.force_login(viewer)
        response = client.get(url("run_pdf", workspace, run_id=ready_run.id))
        assert response.status_code == 200 and "attachment" in response["Content-Disposition"]
        assert b"".join(response.streaming_content).startswith(b"%PDF-")

    def test_share_link_for_managers(self, client, workspace, owner, ready_run):
        client.force_login(owner)
        response = client.post(url("run_share", workspace, run_id=ready_run.id))
        assert b"/reports/share/" in response.content and b"expires in 30 days" in response.content

    def test_revoke_links(self, client, workspace, owner, definition, ready_run):
        token = services.make_token(ready_run)
        client.force_login(owner)
        page = client.get(url("history", workspace, report_id=definition.id)).content.decode()
        assert "Revoke all shared links" in page
        response = client.post(url("revoke_links", workspace, report_id=definition.id), follow=True)
        assert "stopped working" in response.content.decode()
        assert services.resolve_token(token) is None

    def test_run_now_over_the_limit_explains_why(self, client, workspace, owner, definition, monkeypatch):
        monkeypatch.setattr(services, "MANUAL_RUNS_PER_HOUR", 0)
        client.force_login(owner)
        response = client.post(url("run", workspace, report_id=definition.id), follow=True)
        assert response.status_code == 200 and "in the last hour" in response.content.decode()
        assert not definition.runs.exists()

    def test_unfinished_run_has_no_detail_page(self, client, workspace, owner, definition):
        run = services.create_run(definition, trigger="manual")
        client.force_login(owner)
        assert client.get(url("run_detail", workspace, run_id=run.id)).status_code == 302

    def test_delete(self, client, workspace, owner, definition):
        client.force_login(owner)
        client.post(url("delete", workspace, report_id=definition.id))
        assert not ReportDefinition.objects.filter(pk=definition.pk).exists()

    def test_analytics_nav_links_to_reports(self, client, workspace, owner, seeded):
        client.force_login(owner)
        page = client.get(reverse("analytics:index", kwargs={"workspace_id": workspace.id})).content.decode()
        assert url("list", workspace) in page


class TestClientPortal:
    @pytest.fixture
    def portal(self, client, client_user, workspace):
        client.force_login(client_user)
        session = client.session
        session["is_portal_session"] = True
        session["portal_workspace_id"] = str(workspace.id)
        session.save()
        return client

    def test_lists_only_shared_finished_reports(self, portal, definition, ready_run):
        assert b"No reports shared yet" in portal.get(reverse("client_portal:reports")).content
        definition.visible_to_clients = True
        definition.save()
        hidden = ReportDefinition.objects.create(workspace=definition.workspace, name="Internal only")
        services.execute_run(services.create_run(hidden, trigger="manual").id)
        pending = services.create_run(definition, trigger="manual")
        page = portal.get(reverse("client_portal:reports")).content.decode()
        assert "Monthly report" in page and "Internal only" not in page
        assert str(pending.id) not in page

    def test_detail_and_pdf_for_shared_report(self, portal, definition, ready_run):
        definition.visible_to_clients = True
        definition.save()
        detail = portal.get(reverse("client_portal:report_detail", kwargs={"run_id": ready_run.id}))
        assert detail.status_code == 200 and b"Results by network" in detail.content
        pdf = portal.get(reverse("client_portal:report_pdf", kwargs={"run_id": ready_run.id}))
        assert b"".join(pdf.streaming_content).startswith(b"%PDF-")

    def test_unshared_report_is_404(self, portal, ready_run):
        assert portal.get(reverse("client_portal:report_detail", kwargs={"run_id": ready_run.id})).status_code == 404
        assert portal.get(reverse("client_portal:report_pdf", kwargs={"run_id": ready_run.id})).status_code == 404

    def test_other_workspaces_reports_are_not_visible(self, portal, workspace):
        other = Workspace.objects.create(organization=workspace.organization, name="Other client")
        d = ReportDefinition.objects.create(workspace=other, name="Other", visible_to_clients=True)
        run = services.execute_run(services.create_run(d, trigger="manual").id)
        assert portal.get(reverse("client_portal:report_detail", kwargs={"run_id": run.id})).status_code == 404
        assert b"Other" not in portal.get(reverse("client_portal:reports")).content.replace(b"Other client", b"")

    def test_plan_without_reports_hides_them(self, portal, definition, ready_run, settings, workspace):
        definition.visible_to_clients = True
        definition.save()
        settings.BILLING_ENABLED = True
        set_plan(workspace.organization, "free")
        assert b"No reports shared yet" in portal.get(reverse("client_portal:reports")).content

    def test_staff_session_without_portal_flag_is_turned_away(self, client, owner, definition, ready_run):
        client.force_login(owner)
        assert client.get(reverse("client_portal:report_detail", kwargs={"run_id": ready_run.id})).status_code == 302
