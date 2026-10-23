"""A page with no avatar or accent of its own uses the brand kit's logo and colour."""

from __future__ import annotations

import pytest
from django.test import Client

from apps.linkinbio import services
from apps.media_library.models import MediaAsset
from apps.workspaces import brand_kit

pytestmark = pytest.mark.django_db

BROWSER = {"HTTP_USER_AGENT": "Mozilla/5.0 (X11; Linux) Firefox/130.0"}


def _image(workspace, name, *, shared=False):
    return MediaAsset.objects.create(
        organization=workspace.organization,
        workspace=None if shared else workspace,
        file=f"test/{name}",
        filename=name,
        media_type=MediaAsset.MediaType.IMAGE,
        mime_type="image/png",
    )


def html():
    return Client().get("/l/acme/", **BROWSER).content.decode()


def css():
    return Client().get("/l/acme/theme.css").content.decode()


def test_falls_back_to_the_kit_logo_and_colour(workspace, page):
    mark = _image(workspace, "mark.png")
    brand_kit.update(
        brand_kit.kit_for(workspace=workspace),
        {"logo_mark": str(mark.id), "colors": [{"name": "Brand", "hex": "#123456"}]},
    )
    body = html()
    assert "test/mark.png" in body and "theme.css" in body
    assert "--bio-accent:#123456" in css()


def test_the_page_own_values_win(workspace, page, image):
    mark = _image(workspace, "mark.png")
    brand_kit.update(
        brand_kit.kit_for(workspace=workspace), {"logo_mark": str(mark.id), "colors": [{"name": "B", "hex": "#123456"}]}
    )
    services.update_page(page, avatar=str(image.id), accent_color="#abcdef")
    body = html()
    assert "test/avatar.jpg" in body and "test/mark.png" not in body
    assert "--bio-accent:#abcdef" in css()


def test_a_dark_theme_prefers_the_logo_for_dark_backgrounds(workspace, page):
    kit = brand_kit.kit_for(workspace=workspace)
    light, dark = _image(workspace, "light.png"), _image(workspace, "dark.png")
    brand_kit.update(kit, {"logo_on_light": str(light.id), "logo_on_dark": str(dark.id)})
    assert "test/light.png" in html()
    services.update_page(page, theme="midnight")
    assert "test/dark.png" in html()


def test_organization_kit_is_the_fallback_and_changes_show_at_once(workspace, page):
    shared = _image(workspace, "org.png", shared=True)
    assert "theme.css" not in html()  # cached now
    brand_kit.update(
        brand_kit.kit_for(organization=workspace.organization),
        {"logo_primary": str(shared.id), "colors": [{"name": "O", "hex": "#00aa00"}]},
    )
    body = html()
    assert "test/org.png" in body and "theme.css" in body


def test_without_a_kit_nothing_changes(page):
    body = html()
    assert "bio-avatar" not in body and "theme.css" not in body
    assert css() == ""


def test_a_new_page_starts_dark_when_the_brand_only_has_a_dark_logo(workspace):
    dark = _image(workspace, "dark.png")
    brand_kit.update(brand_kit.kit_for(workspace=workspace), {"logo_on_dark": str(dark.id)})
    assert services.create_page(workspace, title="A", slug="dark-one").theme == "midnight"
    assert services.create_page(workspace, title="B", slug="light-one", theme="clean").theme == "clean"


def test_editor_names_the_kit_defaults(client, workspace, owner, page):
    mark = _image(workspace, "mark.png")
    brand_kit.update(
        brand_kit.kit_for(workspace=workspace), {"logo_mark": str(mark.id), "colors": [{"name": "B", "hex": "#123456"}]}
    )
    client.force_login(owner)
    body = client.get(f"/workspace/{workspace.id}/bio/{page.id}/").content.decode()
    assert "Brand kit logo" in body and "#123456" in body
