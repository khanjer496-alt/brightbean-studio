"""Previews, batch approval, deadlines and Slack approvals."""

import json
import time
from datetime import timedelta
from unittest import mock
from urllib.parse import urlencode

from allauth.account.models import EmailAddress
from django.core import mail
from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.approvals import digests, services, slack, tasks
from apps.approvals.models import ApprovalAction, ApprovalReminder, SlackApprovalConnection
from apps.approvals.test_one_tap import OneTapBase
from apps.composer.models import PlatformPost, PlatformPostPart, Post, PostPart
from apps.composer.previews import platform_post_preview, settings_summary
from apps.members.models import WorkspaceMembership


class PreviewTests(OneTapBase):
    def test_override_thread_first_comment_and_settings(self):
        post = self.make_post(accounts=[self.account, self.bluesky])
        Post.objects.filter(pk=post.pk).update(first_comment="Link in comments")
        part = PostPart.objects.create(post=post, position=1, caption="Second part")
        bsky = post.platform_posts.get(social_account=self.bluesky)
        PlatformPostPart.objects.create(platform_post=bsky, part=part, platform_specific_caption="Bluesky part 2")
        PlatformPost.objects.filter(pk=bsky.pk).update(
            platform_specific_caption="Bluesky text", platform_extra={"post_type": "text"}
        )
        bsky.refresh_from_db()
        preview = platform_post_preview(bsky)
        self.assertEqual(preview["caption"], "Bluesky text")
        self.assertEqual(preview["thread"]["parts"][0]["caption"], "Bluesky part 2")
        self.assertEqual(preview["settings"], [{"label": "Format", "value": "Text"}])
        other = platform_post_preview(post.platform_posts.get(social_account=self.account))
        self.assertEqual(other["caption"], "Launch day is here")
        self.assertEqual(other["thread"]["parts"][0]["caption"], "Second part")

    def test_settings_summary_uses_declared_settings(self):
        reddit = type(self.account).objects.create(
            workspace=self.ws,
            platform="reddit",
            account_platform_id="r1",
            account_name="u/x",
            connection_status="connected",
        )
        rows = settings_summary(reddit, {"subreddit": "python", "nsfw": True})
        labels = {r["label"]: r["value"] for r in rows}
        self.assertIn("python", labels.values())
        self.assertIn("Yes", labels.values())

    def test_shared_card_renders_per_network_mockup(self):
        from django.template.loader import render_to_string

        post = self.make_post(accounts=[self.account, self.bluesky])
        from apps.composer.previews import post_previews

        html = render_to_string("composer/partials/approver_previews.html", post_previews(post))
        self.assertIn("LinkedIn (Personal Profile) Preview", html)
        self.assertIn("Bluesky Preview", html)
        self.assertNotIn("style=", html)


