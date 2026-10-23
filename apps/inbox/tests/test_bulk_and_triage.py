"""Bulk actions (including select-all-matching), keyboard triage and the shortcuts help."""

import json
import re

import pytest
from django.urls import reverse

from apps.accounts.models import User
from apps.inbox.models import InboxMessage, InternalNote
from apps.members.models import OrgMembership, WorkspaceMembership

HTMX = {"HTTP_HX_REQUEST": "true"}


def _url(name, workspace, **kwargs):
    return reverse(f"inbox:{name}", kwargs={"workspace_id": workspace.id, **kwargs})


def _ids(messages):
    return ",".join(str(m.id) for m in messages)


@pytest.fixture
def teammate(db, organization, inbox_workspace):
    member = User.objects.create_user(email="mate@example.com", password="x", name="Mate")
    OrgMembership.objects.filter(user=member).exclude(organization=organization).delete()
    OrgMembership.objects.create(user=member, organization=organization, org_role=OrgMembership.OrgRole.MEMBER)
    WorkspaceMembership.objects.create(
        user=member, workspace=inbox_workspace, workspace_role=WorkspaceMembership.WorkspaceRole.EDITOR
    )
    return member


@pytest.mark.django_db
class TestBulkActions:
    def _post(self, client, workspace, **data):
        return client.post(_url("bulk_action", workspace), data, **HTMX)

    def test_resolve_selected(self, inbox_client, inbox_workspace, make_messages):
        msgs = make_messages(4, status="open")
        response = self._post(inbox_client, inbox_workspace, message_ids=_ids(msgs[:2]), action="resolve")
        assert response.status_code == 200
        assert json.loads(response["HX-Trigger"]) == {"inboxBulkDone": {"count": 2}}
        assert InboxMessage.objects.filter(status="resolved").count() == 2

    def test_change_status_sentiment_and_assign(self, inbox_client, inbox_workspace, make_messages, teammate):
        msgs = make_messages(3)
        self._post(inbox_client, inbox_workspace, message_ids=_ids(msgs), action="status", value="archived")
        assert InboxMessage.objects.filter(status="archived").count() == 3
        self._post(inbox_client, inbox_workspace, message_ids=_ids(msgs), action="sentiment", value="negative")
        negatives = InboxMessage.objects.filter(sentiment="negative", sentiment_source="manual")
        assert negatives.count() == 3
        self._post(inbox_client, inbox_workspace, message_ids=_ids(msgs), action="assign", value=str(teammate.id))
        assert InboxMessage.objects.filter(assigned_to=teammate).count() == 3
        self._post(inbox_client, inbox_workspace, message_ids=_ids(msgs[:1]), action="unassign")
        assert InboxMessage.objects.filter(assigned_to=teammate).count() == 2

    def test_add_note_to_each(self, inbox_client, inbox_workspace, inbox_owner, make_messages):
        msgs = make_messages(3)
        self._post(inbox_client, inbox_workspace, message_ids=_ids(msgs), action="note", value="Escalated to billing")
        notes = InternalNote.objects.all()
        assert notes.count() == 3
        assert {n.author for n in notes} == {inbox_owner}

    def test_invalid_values_are_rejected(self, inbox_client, inbox_workspace, make_messages, teammate):
        msgs = make_messages(1)
        for data in (
            {"action": "status", "value": "bogus"},
            {"action": "sentiment", "value": "angry"},
            {"action": "assign", "value": "not-a-uuid"},
            {"action": "note", "value": "  "},
            {"action": "explode"},
        ):
            assert self._post(inbox_client, inbox_workspace, message_ids=_ids(msgs), **data).status_code == 400
        assert self._post(inbox_client, inbox_workspace, message_ids="nope", action="resolve").status_code == 400
        assert self._post(inbox_client, inbox_workspace, message_ids="", action="resolve").status_code == 400

    def test_cannot_assign_to_someone_outside_the_workspace(self, inbox_client, inbox_workspace, make_messages, db):
        outsider = User.objects.create_user(email="out@example.com", password="x", name="Out")
        msgs = make_messages(1)
        response = self._post(
            inbox_client, inbox_workspace, message_ids=_ids(msgs), action="assign", value=str(outsider.id)
        )
        assert response.status_code == 400
        assert InboxMessage.objects.filter(assigned_to__isnull=False).count() == 0

    def test_ids_from_another_workspace_are_ignored(self, inbox_client, inbox_workspace, other_workspace_message):
        self._post(inbox_client, inbox_workspace, message_ids=str(other_workspace_message.id), action="archive")
        other_workspace_message.refresh_from_db()
        assert other_workspace_message.status == "unread"

    def test_requires_the_reply_permission(self, inbox_viewer_client, inbox_workspace, make_messages):
        msgs = make_messages(1)
        response = self._post(inbox_viewer_client, inbox_workspace, message_ids=_ids(msgs), action="resolve")
        assert response.status_code == 403
        assert InboxMessage.objects.get().status == "unread"

    def test_the_list_is_rerendered_under_the_same_filters(self, inbox_client, inbox_workspace, make_messages):
        keep = make_messages(2, status="open", prefix="keep")
        make_messages(2, status="unread", prefix="gone")
        response = self._post(
            inbox_client, inbox_workspace, message_ids=_ids(keep[:1]), action="archive", filters="status=open"
        )
        shown = set(re.findall(r'id="msg-([0-9a-f-]{36})"', response.content.decode()))
        assert shown == {str(keep[1].id)}


