"""X through the real publish engine, HTTP mocked: classification of success, rate limits, budget and lost responses."""

from datetime import UTC, timedelta

import httpx
from django.test import TestCase
from django.utils import timezone

from apps.composer.models import PlatformPost, PlatformPostPart
from apps.publisher.engine import PublishEngine, _publish_thread_part_task
from apps.publisher.test_chat_networks import CREDENTIALS, ChatNetworkPublishMixin, http_error, ok
from apps.publisher.test_threads import ThreadTestMixin
from apps.social_accounts.error_messages import PUBLISH_AMBIGUOUS_MESSAGE, PUBLISH_RATE_LIMIT_MESSAGE
from providers.x import BUDGET_MESSAGE, XProvider

CREDENTIALS["x"] = {"client_id": "app", "client_secret": "shh"}


class XPublishTest(ChatNetworkPublishMixin, TestCase):
    platform = "x"
    account_fields = {"account_platform_id": "42", "account_handle": "alice"}
    platform_extra = {"reply_settings": "following", "poll_options": "Yes | No", "poll_duration": "60"}

    def error(self, status, body=None, headers=None):
        return XProvider(CREDENTIALS["x"])._error_for_response(http_error(status, body, headers))

    def test_the_post_settings_reach_x(self):
        result, request = self.publish(return_value=ok({"data": {"id": "900", "text": "Hello world"}}))
        assert result["success"]
        body = request.call_args.kwargs["json"]
        self.assertEqual(body["text"], "Hello world")
        self.assertEqual(body["reply_settings"], "following")
        self.assertEqual(body["poll"], {"options": ["Yes", "No"], "duration_minutes": 60})
        self.assertEqual(self.platform_post.status, PlatformPost.Status.PUBLISHED)
        self.assertEqual(self.platform_post.platform_post_id, "900")

    def test_a_rate_limit_waits_for_the_reset_time_x_gave(self):
        reset = timezone.now().replace(microsecond=0) + timedelta(minutes=14)
        self.publish(side_effect=self.error(429, {}, {"x-rate-limit-reset": str(int(reset.timestamp()))}))
        self.assertEqual(self.platform_post.status, PlatformPost.Status.SCHEDULED)
        self.assertEqual(self.platform_post.retry_count, 1)
        self.assertEqual(self.platform_post.next_retry_at, reset.astimezone(UTC))
        self.assertEqual(self.platform_post.publish_error, PUBLISH_RATE_LIMIT_MESSAGE)

    def test_spent_credits_fail_at_once_and_say_what_to_do(self):
        self.publish(side_effect=self.error(402, {"title": "Payment Required"}))
        self.assertEqual(self.platform_post.status, PlatformPost.Status.FAILED)
        self.assertEqual(self.platform_post.retry_count, 0)
        self.assertEqual(self.platform_post.publish_error, BUDGET_MESSAGE)

    def test_a_timeout_after_the_post_is_never_retried(self):
        from unittest.mock import patch

        with patch("providers.base.httpx.Client") as client:
            client.return_value.__enter__.return_value.request.side_effect = httpx.ReadTimeout("slow")
            PublishEngine()._publish_platform_post(self.platform_post)
        self.platform_post.refresh_from_db()
        self.assertEqual(self.platform_post.status, PlatformPost.Status.FAILED)
        self.assertEqual(self.platform_post.publish_error, PUBLISH_AMBIGUOUS_MESSAGE)
        self.assertEqual(self.platform_post.retry_count, 0)


