"""One-tap approval links: lifecycle, safety on GET, content fingerprint, audit, previews in email."""

import re
from datetime import timedelta

from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.approvals import services
from apps.approvals.links import issue_links, link_token, resolve_link
from apps.approvals.models import ApprovalAction, ApprovalLink, PostComment
from apps.composer.models import PlatformPost, Post
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace


def make_user(email, name=""):
    user = User.objects.create_user(email=email, password="testpass123", name=name, tos_accepted_at=timezone.now())
    auto_org_ids = list(OrgMembership.objects.filter(user=user).values_list("organization_id", flat=True))
    WorkspaceMembership.objects.filter(user=user).delete()
    OrgMembership.objects.filter(user=user).delete()
    Organization.objects.filter(id__in=auto_org_ids).delete()
    return user


class OneTapBase(TestCase):
    mode = "required_internal"

    def setUp(self):
        self.org = Organization.objects.create(name="Org")
        self.ws = Workspace.objects.create(organization=self.org, name="WS", approval_workflow_mode=self.mode)
        self.reviewer = make_user("reviewer@example.com", "Rita Reviewer")
        self.author = make_user("author@example.com", "Aaron Author")
        self.client_user = make_user("client@example.com", "Cleo Client")
        OrgMembership.objects.create(user=self.reviewer, organization=self.org, org_role="owner")
        WorkspaceMembership.objects.create(user=self.reviewer, workspace=self.ws, workspace_role="manager")
        WorkspaceMembership.objects.create(user=self.author, workspace=self.ws, workspace_role="contributor")
        WorkspaceMembership.objects.create(user=self.client_user, workspace=self.ws, workspace_role="client")
        self.account = SocialAccount.objects.create(
            workspace=self.ws,
            platform="linkedin_personal",
            account_platform_id="li-1",
            account_name="Acme LinkedIn",
            connection_status="connected",
        )
        self.bluesky = SocialAccount.objects.create(
            workspace=self.ws,
            platform="bluesky",
            account_platform_id="bs-1",
            account_name="Acme Bluesky",
            connection_status="connected",
        )

    def make_post(self, status="pending_review", caption="Launch day is here", accounts=None):
        post = Post.objects.create(workspace=self.ws, author=self.author, caption=caption)
        for account in accounts or [self.account]:
            PlatformPost.objects.create(post=post, social_account=account, status=status)
        return post

    def links_for(self, post, user):
        from apps.approvals import decisions

        return issue_links(post, user, self.ws, decisions.current_fingerprint(post, user, self.ws))

    def path(self, url):
        return re.sub(r"^https?://[^/]+", "", url)

    def status_of(self, post):
        return set(post.platform_posts.values_list("status", flat=True))


