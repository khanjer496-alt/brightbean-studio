import re

import pytest
from django.test import override_settings
from django.urls import reverse

from apps.connect import platforms as catalog

SLUGS = {
    "claude",
    "chatgpt",
    "copilot-studio",
    "gemini",
    "mistral",
    "perplexity",
    "claude-code",
    "cursor",
    "vscode",
    "raycast",
}


@pytest.mark.django_db
def test_page_renders_without_login(client):
    response = client.get(reverse("connect:index"))
    assert response.status_code == 200
    html = response.content.decode()
    assert "<h1>Connect your AI</h1>" in html
    assert 'lang="en"' in html
    main = html.split("<main", 1)[1].split("</main>")[0]
    assert 'style="' not in main
    assert "<script" not in main


@pytest.mark.django_db
def test_every_platform_has_a_labelled_section(client):
    html = client.get(reverse("connect:index")).content.decode()
    assert {item.slug for item in catalog.platforms()} == SLUGS
    for slug in SLUGS:
        assert f'aria-labelledby="platform-{slug}"' in html
        assert f'id="platform-{slug}"' in html


@pytest.mark.django_db
def test_server_url_is_the_public_mcp_url(client):
    with override_settings(MCP_PUBLIC_BASE_URL="https://app.example.test"):
        html = client.get(reverse("connect:index")).content.decode()
        assert 'value="https://app.example.test/api/v1/mcp"' in html
        assert "claude mcp add --transport http postdelegate https://app.example.test/api/v1/mcp" in html


@pytest.mark.django_db
def test_form_controls_and_headings_are_accessible(client):
    html = client.get(reverse("connect:index")).content.decode()
    assert re.search(r'<label for="mcp-url">', html)
    assert 'id="mcp-url"' in html
    levels = [int(m) for m in re.findall(r"<h([1-6])[ >]", html.split("<main", 1)[1])]
    assert levels[0] == 1
    assert all(b - a <= 1 for a, b in zip(levels, levels[1:], strict=False)), levels


def test_cursor_link_encodes_the_server_config():
    import base64
    import json
    from urllib.parse import parse_qs, urlparse

    link = catalog.cursor_install_url("https://app.example.test/api/v1/mcp")
    query = parse_qs(urlparse(link).query)
    assert query["name"] == ["postdelegate"]
    assert json.loads(base64.b64decode(query["config"][0])) == {"url": "https://app.example.test/api/v1/mcp"}


def test_vscode_link_carries_the_json_config():
    import json
    from urllib.parse import unquote

    link = catalog.vscode_install_url("https://app.example.test/api/v1/mcp")
    assert link.startswith("vscode:mcp/install?")
    config = json.loads(unquote(link.split("?", 1)[1]))
    assert config == {"name": "postdelegate", "type": "http", "url": "https://app.example.test/api/v1/mcp"}


@pytest.mark.django_db
def test_plugin_command_appears_only_when_the_marketplace_is_configured(client):
    url = reverse("connect:index")
    assert "claude plugin marketplace add" not in client.get(url).content.decode()
    with override_settings(AI_PLUGIN_MARKETPLACE_REPO="acme/plugins"):
        html = client.get(url).content.decode()
    assert "claude plugin marketplace add acme/plugins" in html


@pytest.mark.django_db
def test_page_is_linked_from_the_api_keys_page(client, django_user_model):
    from apps.api.tests.world import build_world

    world = build_world()
    client.force_login(world.owner)
    session = client.session
    session.save()
    response = client.get(reverse("api_keys:list"))
    assert response.status_code == 200
    assert reverse("connect:index") in response.content.decode()