@pytest.mark.django_db
class TestSelectAllMatching:
    def _post(self, client, workspace, **data):
        return client.post(_url("bulk_action", workspace), {"select_all": "1", **data}, **HTMX)

    def test_count_endpoint_matches_the_filter(self, inbox_client, inbox_workspace, make_messages):
        make_messages(7, status="open", prefix="o")
        make_messages(3, status="resolved", prefix="r")
        data = inbox_client.get(_url("bulk_count", inbox_workspace) + "?status=open").json()
        assert data["count"] == 7
        assert data["limit"] > 7

    def test_acts_on_every_match_beyond_one_page(self, inbox_client, inbox_workspace, make_messages):
        make_messages(70, status="open", prefix="o")
        make_messages(5, status="unread", prefix="u")
        response = self._post(inbox_client, inbox_workspace, action="resolve", filters="status=open", expected_count=70)
        assert response.status_code == 200
        assert InboxMessage.objects.filter(status="resolved").count() == 70
        assert InboxMessage.objects.filter(status="unread").count() == 5

    def test_refuses_when_the_count_changed(self, inbox_client, inbox_workspace, make_messages):
        make_messages(5, status="open")
        response = self._post(inbox_client, inbox_workspace, action="resolve", filters="status=open", expected_count=4)
        assert response.status_code == 409
        assert "5 messages match now" in response.content.decode()
        assert InboxMessage.objects.filter(status="resolved").count() == 0

    def test_requires_a_confirmed_count(self, inbox_client, inbox_workspace, make_messages):
        make_messages(2)
        assert self._post(inbox_client, inbox_workspace, action="resolve").status_code == 400

    def test_is_capped(self, inbox_client, inbox_workspace, make_messages, monkeypatch):
        from apps.inbox import services

        monkeypatch.setattr(services, "BULK_SELECT_ALL_MAX", 3)
        make_messages(5)
        response = self._post(inbox_client, inbox_workspace, action="archive", expected_count=5)
        assert response.status_code == 400
        assert InboxMessage.objects.filter(status="archived").count() == 0

    def test_my_queue_filter_uses_the_actor(self, inbox_client, inbox_workspace, inbox_owner, make_messages, teammate):
        make_messages(3, assigned_to=inbox_owner, prefix="mine")
        make_messages(3, assigned_to=teammate, prefix="theirs")
        response = self._post(inbox_client, inbox_workspace, action="archive", filters="view=mine", expected_count=3)
        assert response.status_code == 200
        assert InboxMessage.objects.filter(status="archived", assigned_to=inbox_owner).count() == 3
        assert InboxMessage.objects.filter(status="archived", assigned_to=teammate).count() == 0

    def test_never_touches_another_workspace(
        self, inbox_client, inbox_workspace, other_workspace_message, make_messages
    ):
        make_messages(2)
        response = self._post(inbox_client, inbox_workspace, action="archive", expected_count=2)
        assert response.status_code == 200
        other_workspace_message.refresh_from_db()
        assert other_workspace_message.status == "unread"

    def test_requires_the_reply_permission(self, inbox_viewer_client, inbox_workspace, make_messages):
        make_messages(2)
        assert self._post(inbox_viewer_client, inbox_workspace, action="archive", expected_count=2).status_code == 403
        assert inbox_viewer_client.get(_url("bulk_count", inbox_workspace)).status_code == 403


