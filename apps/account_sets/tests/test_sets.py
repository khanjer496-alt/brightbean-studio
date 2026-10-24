"""Saved account sets: service rules, settings pages and permissions, composer, CSV import, repurpose."""

from __future__ import annotations

import csv
import io

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse

from apps.account_sets import services
from apps.account_sets.models import AccountSet
from apps.composer.models import PlatformPost, Post

pytestmark = pytest.mark.django_db


def url(name, workspace, **kwargs):
    return reverse(f"account_sets:{name}", kwargs={"workspace_id": workspace.id, **kwargs})


def login(user):
    client = Client()
    client.force_login(user)
    return client


class TestService:
    def test_create_with_defaults_cleaned_to_what_the_platform_allows(self, workspace, pinterest, tiktok, linkedin):
        made = services.create_set(
            workspace,
            name="Client A: all channels",
            account_ids=[pinterest.id, tiktok.id, linkedin.id],
            defaults={
                str(pinterest.id): {"board_id": "123"},
                str(tiktok.id): {"privacy_level": "SELF_ONLY", "disable_duet": True},
            },
        )
        assert set(made.accounts.all()) == {pinterest, tiktok, linkedin}
        assert made.defaults == {
            str(pinterest.id): {"board_id": "123"},
            str(tiktok.id): {"privacy_level": "SELF_ONLY", "disable_duet": True},
        }

    def test_refuses_keys_a_platform_does_not_offer(self, workspace, linkedin):
        with pytest.raises(services.SetError, match="can't default"):
            services.create_set(
                workspace, name="X", account_ids=[linkedin.id], defaults={str(linkedin.id): {"board_id": "1"}}
            )

    def test_refuses_defaults_for_accounts_outside_the_set(self, workspace, linkedin, pinterest):
        with pytest.raises(services.SetError, match="in the set"):
            services.create_set(
                workspace, name="X", account_ids=[linkedin.id], defaults={str(pinterest.id): {"board_id": "1"}}
            )

    def test_accounts_must_belong_to_the_workspace(self, workspace, stranger):
        with pytest.raises(services.SetError, match="isn't in this workspace"):
            services.create_set(workspace, name="X", account_ids=[stranger.id])

    def test_name_rules(self, workspace, linkedin):
        services.create_set(workspace, name="Team", account_ids=[linkedin.id])
        with pytest.raises(services.SetError, match="already exists"):
            services.create_set(workspace, name="  team ", account_ids=[linkedin.id])
        with pytest.raises(services.SetError, match="name"):
            services.create_set(workspace, name=" ", account_ids=[linkedin.id])
        with pytest.raises(services.SetError, match="at least one"):
            services.create_set(workspace, name="Empty", account_ids=[])

    def test_update_drops_defaults_of_removed_accounts_and_keeps_hidden_members(self, workspace, pinterest, linkedin):
        made = services.create_set(
            workspace,
            name="Mix",
            account_ids=[pinterest.id, linkedin.id],
            defaults={str(pinterest.id): {"board_id": "9"}},
        )
        services.update_set(made, account_ids=[linkedin.id], hidden_account_ids=[])
        made.refresh_from_db()
        assert made.defaults == {} and list(made.accounts.all()) == [linkedin]
        services.update_set(made, account_ids=[linkedin.id], hidden_account_ids=[pinterest.id])
        assert set(made.accounts.all()) == {linkedin, pinterest}

    def test_usable_accounts_skip_disconnected_and_follow_an_allowlist(self, workspace, linkedin, pinterest):
        made = services.create_set(workspace, name="Mix", account_ids=[pinterest.id, linkedin.id])
        pinterest.connection_status = "disconnected"
        pinterest.save(update_fields=["connection_status"])
        assert services.usable_accounts(made) == [linkedin]
        payload = services.composer_payload(made)
        assert payload["account_ids"] == [str(linkedin.id)] and payload["skipped"] == [pinterest.account_name]
        assert services.usable_accounts(made, allowed_ids=set()) == []

    def test_deleting_a_set_keeps_its_accounts(self, workspace, linkedin):
        made = services.create_set(workspace, name="Gone", account_ids=[linkedin.id])
        services.delete_set(made)
        linkedin.refresh_from_db()
        assert not AccountSet.objects.exists()

    def test_plan_limit(self, workspace, linkedin, settings):
        from apps.billing.tests.support import set_plan

        settings.BILLING_ENABLED = True
        set_plan(workspace.organization, "free")
        services.create_set(workspace, name="One", account_ids=[linkedin.id])
        services.create_set(workspace, name="Two", account_ids=[linkedin.id])
        with pytest.raises(services.SetError, match="saved account sets"):
            services.create_set(workspace, name="Three", account_ids=[linkedin.id])


