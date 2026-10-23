"""Checklist computation: each item, the invite fix and the widget's visibility."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.ai_writer.models import AIRequestLog
from apps.calendar.models import PostingSlot
from apps.composer.models import PlatformPost, Post
from apps.members.models import Invitation, WorkspaceMembership
from apps.onboarding.checklist import confirm_item, get_checklist_items
from apps.onboarding.models import OnboardingChecklist, OnboardingFlow

pytestmark = pytest.mark.django_db

Role = WorkspaceMembership.WorkspaceRole


def _done(items):
    return {item["key"]: item["completed"] for item in items}


def test_fresh_workspace_has_five_open_items(workspace, owner):
    items = get_checklist_items(workspace, owner)
    assert [i["key"] for i in items] == [
        "connect_accounts",
        "schedule_post",
        "brand_voice",
        "invite_members",
        "posting_times",
    ]
    assert not any(i["completed"] for i in items)


def test_connect_channel(workspace, owner, account):
    assert _done(get_checklist_items(workspace, owner))["connect_accounts"]


def test_schedule_post_needs_a_scheduled_post_not_a_draft(workspace, owner, account):
    post = Post.objects.create(workspace=workspace, author=owner, caption="hi")
    pp = PlatformPost.objects.create(post=post, social_account=account, status="draft")
    assert not _done(get_checklist_items(workspace, owner))["schedule_post"]
    pp.status = "scheduled"
    pp.save()
    assert _done(get_checklist_items(workspace, owner))["schedule_post"]


def test_brand_voice(workspace, owner):
    assert not _done(get_checklist_items(workspace, owner))["brand_voice"]
    workspace.brand_voice_tone = "Warm and direct"
    workspace.save()
    assert _done(get_checklist_items(workspace, owner))["brand_voice"]


@pytest.mark.parametrize("role", [Role.EDITOR, Role.MANAGER, Role.CONTRIBUTOR, Role.VIEWER, Role.CLIENT])
def test_invite_teammate_completes_for_any_role(workspace, owner, make_member, role):
    """Regression: only a CLIENT-role member used to complete this item."""
    assert not _done(get_checklist_items(workspace, owner))["invite_members"]
    make_member("mate@example.com", role)
    assert _done(get_checklist_items(workspace, owner))["invite_members"]


def test_pending_invitation_counts_as_inviting(workspace, owner):
    Invitation.objects.create(
        organization=workspace.organization,
        email="soon@example.com",
        invited_by=owner,
        expires_at=timezone.now() + timedelta(days=7),
    )
    assert _done(get_checklist_items(workspace, owner))["invite_members"]


def test_owner_alone_is_not_a_teammate(workspace, owner):
    """The owner's own membership never counts as a teammate."""
    assert WorkspaceMembership.objects.filter(workspace=workspace).count() == 1
    assert not _done(get_checklist_items(workspace, owner))["invite_members"]


def test_posting_times_default_slots_do_not_count(workspace, owner, account):
    PostingSlot.objects.create(social_account=account, day_of_week=1, time="09:00")
    assert not _done(get_checklist_items(workspace, owner))["posting_times"]


def test_posting_times_complete_when_edited(workspace, owner, account):
    slot = PostingSlot.objects.create(social_account=account, day_of_week=1, time="09:00")
    PostingSlot.objects.filter(pk=slot.pk).update(updated_at=timezone.now() + timedelta(minutes=5))
    assert _done(get_checklist_items(workspace, owner))["posting_times"]


def test_posting_times_complete_when_confirmed(workspace, owner):
    confirm_item(owner, workspace, "posting_times")
    assert _done(get_checklist_items(workspace, owner))["posting_times"]


def test_ai_item_only_when_configured(workspace, owner):
    with patch("apps.ai_writer.client.ai_available", return_value=False):
        assert "try_ai" not in _done(get_checklist_items(workspace, owner))
    with patch("apps.ai_writer.client.ai_available", return_value=True):
        assert _done(get_checklist_items(workspace, owner))["try_ai"] is False
        AIRequestLog.objects.create(
            organization=workspace.organization,
            workspace=workspace,
            user=owner,
            action="write",
            provider="anthropic",
            model="m",
            status=AIRequestLog.Status.OK,
        )
        assert _done(get_checklist_items(workspace, owner))["try_ai"] is True