class PortalBatchTests(OneTapBase):
    mode = "required_internal_and_client"

    def login_portal(self):
        self.client.force_login(self.client_user)
        session = self.client.session
        session["is_portal_session"] = True
        session["portal_workspace_id"] = str(self.ws.id)
        session.save()

    def test_review_page_and_select_all_approve(self):
        first = self.make_post(status="pending_client", caption="First post")
        second = self.make_post(status="pending_client", caption="Second post")
        ApprovalReminder.objects.create(post=first, stage="pending_client", due_at=timezone.now() + timedelta(hours=5))
        self.login_portal()
        page = self.client.get(reverse("client_portal:review"))
        self.assertContains(page, "Review all (2)")
        self.assertContains(page, "Needs approval by")
        self.assertContains(page, "Select all")
        data = {"post_ids": [str(first.id), str(second.id)]}
        for post in (first, second):
            from apps.approvals import decisions

            data[f"fp_{post.id}"] = decisions.current_fingerprint(post, self.client_user, self.ws)
            data[f"comment_{post.id}"] = f"ok {post.caption}"
        resp = self.client.post(reverse("client_portal:review_approve"), data)
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(self.status_of(first) | self.status_of(second), {"approved"})
        actions = ApprovalAction.objects.filter(action="approved")
        self.assertEqual({a.channel for a in actions}, {"portal"})
        self.assertEqual({a.comment for a in actions}, {"ok First post", "ok Second post"})

    def test_changed_post_is_not_approved_in_batch(self):
        post = self.make_post(status="pending_client", caption="Before")
        self.login_portal()
        from apps.approvals import decisions

        fp = decisions.current_fingerprint(post, self.client_user, self.ws)
        Post.objects.filter(pk=post.pk).update(caption="After")
        resp = self.client.post(
            reverse("client_portal:review_approve"), {"post_ids": [str(post.id)], f"fp_{post.id}": fp}
        )
        self.assertEqual(json.loads(resp.headers["HX-Trigger"])["showToast"]["tone"], "warn")
        self.assertEqual(self.status_of(post), {"pending_client"})

    def test_internal_comments_are_hidden_from_the_client(self):
        from apps.approvals.models import PostComment

        post = self.make_post(status="pending_client")
        PostComment.objects.create(post=post, author=self.reviewer, body="Internal only secret", visibility="internal")
        PostComment.objects.create(post=post, author=self.reviewer, body="Visible note", visibility="external")
        self.login_portal()
        page = self.client.get(reverse("client_portal:review"))
        self.assertContains(page, "Visible note")
        self.assertNotContains(page, "Internal only secret")

    def test_portal_decisions_record_portal_channel(self):
        post = self.make_post(status="pending_client")
        self.login_portal()
        self.client.post(reverse("client_portal:approve", kwargs={"post_id": post.id}), HTTP_HX_REQUEST="true")
        self.assertEqual(ApprovalAction.objects.get(post=post, action="approved").channel, "portal")

    def test_digest_email_has_review_all_button_for_client(self):
        self.make_post(status="pending_client")
        self.make_post(status="pending_client")
        mail.outbox.clear()
        digests.send_review_digest(self.ws, self.client_user)
        html = mail.outbox[0].alternatives[0][0]
        self.assertIn("Review all (2)", html)
        self.assertIn("/portal/", html)
        self.assertIn("next=review", html)

    def test_magic_link_lands_on_review_page(self):
        from apps.client_portal.services import issue_portal_link

        url = issue_portal_link(self.client_user, self.ws, landing="review")
        path = url.split("//", 1)[1].split("/", 1)[1]
        path = "/" + path
        self.assertContains(self.client.get(path), 'name="next" value="review"')
        resp = self.client.post(path.split("?")[0], {"next": "review"})
        self.assertRedirects(resp, reverse("client_portal:review"), fetch_redirect_response=False)


class TeamDigestAndDeadlineTests(OneTapBase):
    def test_team_digest_links_to_approvals_tab(self):
        self.make_post()
        mail.outbox.clear()
        digests.send_review_digest(self.ws, self.reviewer)
        self.assertIn("tab=approvals", mail.outbox[0].alternatives[0][0])

    def test_workspace_sla_sets_deadline_on_submit(self):
        workspace_model = type(self.ws)
        workspace_model.objects.filter(pk=self.ws.pk).update(approval_sla_hours=10)
        self.ws.refresh_from_db()
        post = self.make_post(status="draft")
        services.submit_for_review(post, self.author, self.ws)
        due = ApprovalReminder.objects.get(post=post, stage="pending_review").due_at
        self.assertAlmostEqual((due - timezone.now()).total_seconds(), 10 * 3600, delta=60)

    def test_reminder_before_then_escalation_after_deadline(self):
        post = self.make_post()
        reminder = ApprovalReminder.objects.create(
            post=post, stage="pending_review", due_at=timezone.now() + timedelta(hours=2)
        )
        mail.outbox.clear()
        tasks.check_approval_reminders()
        digest = [m for m in mail.outbox if "your approval" in m.subject]
        self.assertEqual(len(digest), 1)
        reminder.refresh_from_db()
        self.assertIsNotNone(reminder.deadline_reminded_at)
        self.assertFalse(reminder.escalated)
        # Not reminded twice.
        mail.outbox.clear()
        tasks.check_approval_reminders()
        self.assertEqual([m for m in mail.outbox if "your approval" in m.subject], [])
        # Past the deadline: managers are told, once.
        ApprovalReminder.objects.filter(pk=reminder.pk).update(due_at=timezone.now() - timedelta(minutes=1))
        tasks.check_approval_reminders()
        reminder.refresh_from_db()
        self.assertTrue(reminder.escalated)
        self.assertTrue(self.reviewer.notifications.filter(title="Approval deadline missed").exists())
        self.reviewer.notifications.all().delete()
        tasks.check_approval_reminders()
        self.assertFalse(self.reviewer.notifications.filter(title="Approval deadline missed").exists())

    def test_decided_posts_are_not_reminded(self):
        post = self.make_post(status="approved")
        ApprovalReminder.objects.create(post=post, stage="pending_review", due_at=timezone.now() - timedelta(hours=1))
        tasks.check_approval_reminders()
        self.assertFalse(ApprovalReminder.objects.get(post=post).escalated)