class TestSettingsPages:
    def test_owner_creates_edits_and_deletes(self, workspace, owner, linkedin, pinterest):
        client = login(owner)
        response = client.post(
            url("create", workspace),
            {
                "name": "Client A",
                "accounts": [str(linkedin.id), str(pinterest.id)],
                f"d_board_id_{pinterest.id}": "555",
            },
        )
        assert response.status_code == 302
        made = AccountSet.objects.get()
        assert made.defaults == {str(pinterest.id): {"board_id": "555"}}
        assert "Client A" in client.get(url("list", workspace)).content.decode()
        form = client.get(url("edit", workspace, set_id=made.id)).content.decode()
        assert 'value="555"' in form and "Default settings for this account" in form
        assert (
            client.post(
                url("edit", workspace, set_id=made.id), {"name": "Renamed", "accounts": [str(linkedin.id)]}
            ).status_code
            == 302
        )
        made.refresh_from_db()
        assert made.name == "Renamed" and made.defaults == {}
        assert client.post(url("delete", workspace, set_id=made.id)).status_code == 302
        assert not AccountSet.objects.exists()

    def test_errors_are_shown_not_raised(self, workspace, owner, linkedin):
        client = login(owner)
        response = client.post(url("create", workspace), {"name": "", "accounts": [str(linkedin.id)]}, follow=True)
        assert response.status_code == 200 and "Give the set a name" in response.content.decode()

    @pytest.mark.parametrize("who", ["editor", "viewer"])
    def test_only_account_managers_manage_sets(self, workspace, who, request, linkedin):
        client = login(request.getfixturevalue(who))
        made = services.create_set(workspace, name="Locked", account_ids=[linkedin.id])
        assert client.get(url("list", workspace)).status_code == 403
        assert client.post(url("create", workspace), {"name": "N", "accounts": [str(linkedin.id)]}).status_code == 403
        assert client.post(url("delete", workspace, set_id=made.id)).status_code == 403
        assert AccountSet.objects.count() == 1

    def test_a_set_of_another_workspace_is_a_404(self, workspace, other_workspace, owner, stranger):
        foreign = services.create_set(other_workspace, name="Theirs", account_ids=[stranger.id])
        client = login(owner)
        assert client.get(url("edit", workspace, set_id=foreign.id)).status_code == 404
        assert client.post(url("delete", workspace, set_id=foreign.id)).status_code == 404
        assert AccountSet.objects.count() == 1

    def test_non_members_and_anonymous_are_turned_away(self, workspace, other_workspace, linkedin):
        from apps.analytics.tests.helpers import make_member

        outsider = make_member(other_workspace, "outsider@example.com")
        assert login(outsider).get(url("list", workspace)).status_code in (403, 404)
        assert Client().get(url("list", workspace)).status_code == 302

    def test_the_settings_menu_links_to_sets(self, workspace, owner):
        html = login(owner).get(url("list", workspace)).content.decode()
        assert url("list", workspace) in html and "Account sets" in html


class TestComposer:
    def test_picker_and_apply_json_for_writers_only(self, workspace, owner, viewer, linkedin, tiktok):
        made = services.create_set(
            workspace,
            name="Both",
            account_ids=[linkedin.id, tiktok.id],
            defaults={str(tiktok.id): {"privacy_level": "SELF_ONLY"}},
        )
        data = login(owner).get(url("picker", workspace)).json()
        assert data["sets"][0]["name"] == "Both" and set(data["sets"][0]["account_ids"]) == {
            str(linkedin.id),
            str(tiktok.id),
        }
        assert data["sets"][0]["defaults"] == {str(tiktok.id): {"privacy_level": "SELF_ONLY"}}
        assert login(owner).get(url("apply", workspace, set_id=made.id)).json()["id"] == str(made.id)
        assert login(viewer).get(url("picker", workspace)).status_code == 403

    def test_composer_page_carries_the_sets_and_a_one_click_control(self, workspace, owner, linkedin, tiktok):
        services.create_set(workspace, name="Client A", account_ids=[linkedin.id, tiktok.id])
        html = login(owner).get(reverse("composer:compose", kwargs={"workspace_id": workspace.id})).content.decode()
        assert 'id="composer-account-sets"' in html and "Client A" in html
        assert "applySet(set)" in html and 'aria-label="Account sets"' in html

    def test_saving_a_post_for_a_selected_set_keeps_each_accounts_settings(self, workspace, owner, linkedin, tiktok):
        services.create_set(workspace, name="Both", account_ids=[linkedin.id, tiktok.id])
        response = login(owner).post(
            reverse("composer:save_post", kwargs={"workspace_id": workspace.id}),
            {
                "action": "save_draft",
                "caption": "hello",
                "selected_accounts": f"{linkedin.id},{tiktok.id}",
                f"ps_panel_{linkedin.id}": "1",
            },
        )
        assert response.status_code in (200, 204, 302)
        assert PlatformPost.objects.count() == 2


