"""The Links & UTM settings page and the short-link list: permissions, validation, secrets."""

from __future__ import annotations

import pytest
from django.test import Client
from django.urls import reverse

from apps.links import services
from apps.links.models import LinkSettings, ShortLink

from .conftest import configure

pytestmark = pytest.mark.django_db


def url(name, workspace, **kwargs):
    return reverse(f"links:{name}", kwargs={"workspace_id": workspace.id, **kwargs})


def login(user):
    client = Client()
    client.force_login(user)
    return client


FORM = {
    "shorten_mode": "ask",
    "provider": "builtin",
    "utm_enabled": "on",
    "utm_medium": "social",
    "utm_campaign_from": "post",
    "utm_campaign": "evergreen",
    "utm_custom": "utm_content=hero\nref=social",
}


class TestSettingsPage:
    def test_owner_saves_every_setting(self, workspace, owner):
        response = login(owner).post(
            url("settings", workspace),
            {**FORM, "provider": "dub", "api_key": "dub_key_1", "provider_domain": "go.example.com"},
        )
        assert response.status_code == 302
        saved = LinkSettings.objects.get()
        assert (saved.shorten_mode, saved.provider, saved.utm_enabled, saved.utm_campaign) == (
            "ask",
            "dub",
            True,
            "evergreen",
        )
        assert saved.utm_custom == {"utm_content": "hero", "ref": "social"} and saved.api_key == "dub_key_1"

    def test_the_key_is_never_sent_back_to_the_browser(self, workspace, owner):
        configure(workspace, provider="dub", api_key="dub_key_secret")
        html = login(owner).get(url("settings", workspace)).content.decode()
        assert "dub_key_secret" not in html and "A key is saved" in html
        assert 'type="password"' in html

    def test_a_blank_key_keeps_the_saved_one(self, workspace, owner):
        configure(workspace, provider="dub", api_key="keep-me")
        login(owner).post(url("settings", workspace), {**FORM, "provider": "dub", "api_key": ""})
        assert LinkSettings.objects.get().api_key == "keep-me"

    def test_validation_errors_are_shown(self, workspace, owner):
        response = login(owner).post(url("settings", workspace), {**FORM, "utm_custom": "bad name=1"}, follow=True)
        assert "usable parameter name" in response.content.decode()
        assert not LinkSettings.objects.filter(utm_enabled=True).exists()
        response = login(owner).post(
            url("settings", workspace), {**FORM, "provider": "shortio", "api_key": "k"}, follow=True
        )
        assert "Short.io needs the domain" in response.content.decode()

    def test_custom_short_domain_is_off_unless_the_operator_enabled_it(self, workspace, owner, settings):
        client = login(owner)
        assert "go.example.com" not in client.get(url("settings", workspace)).content.decode()
        client.post(url("settings", workspace), {**FORM, "custom_short_domain": "go.example.com"})
        assert LinkSettings.objects.get().custom_short_domain is None
        settings.LINKS_CUSTOM_DOMAINS_ENABLED = True
        client.post(url("settings", workspace), {**FORM, "custom_short_domain": "go.example.com"})
        assert LinkSettings.objects.get().custom_short_domain == "go.example.com"

    def test_a_short_domain_belongs_to_one_workspace(self, workspace, owner, settings):
        from apps.organizations.models import Organization
        from apps.workspaces.models import Workspace

        settings.LINKS_CUSTOM_DOMAINS_ENABLED = True
        other = Workspace.objects.create(organization=Organization.objects.create(name="O2"), name="Other")
        services.update_settings(other, custom_short_domain="go.example.com")
        response = login(owner).post(
            url("settings", workspace), {**FORM, "custom_short_domain": "go.example.com"}, follow=True
        )
        assert "already used by another workspace" in response.content.decode()

    @pytest.mark.parametrize("who", ["viewer"])
    def test_members_without_manage_rights_are_refused(self, workspace, who, request):
        client = login(request.getfixturevalue(who))
        assert client.get(url("settings", workspace)).status_code == 403
        assert client.post(url("settings", workspace), FORM).status_code == 403
        assert not LinkSettings.objects.exists()

    def test_anonymous_is_redirected_to_sign_in(self, workspace):
        assert Client().get(url("settings", workspace)).status_code == 302

    def test_utm_preview_fragment_lists_each_network(self, workspace, owner):
        configure(workspace, utm_enabled=True)
        html = (
            login(owner)
            .get(url("utm_preview", workspace) + "?url=https://example.com/a&title=Big+Sale")
            .content.decode()
        )
        for network in ("linkedin", "x", "instagram"):
            assert f"utm_source={network}" in html
        assert "utm_campaign=big-sale" in html
        bad = login(owner).get(url("utm_preview", workspace) + "?url=javascript:alert(1)").content.decode()
        assert "utm_source" not in bad and "web address" in bad

    def test_settings_menu_links_here(self, workspace, owner):
        assert url("settings", workspace) in login(owner).get(url("settings", workspace)).content.decode()


