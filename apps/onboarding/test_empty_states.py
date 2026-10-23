"""The shared empty-state component and the pages that use it."""

import pytest
from django.template import Context, Template
from django.urls import reverse

from apps.members.models import WorkspaceMembership
from apps.onboarding import empty_states

pytestmark = pytest.mark.django_db


def render_tag(workspace, key, extra="", user=None):
    from django.test import RequestFactory

    request = RequestFactory().get("/")
    request.workspace_membership = WorkspaceMembership.objects.filter(workspace=workspace).first()
    return Template('{% load onboarding_tags %}{% empty_state "' + key + '" workspace ' + extra + "%}").render(
        Context({"workspace": workspace, "request": request, "csrf_token": "tok"})
    )


@pytest.mark.parametrize("key", ["calendar", "inbox", "analytics", "media", "evergreen", "reports"])
def test_every_empty_state_renders_title_explanation_and_one_action(workspace, account, key):
    html = render_tag(workspace, key)
    assert f'data-empty-state="{key}"' in html
    state = empty_states.empty_state(key, workspace)
    assert state["title"] in html and state["body"] in html
    assert html.count("btn-brand") == 1
    assert "style=" not in html


@pytest.mark.parametrize("key", ["calendar", "inbox", "analytics"])
def test_channel_pages_ask_to_connect_when_nothing_is_connected(workspace, key):
    html = render_tag(workspace, key)
    assert "Connect a channel" in html
    assert reverse("social_accounts:connect", kwargs={"workspace_id": workspace.id}) in html


def test_channel_pages_offer_a_post_once_a_channel_exists(workspace, account):
    html = render_tag(workspace, "inbox")
    assert "Create a post" in html


def test_action_can_be_left_out(workspace):
    assert "btn-brand" not in render_tag(workspace, "reports", "show_action=False")


def test_unknown_state_raises(workspace):
    with pytest.raises(KeyError):
        empty_states.empty_state("nope", workspace)


def test_icons_are_constants():
    assert all(icon.startswith("<") for icon in empty_states._ICONS.values())


# --- the real pages ---------------------------------------------------------------------


def get(client, name, workspace, **params):
    return client.get(reverse(name, kwargs={"workspace_id": workspace.id}), params)


def test_calendar_queue_empty_state(owner_client, workspace, account):
    html = get(owner_client, "calendar:publish_tab_queue", workspace).content.decode()
    assert 'data-empty-state="calendar"' in html and "Create a post" in html


def test_calendar_without_channels_empty_state(owner_client, workspace):
    html = get(owner_client, "calendar:calendar", workspace, mode="list").content.decode()
    assert 'data-empty-state="calendar"' in html and "Connect your first channel" in html


def test_inbox_empty_state(owner_client, workspace, account):
    html = get(owner_client, "inbox:feed", workspace).content.decode()
    assert 'data-empty-state="inbox"' in html


def test_analytics_empty_state(owner_client, workspace):
    html = get(owner_client, "analytics:index", workspace).content.decode()
    assert 'data-empty-state="analytics"' in html


def test_media_library_empty_state(owner_client, workspace):
    html = get(owner_client, "media_library:index", workspace).content.decode()
    assert 'data-empty-state="media"' in html and "Upload media" in html


def test_media_library_search_miss_keeps_its_own_message(owner_client, workspace):
    html = get(owner_client, "media_library:index", workspace, q="zzz").content.decode()
    assert "No media matches your search" in html


def test_reports_empty_state(owner_client, workspace):
    html = get(owner_client, "reports:list", workspace).content.decode()
    assert 'data-empty-state="reports"' in html
