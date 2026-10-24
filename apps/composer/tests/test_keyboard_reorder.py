"""Keyboard/touch alternatives to dragging: move media earlier/later, move an idea to another column."""

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.composer.models import Idea, IdeaGroup, Post, PostMedia
from apps.composer.views import _step_in_order
from apps.media_library.models import MediaAsset
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace


class StepInOrderTests(TestCase):
    def test_moves_one_place(self):
        self.assertEqual(_step_in_order(["a", "b", "c"], "b", "earlier"), ["b", "a", "c"])
        self.assertEqual(_step_in_order(["a", "b", "c"], "b", "later"), ["a", "c", "b"])

    def test_edges_unknown_target_and_bad_direction_leave_order(self):
        self.assertEqual(_step_in_order(["a", "b"], "a", "earlier"), ["a", "b"])
        self.assertEqual(_step_in_order(["a", "b"], "b", "later"), ["a", "b"])
        self.assertEqual(_step_in_order(["a", "b"], "z", "later"), ["a", "b"])
        self.assertEqual(_step_in_order(["a", "b"], "a", "sideways"), ["a", "b"])


class KeyboardReorderTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Org")
        self.workspace = Workspace.objects.create(organization=self.org, name="Workspace")
        self.other_workspace = Workspace.objects.create(organization=self.org, name="Other")
        self.editor = self._member("editor@example.com", WorkspaceMembership.WorkspaceRole.EDITOR)
        self.viewer = self._member("viewer@example.com", WorkspaceMembership.WorkspaceRole.VIEWER)
        self.post = Post.objects.create(workspace=self.workspace, author=self.editor, caption="hello")
        self.assets = [self._asset(f"a{i}.png") for i in range(3)]
        self.attachments = [PostMedia.objects.create(post=self.post, media_asset=a, position=i) for i, a in enumerate(self.assets)]
        self.client.force_login(self.editor)

    def _member(self, email, role):
        user = User.objects.create_user(email=email, password="testpass123", tos_accepted_at=timezone.now())
        OrgMembership.objects.create(user=user, organization=self.org, org_role=OrgMembership.OrgRole.MEMBER)
        WorkspaceMembership.objects.create(user=user, workspace=self.workspace, workspace_role=role)
        return user

    def _asset(self, name):
        return MediaAsset.objects.create(
            organization=self.org,
            workspace=self.workspace,
            uploaded_by=self.editor,
            file=f"media_library/tests/{name}",
            filename=name,
            media_type=MediaAsset.MediaType.IMAGE,
            mime_type="image/png",
            file_size=10,
            source="upload",
        )

    def _order(self):
        return [a.media_asset.filename for a in self.post.media_attachments.order_by("position")]

    def _reorder(self, attachment, direction, post=None):
        url = reverse(
            "composer:reorder_media",
            kwargs={"workspace_id": self.workspace.id, "post_id": (post or self.post).id, "media_id": attachment.id},
        )
        return self.client.post(url, {"direction": direction})

    def test_move_later_then_earlier(self):
        response = self._reorder(self.attachments[0], "later")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._order(), ["a1.png", "a0.png", "a2.png"])
        self.assertContains(response, f'id="move-earlier-{self.attachments[0].id}"')
        self._reorder(self.attachments[0], "earlier")
        self.assertEqual(self._order(), ["a0.png", "a1.png", "a2.png"])

    def test_edge_move_is_a_no_op(self):
        self.assertEqual(self._reorder(self.attachments[0], "earlier").status_code, 200)
        self.assertEqual(self._order(), ["a0.png", "a1.png", "a2.png"])

    def test_viewer_cannot_reorder(self):
        self.client.force_login(self.viewer)
        self.assertEqual(self._reorder(self.attachments[0], "later").status_code, 403)
        self.assertEqual(self._order(), ["a0.png", "a1.png", "a2.png"])

    def test_attachment_of_another_post_is_not_found(self):
        other = Post.objects.create(workspace=self.workspace, author=self.editor, caption="other")
        self.assertEqual(self._reorder(self.attachments[0], "later", post=other).status_code, 404)
        self.assertEqual(self._order(), ["a0.png", "a1.png", "a2.png"])

    def test_pending_media_order_lives_in_the_session(self):
        key = f"pending_media_{self.workspace.id}"
        session = self.client.session
        session[key] = [str(a.id) for a in self.assets]
        session.save()
        url = reverse(
            "composer:reorder_pending_media",
            kwargs={"workspace_id": self.workspace.id, "asset_id": self.assets[2].id},
        )
        response = self.client.post(url, {"direction": "earlier"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.session[key], [str(self.assets[0].id), str(self.assets[2].id), str(self.assets[1].id)])

    def test_pending_reorder_denied_for_viewer(self):
        self.client.force_login(self.viewer)
        url = reverse(
            "composer:reorder_pending_media",
            kwargs={"workspace_id": self.workspace.id, "asset_id": self.assets[0].id},
        )
        self.assertEqual(self.client.post(url, {"direction": "later"}).status_code, 403)


class IdeaMovePermissionTests(TestCase):
    """The card's "Move to" menu posts to idea_move, the same endpoint drag-and-drop uses."""

    def setUp(self):
        self.org = Organization.objects.create(name="Org")
        self.workspace = Workspace.objects.create(organization=self.org, name="Workspace")
        self.other = Workspace.objects.create(organization=self.org, name="Other")
        self.editor = self._member("editor@example.com", WorkspaceMembership.WorkspaceRole.EDITOR)
        self.viewer = self._member("viewer@example.com", WorkspaceMembership.WorkspaceRole.VIEWER)
        self.todo = IdeaGroup.objects.create(workspace=self.workspace, name="To do", position=0)
        self.done = IdeaGroup.objects.create(workspace=self.workspace, name="Done", position=1)
        self.foreign = IdeaGroup.objects.create(workspace=self.other, name="Foreign", position=0)
        self.idea = Idea.objects.create(workspace=self.workspace, author=self.editor, title="Idea", group=self.todo)
        self.url = reverse("composer:idea_move", kwargs={"workspace_id": self.workspace.id, "idea_id": self.idea.id})

    def _member(self, email, role):
        user = User.objects.create_user(email=email, password="testpass123", tos_accepted_at=timezone.now())
        OrgMembership.objects.create(user=user, organization=self.org, org_role=OrgMembership.OrgRole.MEMBER)
        WorkspaceMembership.objects.create(user=user, workspace=self.workspace, workspace_role=role)
        return user

    def test_editor_moves_idea_to_another_column(self):
        self.client.force_login(self.editor)
        self.assertEqual(self.client.post(self.url, {"group": str(self.done.id), "position": "0"}).status_code, 204)
        self.idea.refresh_from_db()
        self.assertEqual(self.idea.group_id, self.done.id)

    def test_viewer_cannot_move_idea(self):
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.post(self.url, {"group": str(self.done.id)}).status_code, 403)
        self.idea.refresh_from_db()
        self.assertEqual(self.idea.group_id, self.todo.id)

    def test_column_from_another_workspace_is_ignored(self):
        self.client.force_login(self.editor)
        self.client.post(self.url, {"group": str(self.foreign.id)})
        self.idea.refresh_from_db()
        self.assertEqual(self.idea.group_id, self.todo.id)

    def test_board_offers_move_menu_and_column_menu(self):
        self.client.force_login(self.editor)
        response = self.client.get(reverse("composer:idea_board", kwargs={"workspace_id": self.workspace.id}))
        self.assertContains(response, "Move to")
        self.assertContains(response, "Move left")