SECRET = "s3cr3t-signing"


@override_settings(
    PLATFORM_CREDENTIALS_FROM_ENV={"slack": {"client_id": "cid", "client_secret": "csec", "signing_secret": SECRET}}
)
class SlackTests(OneTapBase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.conn = SlackApprovalConnection.objects.create(
            workspace=self.ws,
            team_id="T1",
            team_name="Acme",
            bot_token="xoxb-1",
            channel_id="C1",
            channel_name="#approvals",
        )
        for user in (self.reviewer,):
            EmailAddress.objects.create(user=user, email=user.email, verified=True, primary=True)

    def signed(self, payload, *, ts=None, secret=SECRET):
        body = urlencode({"payload": json.dumps(payload)}).encode()
        ts = str(int(ts if ts is not None else time.time()))
        return body, {
            "HTTP_X_SLACK_REQUEST_TIMESTAMP": ts,
            "HTTP_X_SLACK_SIGNATURE": slack.compute_signature(secret, ts, body),
        }

    def post(self, payload, drain=True, **kw):
        """Send a signed interaction; ``drain`` then runs what it queued, as the worker would."""
        body, headers = self.signed(payload, **kw)
        response = self.client.post(
            reverse("approval_slack:interactions"),
            data=body,
            content_type="application/x-www-form-urlencoded",
            **headers,
        )
        if drain:
            self.drain()
        return response

    def drain(self):
        from background_task.models import Task
        from background_task.tasks import tasks as registry

        for task in Task.objects.filter(task_name__startswith="apps.approvals.slack.").order_by("id"):
            args, kwargs = task.params()
            registry.run_task(task.task_name, args, kwargs)
            task.delete()

    def button(self, post, action_id=slack.ACTION_APPROVE, user="U1"):
        from apps.approvals import decisions

        fp = decisions.current_fingerprint(post, self.reviewer, self.ws)
        return {
            "type": "block_actions",
            "team": {"id": "T1"},
            "user": {"id": user},
            "channel": {"id": "C1"},
            "message": {"ts": "1.1"},
            "trigger_id": "trig",
            "response_url": "https://hooks.slack.com/actions/x",
            "actions": [{"action_id": action_id, "value": f"{post.id}|{fp}"}],
        }

    def slack_user(self, email="reviewer@example.com", confirmed=True):
        return {"ok": True, "user": {"is_email_confirmed": confirmed, "profile": {"email": email}}}

    def test_signature_verification(self):
        body = b"payload=x"
        ts = str(int(time.time()))
        sig = slack.compute_signature(SECRET, ts, body)
        self.assertTrue(slack.verify_signature(SECRET, ts, sig, body))
        self.assertFalse(slack.verify_signature("other", ts, sig, body))
        self.assertFalse(slack.verify_signature(SECRET, ts, sig, body + b"x"))
        self.assertFalse(slack.verify_signature(SECRET, ts, "", body))
        self.assertFalse(slack.verify_signature(SECRET, "abc", sig, body))

    def test_replay_window_is_five_minutes(self):
        body = b"x"
        old = str(int(time.time()) - 301)
        self.assertFalse(slack.verify_signature(SECRET, old, slack.compute_signature(SECRET, old, body), body))
        edge = str(int(time.time()) - 299)
        self.assertTrue(slack.verify_signature(SECRET, edge, slack.compute_signature(SECRET, edge, body), body))

    def test_unsigned_stale_and_wrong_secret_requests_are_refused(self):
        post = self.make_post()
        payload = self.button(post)
        with mock.patch("apps.approvals.slack._get", return_value=self.slack_user()):
            self.assertEqual(self.post(payload, secret="wrong").status_code, 401)
            self.assertEqual(self.post(payload, ts=time.time() - 600).status_code, 401)
            bare = self.client.post(
                reverse("approval_slack:interactions"),
                data=urlencode({"payload": json.dumps(payload)}),
                content_type="application/x-www-form-urlencoded",
            )
            self.assertEqual(bare.status_code, 401)
        self.assertEqual(self.status_of(post), {"pending_review"})

    def test_identical_signed_request_is_acted_on_once(self):
        from background_task.models import Task

        post = self.make_post()
        body, headers = self.signed(self.button(post))
        url = reverse("approval_slack:interactions")
        with mock.patch("apps.approvals.slack._get") as get, mock.patch("apps.approvals.slack.api_call") as call:
            first = self.client.post(url, data=body, content_type="application/x-www-form-urlencoded", **headers)
            # Slack resends what it thinks went unanswered, with a fresh signature timestamp.
            resent = self.client.post(
                url,
                data=body,
                content_type="application/x-www-form-urlencoded",
                HTTP_X_SLACK_RETRY_NUM="1",
                HTTP_X_SLACK_RETRY_REASON="http_timeout",
                **headers,
            )
        self.assertEqual((first.status_code, resent.status_code), (200, 200))
        self.assertEqual(Task.objects.filter(task_name="apps.approvals.slack.process_interaction_task").count(), 1)
        get.assert_not_called()
        call.assert_not_called()

    def test_button_is_acknowledged_without_calling_slack(self):
        from background_task.models import Task

        for action_id in (slack.ACTION_APPROVE,):
            post = self.make_post()
            with (
                mock.patch("apps.approvals.slack._get") as get,
                mock.patch("apps.approvals.slack.api_call") as call,
                mock.patch("apps.approvals.slack._respond") as respond,
            ):
                resp = self.post(self.button(post, action_id), drain=False)
            self.assertEqual((resp.status_code, resp.content), (200, b""))
            for mocked in (get, call, respond):
                mocked.assert_not_called()
            self.assertEqual(self.status_of(post), {"pending_review"})
            self.assertTrue(Task.objects.filter(task_name="apps.approvals.slack.process_interaction_task").exists())

    def test_deferred_approval_goes_through_the_decision_service(self):
        post = self.make_post()
        with (
            mock.patch("apps.approvals.slack._get", return_value=self.slack_user()),
            mock.patch("apps.approvals.slack.api_call"),
            mock.patch("apps.approvals.slack.decisions.decide") as decide,
        ):
            self.post(self.button(post))
        self.assertEqual(decide.call_args.args[:3], (post, self.reviewer, self.ws))
        self.assertEqual(decide.call_args.kwargs["channel"], "slack")

    def test_held_buttons_are_deferred_and_release_through_the_autonomy_service(self):
        from background_task.models import Task

        payload = {
            "type": "block_actions",
            "team": {"id": "T1"},
            "user": {"id": "U1"},
            "channel": {"id": "C1"},
            "message": {"ts": "1.1"},
            "trigger_id": "trig-held",
            "response_url": "https://hooks.slack.com/actions/x",
            "actions": [{"action_id": slack.ACTION_HELD_APPROVE, "value": "00000000-0000-0000-0000-000000000001"}],
        }
        action = mock.Mock(id="00000000-0000-0000-0000-000000000001")
        with (
            mock.patch("apps.approvals.slack._held_for", return_value=action),
            mock.patch("apps.approvals.slack._get") as get,
            mock.patch("apps.approvals.slack.api_call") as call,
        ):
            resp = self.post(payload, drain=False)
        self.assertEqual(resp.status_code, 200)
        get.assert_not_called()
        call.assert_not_called()
        self.assertEqual(Task.objects.filter(task_name="apps.approvals.slack.process_interaction_task").count(), 1)
        with (
            mock.patch("apps.approvals.slack._held_for", return_value=action),
            mock.patch("apps.approvals.slack._get", return_value=self.slack_user()),
            mock.patch("apps.approvals.slack.api_call"),
            mock.patch("apps.approvals.slack.held_blocks", return_value=[]),
            mock.patch("apps.approvals.slack.decisions.decide_held") as decide_held,
        ):
            self.drain()
        decide_held.assert_called_once_with(action, self.reviewer, "approve", channel="slack")

    def test_approve_button_maps_verified_email_and_audits(self):
        post = self.make_post()
        with (
            mock.patch("apps.approvals.slack._get", return_value=self.slack_user()),
            mock.patch("apps.approvals.slack.api_call") as call,
        ):
            resp = self.post(self.button(post))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.status_of(post), {"approved"})
        action = ApprovalAction.objects.get(post=post, action="approved")
        self.assertEqual((action.channel, action.user), ("slack", self.reviewer))
        self.assertEqual(call.call_args.args[0], "chat.update")

    def test_unmapped_user_gets_a_link_not_an_approval(self):
        post = self.make_post()
        with (
            mock.patch("apps.approvals.slack._get", return_value=self.slack_user("stranger@example.com")),
            mock.patch("apps.approvals.slack._respond") as respond,
        ):
            self.post(self.button(post))
        self.assertEqual(self.status_of(post), {"pending_review"})
        self.assertIn("approvals", respond.call_args.args[1]["text"])

    def test_unverified_emails_do_not_map(self):
        post = self.make_post()
        EmailAddress.objects.filter(user=self.reviewer).update(verified=False)
        with (
            mock.patch("apps.approvals.slack._get", return_value=self.slack_user()),
            mock.patch("apps.approvals.slack._respond"),
        ):
            self.post(self.button(post))
        self.assertEqual(self.status_of(post), {"pending_review"})
        EmailAddress.objects.filter(user=self.reviewer).update(verified=True)
        with (
            mock.patch("apps.approvals.slack._get", return_value=self.slack_user(confirmed=False)),
            mock.patch("apps.approvals.slack._respond"),
        ):
            self.post(self.button(post))
        self.assertEqual(self.status_of(post), {"pending_review"})

    def test_member_without_permission_cannot_approve(self):
        post = self.make_post()
        WorkspaceMembership.objects.filter(user=self.reviewer).update(workspace_role="viewer")
        with (
            mock.patch("apps.approvals.slack._get", return_value=self.slack_user()),
            mock.patch("apps.approvals.slack._respond") as respond,
        ):
            self.post(self.button(post))
        self.assertEqual(self.status_of(post), {"pending_review"})
        self.assertTrue(respond.called)

    def test_changed_post_is_refused(self):
        post = self.make_post()
        payload = self.button(post)
        Post.objects.filter(pk=post.pk).update(caption="Edited after the message")
        with (
            mock.patch("apps.approvals.slack._get", return_value=self.slack_user()),
            mock.patch("apps.approvals.slack._respond") as respond,
        ):
            self.post(payload)
        self.assertEqual(self.status_of(post), {"pending_review"})
        self.assertIn("changed", respond.call_args.args[1]["text"])

    def test_request_changes_opens_modal_then_submission_decides(self):
        post = self.make_post()
        with (
            mock.patch("apps.approvals.slack._get") as get,
            mock.patch("apps.approvals.slack.api_call") as call,
        ):
            self.post(self.button(post, slack.ACTION_CHANGES))
        # Only the dialog is opened inside the three seconds; the sender is not looked up yet.
        get.assert_not_called()
        self.assertEqual([c.args[0] for c in call.call_args_list], ["views.open"])
        meta = json.loads(call.call_args.args[2]["view"]["private_metadata"])
        submission = {
            "type": "view_submission",
            "team": {"id": "T1"},
            "user": {"id": "U1"},
            "view": {
                "callback_id": slack.MODAL_CALLBACK,
                "private_metadata": json.dumps(meta),
                "state": {"values": {slack.COMMENT_BLOCK: {slack.COMMENT_ACTION: {"value": ""}}}},
            },
        }
        with (
            mock.patch("apps.approvals.slack._get", return_value=self.slack_user()),
            mock.patch("apps.approvals.slack.api_call"),
        ):
            empty = self.post(submission)
        self.assertEqual(empty.json()["response_action"], "errors")
        submission["view"]["state"]["values"][slack.COMMENT_BLOCK][slack.COMMENT_ACTION]["value"] = "Fix the hook"
        with (
            mock.patch("apps.approvals.slack._get", return_value=self.slack_user()),
            mock.patch("apps.approvals.slack.api_call"),
        ):
            ok = self.post(submission, ts=time.time() + 1)
        self.assertEqual(ok.json(), {"response_action": "clear"})
        self.assertEqual(self.status_of(post), {"changes_requested"})
        self.assertEqual(ApprovalAction.objects.get(post=post, action="changes_requested").channel, "slack")

    def test_feature_hidden_without_credentials(self):
        with override_settings(PLATFORM_CREDENTIALS_FROM_ENV={"slack": {"client_id": "a", "client_secret": "b"}}):
            self.assertFalse(slack.is_available(self.ws))
            self.assertEqual(slack.settings_context(self.ws), {"available": False})
        self.assertTrue(slack.is_available(self.ws))

    def test_settings_page_shows_slack_card_only_when_available(self):
        self.client.force_login(self.reviewer)
        url = reverse("workspaces:approvals_settings", kwargs={"workspace_id": self.ws.id})
        with mock.patch("apps.approvals.slack._get", return_value={"ok": True, "channels": []}):
            self.assertContains(self.client.get(url), "Slack approvals")
        with override_settings(PLATFORM_CREDENTIALS_FROM_ENV={}):
            self.assertNotContains(self.client.get(url), "Slack approvals")

    def test_only_managers_can_connect_and_state_is_bound_to_user(self):
        author = self.author
        self.client.force_login(author)
        resp = self.client.post(reverse("approvals:slack_connect", kwargs={"workspace_id": self.ws.id}))
        self.assertEqual(resp.status_code, 403)
        self.client.force_login(self.reviewer)
        resp = self.client.post(reverse("approvals:slack_connect", kwargs={"workspace_id": self.ws.id}))
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.url.startswith("https://slack.com/oauth/v2/authorize"))
        state = slack.read_state(resp.url.split("state=")[1].split("&")[0].replace("%3A", ":"))
        self.assertEqual(state["u"], str(self.reviewer.pk))
        self.client.force_login(author)
        bad = self.client.get(reverse("approval_slack:callback"), {"state": resp.url.split("state=")[1], "code": "x"})
        self.assertEqual(bad.status_code, 400)

    def test_submission_queues_a_slack_request(self):
        post = self.make_post(status="draft")
        with mock.patch("apps.approvals.slack.post_request_task") as task:
            services.submit_for_review(post, self.author, self.ws)
        task.assert_called_once_with(str(post.id))

    def test_request_blocks_show_exact_content_and_buttons(self):
        post = self.make_post(accounts=[self.account, self.bluesky])
        PlatformPost.objects.filter(post=post, social_account=self.bluesky).update(
            platform_specific_caption="Bsky wording"
        )
        targets = list(post.platform_posts.select_related("social_account"))
        blocks = slack.request_blocks(post, targets, "fp")
        text = json.dumps(blocks)
        self.assertIn("Bsky wording", text)
        self.assertIn("Launch day is here", text)
        self.assertEqual([e["action_id"] for e in blocks[-1]["elements"]], [slack.ACTION_APPROVE, slack.ACTION_CHANGES])
