"""Workspace settings pages for hashtag sets and signatures."""

from django.contrib.messages import get_messages
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.composer.models import HashtagSet, Signature
from apps.composer.tests.test_unsplash import ComposerTestCase
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace


def _member(workspace, role, email):
    user = User.objects.create_user(email=email, password="testpass123", tos_accepted_at=timezone.now())
    OrgMembership.objects.create(user=user, organization=workspace.organization, org_role=OrgMembership.OrgRole.MEMBER)
    WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role=role)
    return user


class SettingsTestCase(ComposerTestCase):
    def url(self, name):
        return reverse(f"workspaces:{name}", kwargs={"workspace_id": self.workspace.id})

    def messages(self, response):
        return [str(m) for m in get_messages(response.wsgi_request)]


class HashtagSetSettingsTests(SettingsTestCase):
    def test_owner_creates_a_set_from_loose_text(self):
        response = self.client.post(
            self.url("hashtag_sets_settings"),
            {"action": "create", "name": "Coffee", "hashtags": "coffee, #latte coffee"},
        )
        self.assertRedirects(response, self.url("hashtag_sets_settings"))
        hashtag_set = HashtagSet.objects.get(workspace=self.workspace)
        self.assertEqual(
            (hashtag_set.name, hashtag_set.hashtags, hashtag_set.created_by),
            ("Coffee", ["#coffee", "#latte"], self.user),
        )

    def test_update_and_delete(self):
        hashtag_set = HashtagSet.objects.create(workspace=self.workspace, name="Old", hashtags=["#a"])
        self.client.post(
            self.url("hashtag_sets_settings"),
            {"action": "update", "set_id": hashtag_set.id, "name": "New", "hashtags": "#b #c"},
        )
        hashtag_set.refresh_from_db()
        self.assertEqual((hashtag_set.name, hashtag_set.hashtags), ("New", ["#b", "#c"]))

        self.client.post(self.url("hashtag_sets_settings"), {"action": "delete", "set_id": hashtag_set.id})
        self.assertFalse(HashtagSet.objects.exists())

    def test_rejects_bad_input_without_saving(self):
        HashtagSet.objects.create(workspace=self.workspace, name="Taken", hashtags=["#a"])
        cases = [
            {"name": "", "hashtags": "#a"},
            {"name": "Empty", "hashtags": "!!! 123"},
            {"name": "taken", "hashtags": "#a"},
            {"name": "Many", "hashtags": " ".join(f"#t{n}" for n in range(31))},
        ]
        for count, case in enumerate(cases, start=1):
            response = self.client.post(self.url("hashtag_sets_settings"), {"action": "create", **case})
            # Messages pile up in the session until a page renders them.
            self.assertEqual(len(self.messages(response)), count, case)
        self.assertEqual(HashtagSet.objects.count(), 1)

    def test_other_workspaces_sets_cannot_be_touched(self):
        other = Workspace.objects.create(organization=Organization.objects.create(name="O"), name="Other")
        theirs = HashtagSet.objects.create(workspace=other, name="Theirs", hashtags=["#x"])
        for payload in (
            {"action": "update", "set_id": theirs.id, "name": "Mine now", "hashtags": "#y"},
            {"action": "delete", "set_id": theirs.id},
            {"action": "delete", "set_id": "not-a-uuid"},
        ):
            self.assertEqual(self.client.post(self.url("hashtag_sets_settings"), payload).status_code, 404)
        theirs.refresh_from_db()
        self.assertEqual(theirs.name, "Theirs")

    def test_editor_can_read_but_not_write(self):
        editor = _member(self.workspace, WorkspaceMembership.WorkspaceRole.EDITOR, "editor@example.com")
        HashtagSet.objects.create(workspace=self.workspace, name="Coffee", hashtags=["#coffee"])
        self.client.force_login(editor)

        page = self.client.get(self.url("hashtag_sets_settings"))
        self.assertContains(page, "Coffee")
        self.assertNotContains(page, 'value="create"')
        response = self.client.post(
            self.url("hashtag_sets_settings"), {"action": "create", "name": "X", "hashtags": "#x"}
        )
        self.assertEqual(response.status_code, 404)

    def test_non_members_are_refused(self):
        stranger = User.objects.create_user(email="s@example.com", password="x", tos_accepted_at=timezone.now())
        self.client.force_login(stranger)
        self.assertEqual(self.client.get(self.url("hashtag_sets_settings")).status_code, 403)


