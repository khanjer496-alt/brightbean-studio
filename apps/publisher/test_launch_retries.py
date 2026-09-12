from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import patch

from django.db import close_old_connections
from django.test import TransactionTestCase
from django.utils import timezone

from apps.composer.models import PlatformPost, Post
from apps.organizations.models import Organization
from apps.publisher.engine import PublishEngine
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace


class PublishRetrySafetyTest(TransactionTestCase):
    def setUp(self):
        org = Organization.objects.create(name="Retry tests")
        workspace = Workspace.objects.create(organization=org, name="Tests")
        account = SocialAccount.objects.create(
            workspace=workspace,
            platform="linkedin_personal",
            account_platform_id="fake-test-account",
            account_name="Test",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        self.post = Post.objects.create(workspace=workspace, caption="test only")
        self.pp = PlatformPost.objects.create(
            post=self.post,
            social_account=account,
            status=PlatformPost.Status.SCHEDULED,
            scheduled_at=timezone.now() - timedelta(minutes=5),
            retry_count=1,
            next_retry_at=timezone.now() + timedelta(minutes=5),
        )
        self.success = {"success": True, "platform_post_id": "fake-published-id"}

    def test_backoff_is_not_bypassed_by_normal_poll_or_stale_retry_snapshot(self):
        engine = PublishEngine()
        self.assertNotIn(self.pp, engine._get_due_platform_posts())
        with patch.object(engine, "_dispatch_to_provider") as dispatch:
            engine._process_retries()
            engine._publish_post_group(self.post, [self.pp])
        dispatch.assert_not_called()
        self.pp.refresh_from_db()
        self.assertEqual(self.pp.status, PlatformPost.Status.SCHEDULED)

    def test_competing_workers_cannot_dispatch_same_retry_twice(self):
        self.pp.next_retry_at = timezone.now() - timedelta(seconds=1)
        self.pp.save(update_fields=["next_retry_at"])

        def run():
            try:
                PublishEngine()._process_retries()
            finally:
                close_old_connections()

        with (
            patch.object(PublishEngine, "_dispatch_to_provider", return_value=self.success) as dispatch,
            ThreadPoolExecutor(max_workers=2) as workers,
        ):
            list(workers.map(lambda _: run(), range(2)))
        self.assertEqual(dispatch.call_count, 1)
        self.pp.refresh_from_db()
        self.assertEqual(self.pp.status, PlatformPost.Status.PUBLISHED)

    def test_logging_failure_after_remote_success_never_schedules_second_publish(self):
        engine = PublishEngine()
        with (
            patch.object(engine, "_dispatch_to_provider", return_value=self.success),
            patch("apps.publisher.engine.PublishLog.objects.create", side_effect=RuntimeError("test log failure")),
            patch.object(engine, "_schedule_retry") as retry,
        ):
            engine._publish_platform_post(self.pp)
        retry.assert_not_called()
        self.pp.refresh_from_db()
        self.assertEqual(self.pp.status, PlatformPost.Status.PUBLISHED)
        self.assertEqual(self.pp.platform_post_id, "fake-published-id")

    def test_persistence_failure_after_remote_success_is_parked_for_review(self):
        engine = PublishEngine()
        with (
            patch.object(engine, "_dispatch_to_provider", return_value=self.success),
            patch.object(self.pp, "save", side_effect=RuntimeError("test persistence failure")),
            patch.object(engine, "_schedule_retry") as retry,
        ):
            engine._publish_platform_post(self.pp)
        retry.assert_not_called()
        self.pp.refresh_from_db()
        self.assertEqual(self.pp.status, PlatformPost.Status.FAILED)
        self.assertNotIn(self.pp, engine._get_due_platform_posts())

    def test_accepted_facebook_response_parse_failure_never_retries(self):
        from unittest.mock import Mock

        from apps.social_accounts.error_messages import PUBLISH_AMBIGUOUS_MESSAGE
        from providers.facebook import FacebookProvider

        self.pp.social_account.platform = "facebook"
        self.pp.social_account.save(update_fields=["platform"])
        provider = FacebookProvider()
        for malformed_json in (True, False):
            with self.subTest(malformed_json=malformed_json):
                response = Mock(status_code=200)
                if malformed_json:
                    response.json.side_effect = ValueError("accepted response is not JSON")
                else:
                    response.json.return_value = {}  # Accepted response missing its post id.
                with (
                    patch("apps.publisher.engine._provider_and_access_token", return_value=(provider, "fake-token")),
                    patch.object(provider, "_request", return_value=response) as request,
                    patch.object(PublishEngine, "_schedule_retry") as retry,
                ):
                    result = PublishEngine()._publish_platform_post(self.pp)
                self.assertFalse(result["success"])
                request.assert_called_once()
                self.assertEqual(request.call_args.args[0], "POST")
                retry.assert_not_called()
                self.pp.refresh_from_db()
                self.assertEqual(self.pp.status, PlatformPost.Status.FAILED)
                self.assertEqual(self.pp.publish_error, PUBLISH_AMBIGUOUS_MESSAGE)

    def test_known_rejected_request_and_preparation_errors_keep_safe_retry_behavior(self):
        from providers.exceptions import RateLimitError
        from providers.facebook import FacebookProvider

        provider = FacebookProvider()
        with (
            patch("apps.publisher.engine._provider_and_access_token", return_value=(provider, "fake-token")),
            patch.object(provider, "publish_post", side_effect=RateLimitError("rejected 429")),
            patch.object(PublishEngine, "_schedule_retry") as retry,
        ):
            PublishEngine()._publish_platform_post(self.pp)
        retry.assert_called_once()
        with (
            patch("apps.publisher.engine._provider_and_access_token", side_effect=OSError("preparation failed")),
            patch.object(provider, "publish_post") as publish,
            patch.object(PublishEngine, "_schedule_retry") as retry,
        ):
            PublishEngine()._publish_platform_post(self.pp)
        retry.assert_called_once()
        publish.assert_not_called()