class LinkLifecycleTests(OneTapBase):
    def test_get_never_changes_state(self):
        post = self.make_post()
        links = self.links_for(post, self.reviewer)
        for url in links.values():
            resp = self.client.get(self.path(url))
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(self.client.head(self.path(url)).status_code, 200)
        self.assertEqual(self.status_of(post), {"pending_review"})
        self.assertFalse(ApprovalAction.objects.filter(post=post).exists())
        self.assertFalse(ApprovalLink.objects.filter(used_at__isnull=False).exists())

    def test_post_approves_and_audits_channel_actor_fingerprint(self):
        post = self.make_post()
        url = self.path(self.links_for(post, self.reviewer)["approve"])
        resp = self.client.post(url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.status_of(post), {"approved"})
        action = ApprovalAction.objects.get(post=post, action="approved")
        self.assertEqual(action.channel, "email")
        self.assertEqual(action.user, self.reviewer)
        self.assertEqual(len(action.fingerprint), 64)

    def test_single_use_and_sibling_button_retired(self):
        post = self.make_post()
        links = self.links_for(post, self.reviewer)
        self.assertEqual(self.client.post(self.path(links["approve"])).status_code, 200)
        again = self.client.post(self.path(links["approve"]))
        self.assertEqual(again.status_code, 410)
        sibling = self.client.post(self.path(links["request_changes"]), {"comment": "too late"})
        self.assertEqual(sibling.status_code, 410)
        self.assertEqual(ApprovalAction.objects.filter(post=post).count(), 1)

    def test_expired_link_does_nothing(self):
        post = self.make_post()
        links = self.links_for(post, self.reviewer)
        ApprovalLink.objects.update(expires_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(self.client.get(self.path(links["approve"])).status_code, 410)
        self.assertEqual(self.client.post(self.path(links["approve"])).status_code, 410)
        self.assertEqual(self.status_of(post), {"pending_review"})

    @override_settings(APPROVAL_LINK_TTL_HOURS=72)
    def test_default_expiry_is_72_hours(self):
        post = self.make_post()
        self.links_for(post, self.reviewer)
        link = ApprovalLink.objects.first()
        delta = link.expires_at - timezone.now()
        self.assertTrue(timedelta(hours=71, minutes=59) < delta <= timedelta(hours=72))

    def test_wrong_recipient_cannot_use_link(self):
        post = self.make_post()
        url = self.path(self.links_for(post, self.reviewer)["approve"])
        self.client.force_login(self.author)
        self.assertEqual(self.client.post(url).status_code, 200)  # renders the "sent to someone else" page
        self.assertEqual(self.status_of(post), {"pending_review"})
        self.assertFalse(ApprovalAction.objects.filter(post=post).exists())

    def test_recipient_who_changed_email_is_refused(self):
        post = self.make_post()
        url = self.path(self.links_for(post, self.reviewer)["approve"])
        User.objects.filter(pk=self.reviewer.pk).update(email="new@example.com")
        self.assertEqual(self.client.post(url).status_code, 410)
        self.assertEqual(self.status_of(post), {"pending_review"})

    def test_tampered_token_is_rejected(self):
        post = self.make_post()
        links = self.links_for(post, self.reviewer)
        token = links["approve"].rstrip("/").rsplit("/", 1)[1]
        self.assertEqual(
            self.client.get(reverse("approval_links:confirm", kwargs={"token": token + "x"})).status_code, 404
        )
        self.assertIsNone(resolve_link("garbage"))
        link = ApprovalLink.objects.get(action="approve")
        self.assertEqual(resolve_link(link_token(link)), link)

    def test_changed_content_blocks_the_link_and_offers_a_new_one(self):
        post = self.make_post()
        links = self.links_for(post, self.reviewer)
        Post.objects.filter(pk=post.pk).update(caption="A completely different caption")
        page = self.client.get(self.path(links["approve"]))
        self.assertContains(page, "This post changed")
        resp = self.client.post(self.path(links["approve"]))
        self.assertContains(resp, "This post changed")
        self.assertEqual(self.status_of(post), {"pending_review"})
        self.assertFalse(ApprovalAction.objects.filter(post=post).exists())
        # The recipient can ask for a fresh pair for the new version.
        mail.outbox.clear()
        resend = self.client.post(self.path(links["approve"]).rstrip("/") + "/resend/")
        self.assertContains(resend, "New email on its way")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("A completely different caption", mail.outbox[0].body)
        fresh = re.search(r"Approve: (\S+)", mail.outbox[0].body).group(1)
        self.assertEqual(self.client.post(self.path(fresh)).status_code, 200)
        self.assertEqual(self.status_of(post), {"approved"})

    def test_request_changes_needs_a_comment_and_keeps_link_usable(self):
        post = self.make_post()
        url = self.path(self.links_for(post, self.reviewer)["request_changes"])
        resp = self.client.post(url, {"comment": "  "})
        self.assertEqual(resp.status_code, 422)
        self.assertContains(resp, "comment is required", status_code=422)
        self.assertEqual(self.status_of(post), {"pending_review"})
        ok = self.client.post(url, {"comment": "Shorter hook please"})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(self.status_of(post), {"changes_requested"})
        action = ApprovalAction.objects.get(post=post, action="changes_requested")
        self.assertEqual((action.channel, action.comment), ("email", "Shorter hook please"))

    def test_already_decided_post_shows_nothing_waiting(self):
        post = self.make_post()
        url = self.path(self.links_for(post, self.reviewer)["approve"])
        services.approve_post(post, self.reviewer, self.ws)
        resp = self.client.post(url)
        self.assertContains(resp, "Nothing is waiting for you")
        self.assertEqual(ApprovalAction.objects.filter(post=post, action="approved").count(), 1)

    def test_losing_approve_permission_blocks_the_link(self):
        post = self.make_post()
        url = self.path(self.links_for(post, self.reviewer)["approve"])
        WorkspaceMembership.objects.filter(user=self.reviewer, workspace=self.ws).update(workspace_role="viewer")
        self.client.post(url)
        self.assertEqual(self.status_of(post), {"pending_review"})


class ClientLinkTests(OneTapBase):
    mode = "required_internal_and_client"

    def test_client_without_seat_decides_from_the_email_link(self):
        post = self.make_post(status="pending_client")
        url = self.path(self.links_for(post, self.client_user)["approve"])
        resp = self.client.post(url)  # not signed in, no portal session
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.status_of(post), {"approved"})
        action = ApprovalAction.objects.get(post=post, action="approved")
        self.assertEqual((action.user, action.channel), (self.client_user, "email"))
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_internal_reviewer_link_is_useless_for_the_client_stage(self):
        post = self.make_post(status="pending_client")
        from apps.approvals import decisions

        self.assertEqual(decisions.current_fingerprint(post, self.reviewer, self.ws), "")

    def test_client_link_page_hides_internal_comments(self):
        post = self.make_post(status="pending_client")
        PostComment.objects.create(post=post, author=self.reviewer, body="Secret margin note", visibility="internal")
        PostComment.objects.create(
            post=post, author=self.reviewer, body="Please confirm the date", visibility="external"
        )
        url = self.path(self.links_for(post, self.client_user)["approve"])
        page = self.client.get(url)
        self.assertContains(page, "Please confirm the date")
        self.assertNotContains(page, "Secret margin note")