def test_client_item_only_for_agencies(workspace, owner, flow, make_member):
    assert "connect_client" not in _done(get_checklist_items(workspace, owner))
    flow.account_kind = OnboardingFlow.AccountKind.AGENCY
    flow.save()
    assert _done(get_checklist_items(workspace, owner))["connect_client"] is False
    make_member("client@example.com", Role.CLIENT)
    assert _done(get_checklist_items(workspace, owner))["connect_client"] is True


def test_icons_are_server_constants(workspace, owner, flow):
    flow.account_kind = "agency"
    flow.save()
    for item in get_checklist_items(workspace, owner):
        assert item["icon_svg"].startswith("<")


# --- the widget ---------------------------------------------------------------


def _calendar(client, workspace):
    return client.get(reverse("calendar:calendar", kwargs={"workspace_id": workspace.id}))


def test_widget_renders_for_owner(owner_client, workspace):
    html = _calendar(owner_client, workspace).content.decode()
    assert 'id="onboarding-checklist"' in html
    assert "Invite a teammate" in html
    assert "0 of 5 complete" in html


def test_widget_has_no_inline_styles(owner_client, workspace):
    html = _calendar(owner_client, workspace).content.decode()
    widget = html[html.index('id="onboarding-checklist"') :]
    widget = widget[: widget.index("</aside>")]
    assert "style=" not in widget


def test_widget_is_a_pill_below_the_composer_sheets(owner_client, workspace):
    html = _calendar(owner_client, workspace).content.decode()
    widget = html[html.index('id="onboarding-checklist"') :][:900]
    assert "z-30" in widget  # composer bottom sheets are z-60
    assert "open: false" in widget  # starts as the pill on every screen
    assert "localStorage.getItem('checklistOpen')" in widget  # remembers the user's choice


def test_widget_hidden_on_phones_inside_the_composer(owner_client, workspace):
    html = owner_client.get(reverse("composer:compose", kwargs={"workspace_id": workspace.id})).content.decode()
    widget = html[html.index('id="onboarding-checklist"') :][:900]
    assert "max-sm:hidden" in widget


def test_widget_not_shown_to_invited_members(client, workspace, make_member):
    editor = make_member("editor@example.com", Role.EDITOR)
    client.force_login(editor)
    assert 'id="onboarding-checklist"' not in _calendar(client, workspace).content.decode()


def test_dismiss_is_remembered_server_side(owner_client, workspace, owner):
    url = reverse("onboarding:dismiss_checklist", kwargs={"workspace_id": workspace.id})
    assert owner_client.post(url, HTTP_HX_REQUEST="true").status_code == 200
    assert OnboardingChecklist.objects.get(user=owner, workspace=workspace).is_dismissed
    assert 'id="onboarding-checklist"' not in _calendar(owner_client, workspace).content.decode()


def test_dismiss_keeps_confirmed_items(owner_client, workspace, owner):
    confirm_item(owner, workspace, "posting_times")
    owner_client.post(reverse("onboarding:dismiss_checklist", kwargs={"workspace_id": workspace.id}))
    assert OnboardingChecklist.objects.get(user=owner, workspace=workspace).confirmed_items == ["posting_times"]


def test_confirm_item_view(owner_client, workspace, owner):
    url = reverse("onboarding:confirm_checklist_item", kwargs={"workspace_id": workspace.id, "key": "posting_times"})
    assert owner_client.post(url).status_code == 302
    assert _done(get_checklist_items(workspace, owner))["posting_times"]
    bad = reverse("onboarding:confirm_checklist_item", kwargs={"workspace_id": workspace.id, "key": "connect_accounts"})
    assert owner_client.post(bad).status_code == 404


def test_resume_link_offered_after_skipping(owner_client, workspace, flow):
    flow.status = OnboardingFlow.Status.SKIPPED
    flow.save()
    html = _calendar(owner_client, workspace).content.decode()
    assert "Resume the setup guide" in html
