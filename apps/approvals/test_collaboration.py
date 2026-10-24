"""Comment-only review and team-only notes.

A reviewer can comment on a post in review without deciding it; the comment
tells the author and shows in the post's activity. Notes are a separate,
team-only thread: no client path reads them, and nothing mentioned in one is
sent to someone who may not read notes.
"""

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.approvals import notes as note_service
from apps.approvals.comments import create_comment
from apps.approvals.models import ApprovalAction, PostComment, PostNote
from apps.client_portal.models import MagicLinkToken
from apps.composer.models import PlatformPost, Post
from apps.members.models import CustomRole, OrgMembership, WorkspaceMembership
from apps.notifications.models import EventType, Notification
from apps.organizations.models import Organization
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

SECRET = "SECRET-TEAM-NOTE-TEXT"


def _user(email, name=""):
    user = User.objects.create_user(email=email, password="pw-12345", name=name, tos_accepted_at=timezone.now())
    auto_org_ids = list(OrgMembership.objects.filter(user=user).values_list("organization_id", flat=True))
    WorkspaceMembership.objects.filter(user=user).delete()
    OrgMembership.objects.filter(user=user).delete()
    Organization.objects.filter(id__in=auto_org_ids).delete()
    return user


class CollabBase(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Org")
        self.ws = Workspace.objects.create(
            organization=self.org, name="WS", approval_workflow_mode="required_internal_and_client"
        )
        self.account = SocialAccount.objects.create(
            workspace=self.ws,
            platform="linkedin_personal",
            account_platform_id="li-1",
            account_name="LI",
            connection_status="connected",
        )
        self.author = self.member("author@example.com", "contributor", "Ada Author")
        self.manager = self.member("manager@example.com", "manager", "Max Manager")
        self.editor = self.member("editor@example.com", "editor", "Edie Editor")
        self.viewer = self.member("viewer@example.com", "viewer", "Vic Viewer")
        self.client_user = self.member("client@example.com", "client", "Cleo Client")
        self.post = self.make_post("pending_review")

    def member(self, email, role, name="", custom_role=None):
        user = _user(email, name)
        OrgMembership.objects.create(user=user, organization=self.org, org_role="member")
        WorkspaceMembership.objects.create(user=user, workspace=self.ws, workspace_role=role, custom_role=custom_role)
        return user

    def make_post(self, status):
        post = Post.objects.create(workspace=self.ws, author=self.author, caption="Launch day copy")
        PlatformPost.objects.create(post=post, social_account=self.account, status=status)
        return post

    def comment_only_user(self, email="commenter@example.com"):
        role = CustomRole.objects.create(
            organization=self.org, name="Comment only", permissions={"comment_on_posts": True}
        )
        return self.member(email, "viewer", "Cam Commenter", custom_role=role)

    def url(self, name, **kwargs):
        return reverse(f"approvals:{name}", kwargs={"workspace_id": self.ws.id, "post_id": self.post.id, **kwargs})


class CommentOnlyReviewTests(CollabBase):
    def test_a_reviewer_comments_without_deciding(self):
        self.client.force_login(self.manager)
        response = self.client.post(
            self.url("comment_only"), {"comment": "Check the hashtag", "visibility": "internal"}
        )
        self.assertEqual(response.status_code, 204)
        self.assertEqual(PlatformPost.objects.get(post=self.post).status, "pending_review")
        comment = PostComment.objects.get(post=self.post)
        self.assertEqual(
            (comment.author, comment.body, comment.visibility), (self.manager, "Check the hashtag", "internal")
        )

    def test_the_comment_shows_in_the_posts_activity_and_tells_the_author(self):
        self.client.force_login(self.manager)
        self.client.post(self.url("comment_only"), {"comment": "Check the hashtag"})
        action = ApprovalAction.objects.get(post=self.post)
        self.assertEqual((action.action, action.user, action.comment), ("commented", self.manager, "Check the hashtag"))
        note = Notification.objects.get(user=self.author, event_type=EventType.POST_COMMENT)
        self.assertIn("Max Manager", note.title)
        self.assertEqual(note.data["post_id"], str(self.post.id))
        # The commenter is not told about their own comment.
        self.assertFalse(Notification.objects.filter(user=self.manager).exists())

    def test_the_composer_history_lists_the_comment(self):
        self.client.force_login(self.manager)
        self.client.post(self.url("comment_only"), {"comment": "Check the hashtag"})
        response = self.client.get(
            reverse("composer:compose_edit", kwargs={"workspace_id": self.ws.id, "post_id": self.post.id})
        )
        self.assertContains(response, "commented")

    def test_a_comment_only_role_can_comment_but_not_approve(self):
        commenter = self.comment_only_user()
        self.client.force_login(commenter)
        self.assertEqual(self.client.post(self.url("comment_only"), {"comment": "Looks fine to me"}).status_code, 204)
        self.assertEqual(self.client.post(self.url("approve")).status_code, 403)
        self.assertEqual(self.client.post(self.url("request_changes"), {"comment": "x"}).status_code, 403)
        self.assertEqual(PlatformPost.objects.get(post=self.post).status, "pending_review")

    def test_a_role_denied_comments_cannot_comment(self):
        role = CustomRole.objects.create(
            organization=self.org, name="Read only", permissions={"comment_on_posts": False}
        )
        silent = self.member("silent@example.com", "editor", custom_role=role)
        self.client.force_login(silent)
        self.assertEqual(self.client.post(self.url("comment_only"), {"comment": "hi"}).status_code, 403)
        self.assertEqual(self.client.post(self.url("add_comment"), {"body": "hi"}).status_code, 403)
        self.assertFalse(PostComment.objects.exists())

    def test_an_older_custom_role_keeps_commenting(self):
        """A role saved before the key existed has no entry for it: it keeps what its base role grants."""
        role = CustomRole.objects.create(organization=self.org, name="Old role", permissions={"create_posts": True})
        legacy = self.member("legacy@example.com", "contributor", custom_role=role)
        self.client.force_login(legacy)
        self.assertEqual(self.client.post(self.url("comment_only"), {"comment": "still works"}).status_code, 204)

    def test_an_empty_comment_is_refused_with_a_message(self):
        self.client.force_login(self.manager)
        response = self.client.post(self.url("comment_only"), {"comment": "  "})
        self.assertIn("Write a comment first", response.headers["HX-Trigger"])
        self.assertFalse(PostComment.objects.exists())

    def test_a_client_on_the_web_surface_cannot_write_an_internal_comment(self):
        self.client.force_login(self.client_user)
        self.client.post(self.url("comment_only"), {"comment": "hello", "visibility": "internal"})
        self.assertEqual(PostComment.objects.get().visibility, "external")

    def test_other_workspaces_posts_are_not_reachable(self):
        other_ws = Workspace.objects.create(organization=self.org, name="Other")
        stranger = _user("stranger@example.com")
        WorkspaceMembership.objects.create(user=stranger, workspace=other_ws, workspace_role="manager")
        self.client.force_login(stranger)
        response = self.client.post(
            reverse("approvals:comment_only", kwargs={"workspace_id": other_ws.id, "post_id": self.post.id}),
            {"comment": "hi"},
        )
        self.assertEqual(response.status_code, 404)

    def test_a_mentioned_author_is_notified_once(self):
        create_comment(self.post, self.manager, "ping @adaauthor please look", "internal")
        mentions = Notification.objects.filter(user=self.author)
        self.assertEqual(
            list(mentions.values_list("event_type", flat=True)),
            [EventType.COMMENT_MENTION],
        )

    def test_the_approvals_row_offers_comment_only_to_a_commenter(self):
        commenter = self.comment_only_user()
        self.client.force_login(commenter)
        response = self.client.get(
            reverse("calendar:publish_tab_approvals", kwargs={"workspace_id": self.ws.id}), HTTP_HX_REQUEST="true"
        )
        html = response.content.decode()
        self.assertIn("Comment only", html)
        self.assertNotIn("Request changes", html)
        self.assertNotIn(">Approve<", html)


class PortalCommentTests(CollabBase):
    def setUp(self):
        super().setUp()
        PlatformPost.objects.filter(post=self.post).update(status="pending_client")
        token = MagicLinkToken.objects.create(user=self.client_user, workspace=self.ws)
        self.client.post(reverse("client_portal:magic_link_entry", kwargs={"token": token.token}))

    def test_a_client_comments_without_deciding(self):
        response = self.client.post(
            reverse("client_portal:comment", kwargs={"post_id": self.post.id}),
            {"comment": "Can we use the blue logo?"},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 204)
        self.assertEqual(PlatformPost.objects.get(post=self.post).status, "pending_client")
        comment = PostComment.objects.get()
        self.assertEqual((comment.visibility, comment.author), ("external", self.client_user))
        action = ApprovalAction.objects.get()
        self.assertEqual((action.action, action.channel), ("commented", "portal"))
        self.assertTrue(Notification.objects.filter(user=self.author, event_type=EventType.POST_COMMENT).exists())

    def test_a_post_not_waiting_on_clients_is_not_commentable(self):
        draft = self.make_post("draft")
        response = self.client.post(
            reverse("client_portal:comment", kwargs={"post_id": draft.id}), {"comment": "hi"}, HTTP_HX_REQUEST="true"
        )
        self.assertEqual(response.status_code, 404)

    def test_the_portal_card_offers_a_comment_button(self):
        response = self.client.get(reverse("client_portal:approval_queue"))
        self.assertContains(response, "Send comment")

    def test_clients_never_see_team_notes_or_internal_comments(self):
        note_service.create_note(self.post, self.manager, SECRET)
        create_comment(self.post, self.manager, "SECRET-INTERNAL-COMMENT", "internal")
        create_comment(self.post, self.manager, "Visible to the client", "external")
        for name in ("dashboard", "approval_queue", "review", "published", "activity"):
            html = self.client.get(reverse(f"client_portal:{name}")).content.decode()
            self.assertNotIn(SECRET, html, name)
            self.assertNotIn("SECRET-INTERNAL-COMMENT", html, name)
        self.assertContains(self.client.get(reverse("client_portal:approval_queue")), "Visible to the client")


class NotesTests(CollabBase):
    def add(self, user, body, **extra):
        self.client.force_login(user)
        return self.client.post(self.url("add_note"), {"body": body}, **extra)

    def test_a_teammate_adds_a_note_and_the_thread_lists_it(self):
        response = self.add(self.editor, "Waiting on legal for the claim in line 2")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Waiting on legal")
        self.assertContains(response, "Edie Editor")
        self.add(self.manager, "Legal confirmed")
        body = self.client.get(self.url("notes_panel")).content.decode()
        self.assertLess(body.index("Waiting on legal"), body.index("Legal confirmed"))

    def test_a_note_is_never_a_comment_nor_activity(self):
        self.add(self.editor, "just for us")
        self.assertFalse(PostComment.objects.exists())
        self.assertFalse(ApprovalAction.objects.exists())
        self.assertEqual(PostNote.objects.get().body, "just for us")

    def test_a_mention_notifies_the_teammate_and_only_if_they_may_read_notes(self):
        self.add(self.editor, "@maxmanager can you check? @cleoclient @vicviewer too")
        got = Notification.objects.filter(event_type=EventType.COMMENT_MENTION)
        self.assertEqual([n.user for n in got], [self.manager])
        self.assertIn("note", got[0].body)

    def test_the_author_is_not_told_about_a_note(self):
        self.add(self.editor, "internal chatter")
        self.assertFalse(Notification.objects.filter(user=self.author).exists())

    def test_clients_and_viewers_cannot_read_or_write_notes(self):
        note_service.create_note(self.post, self.manager, SECRET)
        for user in (self.client_user, self.viewer):
            self.client.force_login(user)
            self.assertEqual(self.client.get(self.url("notes_panel")).status_code, 403, user.email)
            self.assertEqual(self.client.post(self.url("add_note"), {"body": "x"}).status_code, 403, user.email)
        self.assertEqual(PostNote.objects.count(), 1)

    def test_a_comment_only_role_has_no_notes(self):
        self.client.force_login(self.comment_only_user())
        self.assertEqual(self.client.get(self.url("notes_panel")).status_code, 403)

    def test_a_role_can_be_granted_notes(self):
        role = CustomRole.objects.create(
            organization=self.org, name="Notes", permissions={"use_internal_notes": True, "comment_on_posts": True}
        )
        user = self.member("notes@example.com", "viewer", custom_role=role)
        self.assertEqual(self.add(user, "granted").status_code, 200)

    def test_notes_are_scoped_to_the_workspace(self):
        other_ws = Workspace.objects.create(organization=self.org, name="Other")
        stranger = _user("stranger@example.com")
        WorkspaceMembership.objects.create(user=stranger, workspace=other_ws, workspace_role="manager")
        self.client.force_login(stranger)
        url = reverse("approvals:notes_panel", kwargs={"workspace_id": other_ws.id, "post_id": self.post.id})
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_empty_and_overlong_notes_are_refused(self):
        self.assertEqual(self.add(self.editor, "   ").status_code, 400)
        self.assertEqual(self.add(self.editor, "x" * 5001).status_code, 400)
        self.assertFalse(PostNote.objects.exists())

    def test_deleting_is_for_the_author_or_an_editor_of_others_posts(self):
        note = note_service.create_note(self.post, self.editor, "mine")
        url = self.url("delete_note", note_id=note.id)
        self.client.force_login(self.author)
        self.assertEqual(self.client.post(url).status_code, 403)
        self.client.force_login(self.manager)
        self.assertEqual(self.client.post(url).status_code, 200)
        note.refresh_from_db()
        self.assertTrue(note.is_deleted)
        self.assertNotIn("mine", self.client.get(self.url("notes_panel")).content.decode())

    def test_the_composer_loads_the_thread_for_the_team_only(self):
        compose = reverse("composer:compose_edit", kwargs={"workspace_id": self.ws.id, "post_id": self.post.id})
        self.client.force_login(self.editor)
        self.assertContains(self.client.get(compose), self.url("notes_panel"))
        role = CustomRole.objects.create(
            organization=self.org,
            name="Drafts only",
            permissions={"create_posts": True, "edit_others_posts": True, "use_internal_notes": False},
        )
        drafter = self.member("drafter@example.com", "contributor", custom_role=role)
        self.client.force_login(drafter)
        self.assertNotContains(self.client.get(compose), "/notes/")

    def test_a_note_is_not_part_of_the_published_content(self):
        note_service.create_note(self.post, self.editor, SECRET)
        from apps.composer.previews import post_previews

        self.assertNotIn(SECRET, repr(post_previews(self.post)))
        self.assertNotIn(SECRET, self.post.caption)