class ConfirmPageTests(OneTapBase):
    def test_page_shows_each_networks_exact_content(self):
        post = self.make_post(accounts=[self.account, self.bluesky])
        PlatformPost.objects.filter(post=post, social_account=self.bluesky).update(
            platform_specific_caption="Short Bluesky version"
        )
        url = self.path(self.links_for(post, self.reviewer)["approve"])
        page = self.client.get(url)
        self.assertContains(page, "Launch day is here")
        self.assertContains(page, "Short Bluesky version")
        self.assertContains(page, "data-approver-previews")
        self.assertContains(page, "Nothing is recorded until you press the button")


class EmailTests(OneTapBase):
    def test_submission_email_has_one_tap_buttons_and_exact_previews(self):
        post = self.make_post(status="draft", accounts=[self.account, self.bluesky])
        PlatformPost.objects.filter(post=post, social_account=self.bluesky).update(
            platform_specific_caption="Bluesky only wording"
        )
        mail.outbox.clear()
        services.submit_for_review(post, self.author, self.ws)
        sent = [m for m in mail.outbox if m.to == ["reviewer@example.com"]]
        self.assertEqual(len(sent), 1)
        html = sent[0].alternatives[0][0]
        self.assertIn("/approve-link/", html)
        self.assertIn("Approve", html)
        self.assertIn("Request changes", html)
        self.assertIn("Launch day is here", html)
        self.assertIn("Bluesky only wording", html)
        self.assertEqual(len(re.findall(r"/approve-link/", sent[0].body)), 2)
        self.assertEqual(ApprovalLink.objects.filter(recipient=self.reviewer).count(), 2)

    def test_links_are_bound_to_each_recipient(self):
        second = make_user("second@example.com")
        WorkspaceMembership.objects.create(user=second, workspace=self.ws, workspace_role="manager")
        post = self.make_post(status="draft")
        services.submit_for_review(post, self.author, self.ws)
        self.assertEqual(
            set(ApprovalLink.objects.values_list("recipient__email", flat=True)),
            {"reviewer@example.com", "second@example.com"},
        )

    def test_other_notifications_keep_the_generic_template(self):
        from apps.notifications.engine import notify
        from apps.notifications.models import EventType

        mail.outbox.clear()
        notify(
            user=self.reviewer,
            event_type=EventType.SOCIAL_ACCOUNT_DISCONNECTED,
            title="Disconnected",
            body="x",
            data={},
        )
        html = mail.outbox[0].alternatives[0][0]
        self.assertNotIn("/approve-link/", html)