class TestLinkList:
    def test_lists_links_with_their_clicks_and_can_switch_them_off(self, workspace, owner):
        link = services.create_short_link(workspace, "https://example.com/a")
        client = login(owner)
        html = client.get(url("list", workspace)).content.decode()
        assert link.short_url in html and "https://example.com/a" in html
        assert client.post(url("toggle", workspace, link_id=link.id)).status_code == 302
        link.refresh_from_db()
        assert link.is_active is False
        assert Client().get(f"/s/{link.code}", HTTP_USER_AGENT="Mozilla/5.0 Firefox/130").status_code == 404

    def test_make_a_link_from_the_page(self, workspace, owner):
        client = login(owner)
        client.post(url("create", workspace), {"url": "https://example.com/new"})
        assert ShortLink.objects.get().created_by == owner
        client.post(url("create", workspace), {"url": "javascript:alert(1)"})
        assert ShortLink.objects.count() == 1

    def test_viewers_cannot_make_or_toggle_links(self, workspace, viewer):
        link = services.create_short_link(workspace, "https://example.com/a")
        client = login(viewer)
        assert client.post(url("create", workspace), {"url": "https://example.com/b"}).status_code == 403
        assert client.post(url("toggle", workspace, link_id=link.id)).status_code == 403
        assert client.get(url("list", workspace)).status_code == 200

    def test_another_workspaces_link_is_a_404(self, workspace, owner):
        from apps.organizations.models import Organization
        from apps.workspaces.models import Workspace

        other = Workspace.objects.create(organization=Organization.objects.create(name="O2"), name="Other")
        foreign = services.create_short_link(other, "https://example.com/theirs")
        assert login(owner).post(url("toggle", workspace, link_id=foreign.id)).status_code == 404
        foreign.refresh_from_db()
        assert foreign.is_active is True


class TestSettingsValidation:
    def test_custom_parameter_rules(self):
        assert services.clean_custom_params({"a": "1", "": ""}) == {"a": "1"}
        for bad in (
            {"utm_source": "x"},
            {"bad name": "1"},
            {"a": "v" * 101},
            {f"p{i}": "1" for i in range(11)},
            ["list"],
        ):
            with pytest.raises(services.LinkError):
                services.clean_custom_params(bad)

    def test_apply_utm_edge_cases(self):
        assert (
            services.apply_utm("https://e.com/a?x=1#frag", {"utm_source": "x"})
            == "https://e.com/a?x=1&utm_source=x#frag"
        )
        assert (
            services.apply_utm("https://e.com/a?UTM_SOURCE=mine", {"utm_source": "x"})
            == "https://e.com/a?UTM_SOURCE=mine"
        )
        assert services.apply_utm("https://e.com/", {"utm_campaign": ""}) == "https://e.com/"
        assert services.apply_utm("https://e.com/a b", {"k": "v w"}).endswith("k=v+w")