class SignatureSettingsTests(SettingsTestCase):
    def test_creates_personal_and_workspace_signatures(self):
        self.client.post(self.url("signatures_settings"), {"action": "create", "name": "Mine", "body": "me"})
        self.client.post(
            self.url("signatures_settings"), {"action": "create", "name": "House", "body": "us", "scope": "workspace"}
        )
        self.assertEqual(Signature.objects.get(name="Mine").user, self.user)
        self.assertIsNone(Signature.objects.get(name="Mine").workspace)
        self.assertEqual(Signature.objects.get(name="House").workspace, self.workspace)
        self.assertIsNone(Signature.objects.get(name="House").user)

    def test_a_new_default_replaces_the_old_one_for_the_same_owner(self):
        first = Signature.objects.create(user=self.user, name="First", body="1", is_default=True)
        workspace_default = Signature.objects.create(workspace=self.workspace, name="House", body="h", is_default=True)

        self.client.post(
            self.url("signatures_settings"), {"action": "create", "name": "Second", "body": "2", "is_default": "on"}
        )

        first.refresh_from_db()
        workspace_default.refresh_from_db()
        self.assertFalse(first.is_default)
        self.assertTrue(workspace_default.is_default)
        self.assertTrue(Signature.objects.get(name="Second").is_default)

    def test_update_can_clear_the_default(self):
        signature = Signature.objects.create(user=self.user, name="S", body="s", is_default=True)
        self.client.post(
            self.url("signatures_settings"),
            {"action": "update", "signature_id": signature.id, "name": "S", "body": "t"},
        )
        signature.refresh_from_db()
        self.assertEqual((signature.body, signature.is_default), ("t", False))

    def test_delete(self):
        signature = Signature.objects.create(user=self.user, name="S", body="s")
        self.client.post(self.url("signatures_settings"), {"action": "delete", "signature_id": signature.id})
        self.assertFalse(Signature.objects.exists())

    def test_rejects_bad_input(self):
        payloads = ({"name": "", "body": "x"}, {"name": "x", "body": " "}, {"name": "x", "body": "y" * 1001})
        for count, payload in enumerate(payloads, start=1):
            response = self.client.post(self.url("signatures_settings"), {"action": "create", **payload})
            self.assertEqual(len(self.messages(response)), count, payload)
        self.assertFalse(Signature.objects.exists())

    def test_members_manage_their_own_but_not_the_workspaces(self):
        editor = _member(self.workspace, WorkspaceMembership.WorkspaceRole.EDITOR, "editor@example.com")
        shared = Signature.objects.create(workspace=self.workspace, name="House", body="h")
        self.client.force_login(editor)

        self.client.post(self.url("signatures_settings"), {"action": "create", "name": "Mine", "body": "m"})
        self.assertEqual(Signature.objects.get(name="Mine").user, editor)

        for payload in (
            {"action": "create", "name": "Sneaky", "body": "s", "scope": "workspace"},
            {"action": "update", "signature_id": shared.id, "name": "House", "body": "hacked"},
            {"action": "delete", "signature_id": shared.id},
        ):
            self.assertEqual(self.client.post(self.url("signatures_settings"), payload).status_code, 404)
        shared.refresh_from_db()
        self.assertEqual(shared.body, "h")

    def test_other_users_personal_signatures_are_untouchable(self):
        editor = _member(self.workspace, WorkspaceMembership.WorkspaceRole.EDITOR, "editor@example.com")
        theirs = Signature.objects.create(user=editor, name="Theirs", body="t")
        # Even the workspace owner cannot see or change a teammate's personal signature.
        page = self.client.get(self.url("signatures_settings"))
        self.assertNotContains(page, "Theirs")
        for payload in (
            {"action": "update", "signature_id": theirs.id, "name": "Theirs", "body": "hacked"},
            {"action": "delete", "signature_id": theirs.id},
        ):
            self.assertEqual(self.client.post(self.url("signatures_settings"), payload).status_code, 404)
        self.assertTrue(Signature.objects.filter(pk=theirs.pk, body="t").exists())

    def test_other_workspaces_signatures_are_untouchable(self):
        other = Workspace.objects.create(organization=Organization.objects.create(name="O"), name="Other")
        theirs = Signature.objects.create(workspace=other, name="Theirs", body="t")
        response = self.client.post(self.url("signatures_settings"), {"action": "delete", "signature_id": theirs.id})
        self.assertEqual(response.status_code, 404)
        self.assertTrue(Signature.objects.filter(pk=theirs.pk).exists())

    def test_non_members_are_refused(self):
        stranger = User.objects.create_user(email="s@example.com", password="x", tos_accepted_at=timezone.now())
        self.client.force_login(stranger)
        self.assertEqual(self.client.get(self.url("signatures_settings")).status_code, 403)