class XReplyChainTest(ThreadTestMixin, TestCase):
    platform = "x"

    def setUp(self):
        super().setUp()
        self.parts = self._add_parts("two", "three", "four")

    def _run(self, responses, timeline=()):
        """Publish the thread; ``timeline`` is what a reconciliation reads back as the account's posts."""
        calls = []
        iterator = iter(responses)

        def fake_request(method, url, **kwargs):
            if method == "GET" and url.endswith("/2/users/me"):
                return ok({"data": {"id": "42"}})
            if method == "GET":
                return ok({"data": list(timeline)})
            calls.append(kwargs["json"])
            outcome = next(iterator)
            if isinstance(outcome, Exception):
                raise outcome
            return ok({"data": {"id": outcome}})

        from unittest.mock import patch

        provider = XProvider(CREDENTIALS["x"])
        with patch.object(XProvider, "_request", side_effect=fake_request):
            self._start(provider)
        return provider, calls

    def test_the_thread_is_a_reply_chain(self):
        _provider, calls = self._run(["s-2", "s-3", "s-4"])
        self.assertEqual([c["reply"]["in_reply_to_tweet_id"] for c in calls], ["root-1", "s-2", "s-3"])
        self.assertEqual([s.platform_part_id for s in self._states()], ["s-2", "s-3", "s-4"])
        self.platform_post.refresh_from_db()
        self.assertEqual(self.platform_post.thread_status, "complete")

    def test_a_rejected_middle_part_keeps_the_live_ones_and_resumes_from_it(self):
        from unittest.mock import patch

        from apps.publisher.engine import resume_thread
        from providers.exceptions import PublishError

        rejected = PublishError("X rejected the request: too long", platform="X", retryable=False)
        provider, calls = self._run(["s-2", rejected])
        statuses = [s.status for s in self._states()]
        self.assertEqual(
            statuses,
            [PlatformPostPart.Status.PUBLISHED, PlatformPostPart.Status.FAILED, PlatformPostPart.Status.PENDING],
        )
        self.platform_post.refresh_from_db()
        self.assertEqual(self.platform_post.thread_status, "partial")

        queue = []
        with patch(
            "apps.publisher.engine._publish_thread_part_task",
            side_effect=lambda pp_id, part_id, schedule=0: queue.append((pp_id, part_id)),
        ):
            resume_thread(self.platform_post)
        outcomes = iter(["s-3", "s-4"])
        resumed = []

        def fake_request(method, url, **kwargs):
            resumed.append(kwargs["json"])
            return ok({"data": {"id": next(outcomes)}})

        with (
            patch.object(XProvider, "_request", side_effect=fake_request),
            patch("apps.publisher.engine._provider_and_access_token", return_value=(provider, "token")),
            patch("apps.publisher.engine._publish_thread_part_task", side_effect=lambda *a, **k: queue.append(a)),
        ):
            while queue:
                pp_id, part_id = queue.pop(0)[:2]
                _publish_thread_part_task.task_function(pp_id, part_id)
        # Part two is not sent again; part three replies to it.
        self.assertEqual([c["text"] for c in resumed], ["three", "four"])
        self.assertEqual(resumed[0]["reply"]["in_reply_to_tweet_id"], "s-2")
        self.platform_post.refresh_from_db()
        self.assertEqual(self.platform_post.thread_status, "complete")

    def test_a_lost_response_that_did_post_is_found_not_resent(self):
        from providers.exceptions import APIError

        lost = APIError("timeout", platform="X", ambiguous_write=True)
        _provider, calls = self._run([lost, "s-3", "s-4"], timeline=[{"id": "s-2", "text": "two"}])
        # Part two went out once; the retry found it on the timeline and the chain continued from it.
        self.assertEqual([c["text"] for c in calls], ["two", "three", "four"])
        self.assertEqual([s.platform_part_id for s in self._states()], ["s-2", "s-3", "s-4"])
        self.assertEqual(calls[1]["reply"]["in_reply_to_tweet_id"], "s-2")

    def test_a_lost_response_that_did_not_post_is_sent_again(self):
        from providers.exceptions import APIError

        lost = APIError("timeout", platform="X", ambiguous_write=True)
        _provider, calls = self._run([lost, "s-2", "s-3", "s-4"])
        self.assertEqual([c["text"] for c in calls], ["two", "two", "three", "four"])
        self.assertEqual([s.platform_part_id for s in self._states()], ["s-2", "s-3", "s-4"])
