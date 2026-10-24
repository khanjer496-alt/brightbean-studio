"""Wave 1 shell: floating-card header, icon rail, phone tab bar, bottom sheet, toasts, safe areas, touch and motion CSS."""

import re
from collections import Counter
from pathlib import Path

import pytest
from django.conf import settings
from django.template import Context, Template
from django.urls import reverse

from apps.analytics.tests.helpers import make_member
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace

STYLES = (Path(settings.BASE_DIR) / "theme" / "static_src" / "src" / "styles.css").read_text()


@pytest.fixture
def workspace_client(client):
    workspace = Workspace.objects.create(organization=Organization.objects.create(name="Agency"), name="Acme")
    client.force_login(make_member(workspace, "owner@example.com"))
    return client, workspace


def _calendar_html(workspace_client):
    client, workspace = workspace_client
    return client.get(reverse("calendar:calendar", kwargs={"workspace_id": workspace.id})).content.decode()


@pytest.mark.django_db
def test_viewport_covers_the_notch_and_theme_color_is_set(workspace_client):
    html = _calendar_html(workspace_client)
    assert re.search(r'<meta name="viewport" content="[^"]*viewport-fit=cover', html)
    assert '<meta name="theme-color"' in html


@pytest.mark.django_db
def test_signed_in_shell_has_header_cluster_and_tab_bar(workspace_client):
    html = _calendar_html(workspace_client)
    assert 'class="pd-header"' in html
    for hook in ("header-ws-btn", "header-account-btn", "mobile-menu-btn", "app-sidebar", "sidebar-new-btn"):
        assert f'id="{hook}"' in html, hook
    assert re.search(r'<nav class="pd-tabbar"[^>]*aria-label="Main tabs"', html)
    assert 'id="tab-create-btn"' in html and 'id="tab-more-btn"' in html
    assert "pd-modal--sheet" in html
    assert "pd-has-tabbar" in re.search(r"<body[^>]*>", html).group(0)
    assert 'aria-label="Collapse sidebar"' in html and 'aria-label="Expand sidebar"' in html
    # duplicated menus (header and drawer) must not duplicate ids
    ids = Counter(re.findall(r'\sid="([^"]+)"', html))
    assert [i for i, n in ids.items() if n > 1 and not i.startswith("pd-modal-")] == []


@pytest.mark.django_db
def test_composer_has_no_tab_bar(workspace_client):
    client, workspace = workspace_client
    html = client.get(reverse("composer:compose", kwargs={"workspace_id": workspace.id})).content.decode()
    assert 'class="pd-tabbar"' not in html
    assert "pd-has-tabbar" not in re.search(r"<body[^>]*>", html).group(0)


@pytest.mark.django_db
def test_toast_has_separate_alert_and_status_regions(workspace_client):
    html = _calendar_html(workspace_client)
    assert 'role="alert"' in html and 'role="status" aria-live="polite"' in html
    assert "pd-toast--success" in html and "@pd-toast.window" in html and "@htmx-error.window" in html


def test_sheet_size_renders_the_sheet_class():
    html = Template("{% load common_extras %}{% modal open='o' title='T' size='sheet' %}x{% endmodal %}").render(
        Context()
    )
    assert "pd-modal pd-modal--sheet" in html and 'role="dialog"' in html


def test_stylesheet_honours_safe_areas_touch_targets_and_reduced_motion():
    assert "env(safe-area-inset-bottom)" in STYLES and "env(safe-area-inset-top)" in STYLES
    assert "env(safe-area-inset-left)" in STYLES and "env(safe-area-inset-right)" in STYLES
    assert "@media (pointer: coarse)" in STYLES and "min-height: 44px" in STYLES
    reduced = STYLES.split("@media (prefers-reduced-motion: reduce)")[-1]
    assert "animation-duration: 0.01ms !important" in reduced and "transition-duration: 0.01ms !important" in reduced
    assert ".animate-spin" in reduced  # spinners are status, not decoration


def test_shell_uses_dynamic_viewport_height_not_vh():
    shell = STYLES.split("APP SHELL (templates/base.html)")[1].split("/* ── Idea modal")[0]
    assert "100dvh" in shell and not re.search(r"\b100vh\b", shell)
    assert "--pd-tabbar-h" in shell and ".pd-tabbar" in shell


def test_checklist_pill_clears_the_tab_bar():
    source = (Path(settings.BASE_DIR) / "templates" / "onboarding" / "partials" / "_checklist.html").read_text()
    assert "var(--pd-tabbar-h" in source


@pytest.mark.django_db
def test_no_channel_logo_is_shown_twice_and_ai_is_one_phone_pill(workspace_client):
    client, workspace = workspace_client
    from apps.analytics.tests.helpers import make_account

    make_account(workspace, "facebook", "Acme Page")
    html = client.get(reverse("composer:compose", kwargs={"workspace_id": workspace.id})).content.decode()
    # no profile picture: initial tile plus a single badge, never logo-as-avatar and badge
    assert 'aria-hidden="true">A</span>' in html
    assert "connect-ai-pill" in html and ">Connect AI<" in html.replace("\n", "").replace("  ", "")
    cal = client.get(reverse("calendar:calendar", kwargs={"workspace_id": workspace.id})).content.decode()
    assert "open-checklist" in cal
