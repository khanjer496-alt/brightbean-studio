"""PWA: root-scope service worker, manifest, offline page and the rules that keep private data out of caches."""

import json
import re
from pathlib import Path

import pytest
from django.conf import settings
from django.urls import reverse

from apps.analytics.tests.helpers import make_member
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace

STATIC = Path(settings.BASE_DIR) / "static"
MANIFEST = STATIC / "favicon" / "postdelegate.webmanifest"


def test_service_worker_headers_and_scope(client):
    response = client.get("/sw.js")
    assert response.status_code == 200
    assert response["Content-Type"].startswith("application/javascript")
    assert response["Service-Worker-Allowed"] == "/"
    cache_control = response["Cache-Control"]
    assert "no-cache" in cache_control and "no-store" in cache_control
    assert response["X-Content-Type-Options"] == "nosniff"


def test_service_worker_version_is_baked_in(client, settings):
    settings.PWA_BUILD_ID = "abc123"
    body = client.get("/sw.js").content.decode()
    assert "__PD_SW_VERSION__" not in body
    assert re.search(r"const VERSION = 'abc123-[0-9a-f]{8}';", body)


def test_service_worker_only_get(client):
    assert client.post("/sw.js").status_code == 405


def test_service_worker_never_caches_private_paths():
    source = (STATIC / "sw.js").read_text()
    deny = re.search(r"NEVER_INTERCEPT = \[(.*?)\];", source, re.S).group(1)
    for path in ("/api/", "/mcp", "/oauth/", "/accounts/", "/webhooks/", "/media/"):
        assert f"'{path}'" in deny
    # The only cache writes are for checked static responses and the offline page.
    assert source.count("cache.put(") == 1
    assert "isCacheable(response)" in source
    assert "request.method" in source


def test_manifest_is_installable():
    data = json.loads(MANIFEST.read_text())
    assert data["name"] and data["short_name"]
    assert data["start_url"] == "/" and data["scope"] == "/"
    assert data["display"] == "standalone"
    assert data["theme_color"].lower() == "#080a0f" == data["background_color"].lower()
    sizes = {(i["sizes"], i["purpose"]) for i in data["icons"]}
    assert {("192x192", "any"), ("512x512", "any"), ("192x192", "maskable"), ("512x512", "maskable")} <= sizes
    for icon in data["icons"]:
        assert (MANIFEST.parent / icon["src"]).is_file(), icon["src"]
    names = {s["name"] for s in data["shortcuts"]}
    assert names == {"New post", "Calendar"}
    for shortcut in data["shortcuts"]:
        assert shortcut["url"].startswith("/")
        for icon in shortcut["icons"]:
            assert (MANIFEST.parent / icon["src"]).is_file()


@pytest.mark.django_db
def test_offline_page_renders_without_auth(client):
    response = client.get(reverse("offline"))
    assert response.status_code == 200
    html = response.content.decode()
    assert "You are offline" in html and "PostDelegate" in html
    assert "csrfmiddlewaretoken" not in html
    assert "Set-Cookie" not in response.headers or "sessionid" not in response.headers.get("Set-Cookie", "")


@pytest.mark.django_db
def test_offline_page_has_no_account_data(client):
    workspace = Workspace.objects.create(organization=Organization.objects.create(name="Secret Agency"), name="Acme")
    user = make_member(workspace, "owner@example.com")
    client.force_login(user)
    html = client.get(reverse("offline")).content.decode()
    assert "owner@example.com" not in html and "Secret Agency" not in html and "Acme" not in html


@pytest.mark.django_db
def test_new_post_shortcut_requires_login_then_opens_composer(client):
    response = client.get("/app/new-post/")
    assert response.status_code == 302 and "login" in response["Location"]
    workspace = Workspace.objects.create(organization=Organization.objects.create(name="Agency"), name="Acme")
    user = make_member(workspace, "owner@example.com")
    user.last_workspace_id = workspace.id
    user.save(update_fields=["last_workspace_id"])
    client.force_login(user)
    response = client.get("/app/new-post/")
    assert response.status_code == 302
    assert response["Location"] == reverse("composer:compose", kwargs={"workspace_id": workspace.id})


@pytest.mark.django_db
def test_shell_links_manifest_registers_worker_and_offers_install(client):
    workspace = Workspace.objects.create(organization=Organization.objects.create(name="Agency"), name="Acme")
    client.force_login(make_member(workspace, "owner@example.com"))
    html = client.get(reverse("calendar:calendar", kwargs={"workspace_id": workspace.id})).content.decode()
    assert 'rel="manifest"' in html
    assert "js/pwa" in html
    assert "data-pd-install-button" in html
    assert "Add to Home Screen" in html


def test_csp_allows_same_origin_worker_and_manifest():
    assert settings.CSP_WORKER_SRC == ("'self'",)
    assert settings.CSP_MANIFEST_SRC == ("'self'",)