@pytest.mark.django_db
class TestTriage:
    def _post(self, client, workspace, message, action):
        return client.post(_url("triage", workspace, message_id=message.id), {"action": action}, **HTMX)

    def test_resolve_then_archive(self, inbox_client, inbox_workspace, inbox_message):
        response = self._post(inbox_client, inbox_workspace, inbox_message, "resolve")
        assert response.status_code == 200
        assert f'id="msg-{inbox_message.id}"' in response.content.decode()
        inbox_message.refresh_from_db()
        assert inbox_message.status == "resolved"
        self._post(inbox_client, inbox_workspace, inbox_message, "resolve")
        inbox_message.refresh_from_db()
        assert inbox_message.status == "archived"

    def test_mark_unread_and_assign_to_me(self, inbox_client, inbox_workspace, inbox_message, inbox_owner):
        InboxMessage.objects.filter(id=inbox_message.id).update(status="open")
        self._post(inbox_client, inbox_workspace, inbox_message, "unread")
        self._post(inbox_client, inbox_workspace, inbox_message, "assign_me")
        inbox_message.refresh_from_db()
        assert inbox_message.status == "unread"
        assert inbox_message.assigned_to == inbox_owner

    def test_rejects_unknown_actions_and_other_workspaces(
        self, inbox_client, inbox_workspace, inbox_message, other_workspace_message
    ):
        assert self._post(inbox_client, inbox_workspace, inbox_message, "delete").status_code == 400
        assert self._post(inbox_client, inbox_workspace, other_workspace_message, "resolve").status_code == 404

    def test_viewers_cannot_triage(self, inbox_viewer_client, inbox_workspace, inbox_message):
        assert self._post(inbox_viewer_client, inbox_workspace, inbox_message, "resolve").status_code == 403


@pytest.mark.django_db
class TestShortcutsHelp:
    def test_feed_renders_the_shortcuts_dialog(self, inbox_client, inbox_workspace, inbox_message):
        html = inbox_client.get(_url("feed", inbox_workspace)).content.decode()
        assert '<dialog id="inbox-shortcuts"' in html
        assert 'aria-labelledby="inbox-shortcuts-title"' in html
        for key in ("j", "k", "Enter", "e", "r", "a", "u", "x", "/", "?"):
            assert f'<kbd class="inbox-kbd">{key}</kbd>' in html
        assert "js/inbox-keyboard.js" in html

    def test_rows_carry_what_the_keyboard_needs(self, inbox_client, inbox_workspace, inbox_message):
        html = inbox_client.get(_url("feed", inbox_workspace)).content.decode()
        assert f'data-message-id="{inbox_message.id}"' in html
        assert f"/{inbox_message.id}/triage/" in html
        assert 'tabindex="0"' in html
        assert ".inbox-row:focus-visible" in html

    def test_new_markup_has_no_inline_styles(self, inbox_client, inbox_workspace, inbox_message):
        from pathlib import Path

        root = Path(__file__).resolve().parents[3] / "templates/inbox/partials"
        for name in ("_poll.html", "_message_page.html", "_ai_draft_result.html"):
            assert "style=" not in (root / name).read_text()
