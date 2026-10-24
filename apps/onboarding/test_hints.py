"""First-visit hints on the composer and calendar, remembered per user."""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.members.models import WorkspaceMembership
from apps.onboarding import hints
from apps.onboarding.models import DismissedHint

pytestmark = pytest.mark.django_db


def dismiss_url(key):
    return reverse("onboarding:dismiss_hint", kwargs={"key": key})


def page(client, name, workspace):
    return client.get(reverse(name, kwargs={"workspace_id": workspace.id})).content.decode()


def test_composer_and_calendar_show_a_hint_on_first_visit(owner_client, workspace):
    assert 'data-hint="composer"' in page(owner_client, "composer:compose", workspace)
    assert 'data-hint="calendar"' in page(owner_client, "calendar:calendar", workspace)


def test_hints_are_dismissible_without_inline_styles(owner_client, workspace):
    html = page(owner_client, "calendar:calendar", workspace)
    hint = html[html.index('data-hint="calendar"') :]
    hint = hint[: hint.index("</button>")]
    assert "Got it" in hint and "style=" not in hint and 'role="region"' in hint
    assert 'hx-post="' + dismiss_url("calendar") + '"' in hint


def test_dismissal_is_stored_and_survives_reloads(owner_client, owner, workspace):
    assert owner_client.post(dismiss_url("composer")).status_code == 200
    assert DismissedHint.objects.filter(user=owner, key="composer").exists()
    assert 'data-hint="composer"' not in page(owner_client, "composer:compose", workspace)
    assert 'data-hint="calendar"' in page(owner_client, "calendar:calendar", workspace)


def test_dismissing_twice_is_harmless(owner_client, owner):
    owner_client.post(dismiss_url("composer"))
    assert owner_client.post(dismiss_url("composer")).status_code == 200
    assert DismissedHint.objects.filter(user=owner).count() == 1


def test_unknown_hint_is_404(owner_client):
    assert owner_client.post(dismiss_url("nonsense")).status_code == 404
    assert not DismissedHint.objects.exists()


def test_dismissal_is_per_user(client, workspace, owner, owner_client):
    other = User.objects.create_user(email="other@example.com", password="pw12345678", tos_accepted_at=timezone.now())
    hints.dismiss(owner, "composer")
    assert hints.hint_for(other, "composer") is not None
    assert hints.hint_for(owner, "composer") is None


def test_hints_need_post_and_login(client, owner_client):
    assert owner_client.get(dismiss_url("composer")).status_code == 405
    client.logout()
    assert client.post(dismiss_url("composer")).status_code == 302


def test_established_accounts_are_not_greeted_with_tips(owner, workspace, owner_client):
    User.objects.filter(pk=owner.pk).update(created_at=timezone.now() - timedelta(days=90))
    assert 'data-hint="composer"' not in page(owner_client, "composer:compose", workspace)


def test_invited_members_get_hints_too(client, workspace, make_member):
    client.force_login(make_member("editor@example.com", WorkspaceMembership.WorkspaceRole.EDITOR))
    assert 'data-hint="calendar"' in page(client, "calendar:calendar", workspace)
