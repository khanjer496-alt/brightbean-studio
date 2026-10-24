"""Held agent items from email and Slack, exact previews on the existing surfaces, and link hardening."""

import re
from datetime import timedelta
from unittest import mock

from django.core import mail, signing
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone

from apps.approvals import digests, links, slack, tasks
from apps.approvals.models import ApprovalLink, ApprovalReminder, PostComment
from apps.approvals.test_batch_slack import SlackTests
from apps.approvals.test_one_tap import OneTapBase, make_user
from apps.autonomy.models import ActionType, AgentAction
from apps.composer.models import PlatformPost, Post
from apps.members.models import WorkspaceMembership
from apps.notifications.models import Notification


def held_action(ws, post, account, **extra):
    pp = PlatformPost.objects.create(post=post, social_account=account, status="draft")
    return AgentAction.objects.create(
        workspace=ws,
        social_account=account,
        actor_type="agent",
        actor_name="Drafting bot",
        action_type=ActionType.PUBLISH_POSTS,
        operation="publish.schedule",
        decision="held",
        summary="Held for review because Draft only is on.",
        final_text=post.caption,
        post=post,
        platform_post=pp,
        requested_at=timezone.now() + timedelta(days=1),
        **extra,
    )


class LinkHardeningTests(OneTapBase):
    def setUp(self):
        super().setUp()
        # The link and picture views are rate limited per address through the cache.
        cache.clear()

    def test_token_is_bound_to_its_decision(self):
        post = self.make_post()
        self.links_for(post, self.reviewer)
        changes = ApprovalLink.objects.get(action="request_changes")
        forged = signing.dumps([str(changes.pk), "approve"], salt=links.SALT, compress=True)
        self.assertIsNone(links.resolve_link(forged))
        # The raw row id under another salt is refused too.
        self.assertIsNone(links.resolve_link(signing.dumps(str(changes.pk))))

    def test_confirm_page_headers_and_no_referrer(self):
        post = self.make_post()
        resp = self.client.get(self.path(self.links_for(post, self.reviewer)["approve"]))
        self.assertEqual(resp["Referrer-Policy"], "no-referrer")
        self.assertIn("no-store", resp["Cache-Control"])
        self.assertContains(resp, 'name="referrer" content="no-referrer"')
        self.assertContains(resp, "csrfmiddlewaretoken")

    def test_post_requires_csrf(self):
        from django.test import Client

        post = self.make_post()
        url = self.path(self.links_for(post, self.reviewer)["approve"])
        strict = Client(enforce_csrf_checks=True)
        self.assertEqual(strict.post(url).status_code, 403)
        self.assertEqual(self.status_of(post), {"pending_review"})

    def test_removed_member_links_are_retired(self):
        post = self.make_post()
        url = self.path(self.links_for(post, self.reviewer)["approve"])
        WorkspaceMembership.objects.filter(user=self.reviewer, workspace=self.ws).delete()
        self.assertFalse(ApprovalLink.objects.filter(used_at__isnull=True).exists())
        WorkspaceMembership.objects.create(user=self.reviewer, workspace=self.ws, workspace_role="manager")
        self.assertEqual(self.client.post(url).status_code, 410)
        self.assertEqual(self.status_of(post), {"pending_review"})

    def test_deleted_post_takes_its_links_with_it(self):
        post = self.make_post()
        url = self.path(self.links_for(post, self.reviewer)["approve"])
        post.delete()
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_link_page_shows_only_shared_comments(self):
        post = self.make_post()
        PostComment.objects.create(post=post, author=self.author, body="Internal: legal nervous", visibility="internal")
        PostComment.objects.create(post=post, author=self.author, body="Shared: new photo", visibility="external")
        page = self.client.get(self.path(self.links_for(post, self.reviewer)["approve"]))
        self.assertContains(page, "Shared: new photo")
        self.assertNotContains(page, "legal nervous")

    def test_email_pictures_are_signed_urls_not_attachments(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        from apps.composer.models import PostMedia
        from apps.media_library.models import MediaAsset

        post = self.make_post()
        gif = b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x00\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
        asset = MediaAsset.objects.create(
            workspace=self.ws,
            file=SimpleUploadedFile("pic.gif", gif, "image/gif"),
            media_type="image",
            filename="pic.gif",
        )
        PostMedia.objects.create(post=post, media_asset=asset, position=0)
        mail.outbox.clear()
        from apps.approvals import services

        services._notify_reviewers(
            self.ws, self.author, post=post, event_type="post_submitted", title="Review", body="Please"
        )
        message = next(m for m in mail.outbox if m.to == [self.reviewer.email])
        self.assertEqual(message.attachments, [])
        html = message.alternatives[0][0]
        src = re.search(r'<img src="([^"]+)"', html).group(1)
        self.assertIn("/approve-link/media/", src)
        redirect = self.client.get(self.path(src))
        self.assertEqual(redirect.status_code, 302, redirect.content[:200])
        self.assertEqual(self.client.get(self.path(src) + "x").status_code, 404)
        self.assertIn("color-scheme", html)
        self.assertNotIn("&#x27;", message.body)


class DeadlineIdempotencyTests(OneTapBase):
    def test_escalation_is_sent_once(self):
        post = self.make_post()
        ApprovalReminder.objects.create(post=post, stage="pending_review", due_at=timezone.now() - timedelta(hours=1))
        tasks._process_deadlines(timezone.now())
        tasks._process_deadlines(timezone.now())
        self.assertEqual(Notification.objects.filter(event_type="approval_stalled").count(), 1)


class HeldFromEmailTests(OneTapBase):
    # Approving held agent work schedules it, which a workspace with an approval workflow refuses.
    mode = "none"

    def setUp(self):
        super().setUp()
        self.post = Post.objects.create(workspace=self.ws, author=self.author, caption="Agent wrote this")
        self.action = held_action(self.ws, self.post, self.account)

    def digest_links(self, user):
        mail.outbox.clear()
        digests.send_review_digest(self.ws, user)
        body = mail.outbox[-1].body
        return dict(re.findall(r"(Approve|Dismiss): (\S+)", body))

    def test_digest_offers_held_items_and_approve_goes_through_autonomy(self):
        found = self.digest_links(self.reviewer)
        self.assertEqual(set(found), {"Approve", "Dismiss"})
        url = self.path(found["Approve"])
        self.assertEqual(self.client.get(url).status_code, 200)
        self.action.refresh_from_db()
        self.assertEqual(self.action.review_outcome, "")  # GET changes nothing
        resp = self.client.post(url)
        self.assertEqual(resp.status_code, 200, re.findall(r'role="alert">([^<]+)', resp.content.decode()))
        self.action.refresh_from_db()
        self.assertEqual((self.action.review_outcome, self.action.review_channel), ("approved", "email"))
        self.assertEqual(self.action.reviewed_by, self.reviewer)
        self.assertEqual(self.status_of(self.post), {"scheduled"})
        trail = AgentAction.objects.filter(operation="autonomy.approve_held").get()
        self.assertIn("via an email link", trail.summary)
        # Single use: the sibling Dismiss link is retired.
        self.assertEqual(self.client.post(self.path(found["Dismiss"])).status_code, 410)

    def test_dismiss_from_email(self):
        found = self.digest_links(self.reviewer)
        self.client.post(self.path(found["Dismiss"]))
        self.action.refresh_from_db()
        self.assertEqual((self.action.review_outcome, self.action.review_channel), ("dismissed", "email"))
        self.assertEqual(self.status_of(self.post), {"draft"})

    def test_permissions_follow_the_activity_page(self):
        reviewer_only = make_user("rev2@example.com")
        role = WorkspaceMembership.objects.create(user=reviewer_only, workspace=self.ws, workspace_role="editor")
        from apps.members.models import CustomRole

        custom = CustomRole.objects.create(
            organization=self.org, name="Approver", permissions={"approve_posts": True, "publish_directly": False}
        )
        role.custom_role = custom
        role.save()
        self.assertEqual(set(self.digest_links(reviewer_only)), {"Dismiss"})
        # A client never sees agent work.
        mail.outbox.clear()
        digests.send_review_digest(self.ws, self.client_user)
        self.assertEqual(mail.outbox, [])

    def test_link_stops_when_permission_is_lost(self):
        url = self.path(self.digest_links(self.reviewer)["Approve"])
        WorkspaceMembership.objects.filter(user=self.reviewer).update(workspace_role="viewer")
        self.client.post(url)
        self.action.refresh_from_db()
        self.assertEqual(self.action.review_outcome, "")

    def test_held_notification_email_has_buttons(self):
        from apps.autonomy import service

        mail.outbox.clear()
        service.announce(self.action)
        bodies = [m.body for m in mail.outbox if m.to == [self.reviewer.email]]
        self.assertTrue(bodies)
        self.assertIn("Approve: ", bodies[0])
        self.assertIn("Dismiss: ", bodies[0])


class HeldFromSlackTests(SlackTests):
    def setUp(self):
        super().setUp()
        self.ws.approval_workflow_mode = "none"
        self.ws.save(update_fields=["approval_workflow_mode"])
        self.held_post = Post.objects.create(workspace=self.ws, author=self.author, caption="Agent wrote this")
        self.action = held_action(self.ws, self.held_post, self.account)

    def held_button(self, action_id="held_approve", value=None):
        return {
            "type": "block_actions",
            "team": {"id": "T1"},
            "user": {"id": "U1"},
            "channel": {"id": "C1"},
            "message": {"ts": "1.1"},
            "response_url": "https://hooks.slack.com/actions/x",
            "actions": [{"action_id": action_id, "value": value or str(self.action.id)}],
        }

    def test_approve_held_from_slack(self):
        with (
            mock.patch("apps.approvals.slack._get", return_value=self.slack_user()),
            mock.patch("apps.approvals.slack.api_call") as call,
        ):
            self.assertEqual(self.post(self.held_button()).status_code, 200)
        self.action.refresh_from_db()
        self.assertEqual((self.action.review_outcome, self.action.review_channel), ("approved", "slack"))
        self.assertEqual(call.call_args.args[0], "chat.update")

    def test_dismiss_needs_a_mapped_member(self):
        with (
            mock.patch("apps.approvals.slack._get", return_value=self.slack_user("stranger@example.com")),
            mock.patch("apps.approvals.slack._respond") as respond,
        ):
            self.post(self.held_button("held_dismiss"))
        self.action.refresh_from_db()
        self.assertEqual(self.action.review_outcome, "")
        self.assertTrue(respond.called)

    def test_held_item_of_another_workspace_is_out_of_reach(self):
        from apps.workspaces.models import Workspace

        other = Workspace.objects.create(organization=self.org, name="Other")
        other_post = Post.objects.create(workspace=other, author=self.author, caption="x")
        from apps.social_accounts.models import SocialAccount

        acct = SocialAccount.objects.create(
            workspace=other,
            platform="bluesky",
            account_platform_id="o",
            account_name="O",
            connection_status="connected",
        )
        foreign = held_action(other, other_post, acct)
        WorkspaceMembership.objects.create(user=self.reviewer, workspace=other, workspace_role="manager")
        with (
            mock.patch("apps.approvals.slack._get", return_value=self.slack_user()),
            mock.patch("apps.approvals.slack._respond"),
            mock.patch("apps.approvals.slack.api_call"),
        ):
            self.post(self.held_button(value=str(foreign.id)))
        foreign.refresh_from_db()
        self.assertEqual(foreign.review_outcome, "")

    def test_member_from_another_slack_team_does_not_map(self):
        user = self.slack_user()
        user["user"]["team_id"] = "T-OTHER"
        with mock.patch("apps.approvals.slack._get", return_value=user):
            self.assertIsNone(slack.member_for_slack_user(self.conn, "U1"))

    def test_held_message_is_queued_when_an_agent_is_held(self):
        from apps.autonomy import service

        with mock.patch("apps.approvals.slack.post_held_request_task") as task:
            service.announce(self.action)
        task.assert_called_once_with(str(self.action.id))


class ExactPreviewSurfaceTests(OneTapBase):
    def test_approvals_tab_row_loads_the_shared_preview_card(self):
        post = self.make_post(caption="Exact words for LinkedIn")
        self.client.force_login(self.reviewer)
        url = reverse("approvals:post_previews", kwargs={"workspace_id": self.ws.id, "post_id": post.id})
        resp = self.client.get(url)
        self.assertContains(resp, "data-approver-previews")
        self.assertContains(resp, "data-platform-preview")
        self.assertContains(resp, "Exact words for LinkedIn")

    def test_portal_card_loads_the_shared_preview_card(self):
        post = self.make_post(status="pending_client", caption="Client sees exactly this")
        self.client.force_login(self.client_user)
        session = self.client.session
        session["is_portal_session"] = True
        session["portal_workspace_id"] = str(self.ws.id)
        session.save()
        queue = self.client.get(reverse("client_portal:approval_queue"))
        self.assertContains(queue, reverse("client_portal:post_previews", kwargs={"post_id": post.id}))
        resp = self.client.get(reverse("client_portal:post_previews", kwargs={"post_id": post.id}))
        self.assertContains(resp, "data-platform-preview")
        self.assertContains(resp, "Client sees exactly this")