class TestCsvImport:
    def csv_file(self, rows):
        out = io.StringIO()
        csv.writer(out).writerows(rows)
        return SimpleUploadedFile("posts.csv", out.getvalue().encode(), content_type="text/csv")

    def run_import(self, client, workspace, rows, default_set=""):
        upload = client.post(
            reverse("composer:csv_upload", kwargs={"workspace_id": workspace.id}), {"csv_file": self.csv_file(rows)}
        )
        assert upload.status_code == 200
        mapping = {f"map_{name}": str(i) for i, name in enumerate(rows[0])}
        preview = client.post(
            reverse("composer:csv_preview", kwargs={"workspace_id": workspace.id}),
            {**mapping, "default_set": default_set},
        )
        confirm = client.post(reverse("composer:csv_confirm_import", kwargs={"workspace_id": workspace.id}))
        return preview, confirm

    def test_a_set_column_targets_every_account_with_its_saved_settings(self, workspace, owner, pinterest, linkedin):
        services.create_set(
            workspace,
            name="Client A",
            account_ids=[pinterest.id, linkedin.id],
            defaults={str(pinterest.id): {"board_id": "77"}},
        )
        client = login(owner)
        preview, _ = self.run_import(
            client, workspace, [["caption", "set"], ["Hello", "client a"], ["Again", "CLIENT A"]]
        )
        assert "Unknown" not in preview.content.decode()
        assert Post.objects.count() == 2
        assert PlatformPost.objects.count() == 4
        pin = PlatformPost.objects.filter(social_account=pinterest)
        assert {pp.platform_extra.get("board_id") for pp in pin} == {"77"}
        assert PlatformPost.objects.filter(social_account=linkedin).count() == 2

    def test_unknown_set_names_are_reported(self, workspace, owner, linkedin):
        preview, confirm = self.run_import(login(owner), workspace, [["caption", "set"], ["Hello", "Nope"]])
        assert "Unknown account set" in preview.content.decode() and "Nope" in preview.content.decode()

    def test_a_default_set_covers_rows_with_no_set(self, workspace, owner, linkedin, x_account):
        made = services.create_set(workspace, name="All", account_ids=[linkedin.id, x_account.id])
        self.run_import(login(owner), workspace, [["caption"], ["One"]], default_set=str(made.id))
        assert PlatformPost.objects.count() == 2

    def test_the_upload_step_offers_the_sets(self, workspace, owner, linkedin):
        services.create_set(workspace, name="Client A", account_ids=[linkedin.id])
        client = login(owner)
        response = client.post(
            reverse("composer:csv_upload", kwargs={"workspace_id": workspace.id}),
            {"csv_file": self.csv_file([["caption", "set"], ["x", "y"]])},
        )
        html = response.content.decode()
        assert 'name="default_set"' in html and "Client A" in html and 'name="map_set"' in html


class TestRepurpose:
    def test_a_set_picks_the_accounts_that_can_take_each_output(self, workspace, owner, linkedin, tiktok, monkeypatch):
        from apps.repurpose import outputs, views

        services.create_set(workspace, name="Both", account_ids=[linkedin.id, tiktok.id])
        captured = {}

        def fake_parse(workspace_, raw, **kwargs):
            captured["raw"] = raw
            return []

        monkeypatch.setattr(outputs, "parse_requests", fake_parse)
        from django.test import RequestFactory

        made = services.get_set(workspace, services.sets_for(workspace).first().id)
        request = RequestFactory().post("/", {"kind_post": "1", "set_post": str(made.id)})
        request.workspace = workspace
        views._requests_from_form(request)
        ids = captured["raw"][0]["account_ids"]
        eligible = {str(a.id) for a in (linkedin, tiktok) if not outputs.KINDS["post"].refusal(a)}
        assert set(ids) == eligible and len(ids) == len(set(ids))

    def test_the_page_offers_the_sets(self, workspace, owner, linkedin, monkeypatch):
        monkeypatch.setattr("apps.repurpose.views.ai_available", lambda org: True)
        services.create_set(workspace, name="Client A", account_ids=[linkedin.id])
        html = login(owner).get(reverse("repurpose:home", kwargs={"workspace_id": workspace.id})).content.decode()
        assert 'name="set_post"' in html and "Client A" in html
