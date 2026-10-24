"""How a publish reacts to what the platform says about the account.

A dead credential should end the post at once and flag the account for
reconnection, once, instead of spending three retries over half an hour on a
token that will never work. A transient fault on a call that cannot have
published anything should still be retried.
"""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import User
from apps.composer.models import PlatformPost, Post
from apps.members.models import WorkspaceMembership
from apps.notifications.models import EventType, Notification
from apps.organizations.models import Organization
from apps.publisher.engine import PublishEngine, _provider_and_access_token, _retry_deadline
from apps.social_accounts.error_messages import PUBLISH_AMBIGUOUS_MESSAGE, PUBLISH_RECONNECT_MESSAGE
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace
from providers.exceptions import (
    APIError,
    AuthRejectedError,
    OAuthError,
    RateLimitError,
    ReconnectRequiredError,
)

Status = SocialAccount.ConnectionStatus


class AccountStateTestCase(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Org")
        self.workspace = Workspace.objects.create(organization=self.org, name="WS")
        self.owner = User.objects.create_user(
            email="owner@example.com", password="x", name="Owner", tos_accepted_at=timezone.now()
        )
        self.viewer = User.objects.create_user(
            email="viewer@example.com", password="x", name="Viewer", tos_accepted_at=timezone.now()
        )
        WorkspaceMembership.objects.create(
            user=self.owner, workspace=self.workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
        )
        WorkspaceMembership.objects.create(
            user=self.viewer, workspace=self.workspace, workspace_role=WorkspaceMembership.WorkspaceRole.VIEWER
        )
        self.account = SocialAccount.objects.create(
            workspace=self.workspace,
            platform="facebook",
            account_platform_id="page-1",
            account_name="Page",
            oauth_access_token="tok",
            oauth_refresh_token="refresh",
            connection_status=Status.CONNECTED,
        )
        self.post = Post.objects.create(workspace=self.workspace, caption="hi", author=self.owner)
        self.pp = PlatformPost.objects.create(
            post=self.post, social_account=self.account, status=PlatformPost.Status.PUBLISHING
        )

    def fail_with(self, error):
        with patch.object(PublishEngine, "_dispatch_to_provider", side_effect=error):
            PublishEngine()._publish_platform_post(self.pp)
        self.pp.refresh_from_db()
        self.account.refresh_from_db()

    def reconnect_notifications(self):
        return Notification.objects.filter(event_type=EventType.SOCIAL_ACCOUNT_DISCONNECTED)


class RevokedTokenAtPublishTest(AccountStateTestCase):
    def test_a_token_the_platform_calls_dead_fails_the_post_without_retries(self):
        self.fail_with(AuthRejectedError("Graph 190", status_code=400, platform="Facebook"))

        self.assertEqual(self.pp.status, PlatformPost.Status.FAILED)
        self.assertEqual(self.pp.retry_count, 0)
        self.assertIsNone(self.pp.next_retry_at)
        self.assertEqual(self.pp.publish_error, PUBLISH_RECONNECT_MESSAGE)

    def test_the_account_is_flagged_and_only_people_who_can_reconnect_are_told(self):
        self.fail_with(AuthRejectedError("Graph 190", status_code=400, platform="Facebook"))

        self.assertEqual(self.account.connection_status, Status.ERROR)
        self.assertTrue(self.account.needs_reconnect)
        self.assertEqual(list(self.reconnect_notifications().values_list("user", flat=True)), [self.owner.pk])

    def test_a_second_failure_on_the_same_dead_token_does_not_notify_again(self):
        self.fail_with(AuthRejectedError("Graph 190", status_code=400, platform="Facebook"))
        other = PlatformPost.objects.create(
            post=Post.objects.create(workspace=self.workspace, caption="again", author=self.owner),
            social_account=self.account,
            status=PlatformPost.Status.PUBLISHING,
        )
        with patch.object(
            PublishEngine, "_dispatch_to_provider", side_effect=AuthRejectedError("190", status_code=400)
        ):
            PublishEngine()._publish_platform_post(other)

        other.refresh_from_db()
        self.assertEqual(other.status, PlatformPost.Status.FAILED)
        self.assertEqual(self.reconnect_notifications().count(), 1)

    def test_a_dead_token_with_a_5xx_status_is_not_parked_as_an_unknown_outcome(self):
        """The platform refused the call before writing anything."""
        self.fail_with(
            AuthRejectedError("Graph 190", status_code=500, platform="Facebook", ambiguous_write=True),
        )

        self.assertEqual(self.pp.publish_error, PUBLISH_RECONNECT_MESSAGE)
        self.assertNotEqual(self.pp.publish_error, PUBLISH_AMBIGUOUS_MESSAGE)

    def test_a_token_rotated_by_another_process_is_retried_instead(self):
        SocialAccount.objects.filter(pk=self.account.pk).update(oauth_access_token="rotated")
        self.fail_with(AuthRejectedError("Graph 190", status_code=400, platform="Facebook"))

        self.assertEqual(self.pp.status, PlatformPost.Status.SCHEDULED)
        self.assertEqual(self.pp.retry_count, 1)
        self.assertEqual(self.account.connection_status, Status.CONNECTED)


class TransientFaultsAtPublishTest(AccountStateTestCase):
    def test_a_5xx_on_a_call_that_cannot_publish_is_retried(self):
        self.fail_with(APIError("503", status_code=503, platform="Facebook", ambiguous_write=False))

        self.assertEqual(self.pp.status, PlatformPost.Status.SCHEDULED)
        self.assertEqual(self.pp.retry_count, 1)
        self.assertEqual(self.account.connection_status, Status.CONNECTED)

    def test_a_5xx_on_the_publishing_call_is_still_parked(self):
        self.fail_with(APIError("503", status_code=503, platform="Facebook", ambiguous_write=True))

        self.assertEqual(self.pp.status, PlatformPost.Status.FAILED)
        self.assertEqual(self.pp.publish_error, PUBLISH_AMBIGUOUS_MESSAGE)

    def test_a_rate_limit_hint_sets_the_retry_time(self):
        before = timezone.now()
        self.fail_with(RateLimitError("slow down", retry_after=3600, platform="Facebook"))

        self.assertEqual(self.pp.status, PlatformPost.Status.SCHEDULED)
        self.assertGreaterEqual(self.pp.next_retry_at, before + timedelta(seconds=3600))
        self.assertLess(self.pp.next_retry_at, before + timedelta(seconds=3700))

    def test_a_rate_limit_hint_never_pulls_the_retry_in_ahead_of_the_ladder(self):
        before = timezone.now()
        self.fail_with(RateLimitError("slow down", retry_after=5, platform="Facebook"))

        self.assertGreaterEqual(self.pp.next_retry_at, before + timedelta(seconds=60))

    def test_a_rate_limit_without_a_hint_uses_the_ordinary_ladder(self):
        self.assertIsNone(_retry_deadline(RateLimitError("slow down"), 0))
        self.assertIsNone(_retry_deadline(APIError("500", status_code=500), 0))


class RejectedRefreshAtPublishTest(AccountStateTestCase):
    def refresh_fails_with(self, error, *, expires_in):
        self.account.token_expires_at = timezone.now() + expires_in
        self.account.save(update_fields=["token_expires_at"])
        self.pp.social_account = self.account
        return patch.object(SocialAccount, "refresh_oauth_token", side_effect=error)

    def test_a_rejected_refresh_of_a_lapsed_token_fails_the_post_at_once(self):
        with (
            self.refresh_fails_with(OAuthError("invalid_grant", platform="Facebook"), expires_in=-timedelta(hours=1)),
            patch("apps.publisher.engine.get_provider") as get_provider,
        ):
            PublishEngine()._publish_platform_post(self.pp)

        self.pp.refresh_from_db()
        self.account.refresh_from_db()
        get_provider.return_value.publish_post.assert_not_called()
        self.assertEqual(self.pp.status, PlatformPost.Status.FAILED)
        self.assertEqual(self.pp.retry_count, 0)
        self.assertEqual(self.pp.publish_error, PUBLISH_RECONNECT_MESSAGE)
        self.assertEqual(self.account.connection_status, Status.ERROR)
        self.assertEqual(self.reconnect_notifications().count(), 1)

    def test_a_rejected_refresh_of_a_token_with_days_left_still_publishes_and_warns(self):
        with (
            self.refresh_fails_with(OAuthError("invalid_grant", platform="Facebook"), expires_in=timedelta(days=2)),
            patch("apps.publisher.engine.get_provider") as get_provider,
        ):
            provider, token = _provider_and_access_token(self.account, raise_on_rejected_refresh=True)

        self.assertIs(provider, get_provider.return_value)
        self.assertEqual(token, "tok")
        self.account.refresh_from_db()
        self.assertEqual(self.account.connection_status, Status.TOKEN_EXPIRING)
        self.assertEqual(self.reconnect_notifications().count(), 0)

    def test_a_refresh_that_failed_because_the_platform_was_down_flags_nothing(self):
        self.account.token_expires_at = timezone.now() - timedelta(hours=1)
        self.account.save(update_fields=["token_expires_at"])
        with (
            patch.object(
                SocialAccount, "refresh_oauth_token", side_effect=APIError("503", status_code=503, platform="Facebook")
            ),
            patch("apps.publisher.engine.get_provider"),
        ):
            _provider_and_access_token(self.account, raise_on_rejected_refresh=True)

        self.account.refresh_from_db()
        self.assertEqual(self.account.connection_status, Status.CONNECTED)
        self.assertEqual(self.reconnect_notifications().count(), 0)

    def test_the_first_comment_path_flags_but_does_not_raise(self):
        self.account.token_expires_at = timezone.now() - timedelta(hours=1)
        self.account.save(update_fields=["token_expires_at"])
        with (
            patch.object(SocialAccount, "refresh_oauth_token", side_effect=OAuthError("invalid_grant")),
            patch("apps.publisher.engine.get_provider"),
        ):
            _, token = _provider_and_access_token(self.account)

        self.assertEqual(token, "tok")
        self.account.refresh_from_db()
        self.assertEqual(self.account.connection_status, Status.ERROR)

    def test_the_raised_error_is_the_reconnect_kind(self):
        self.account.token_expires_at = timezone.now() - timedelta(hours=1)
        self.account.save(update_fields=["token_expires_at"])
        with (
            patch.object(SocialAccount, "refresh_oauth_token", side_effect=OAuthError("invalid_grant")),
            patch("apps.publisher.engine.get_provider"),
            self.assertRaises(ReconnectRequiredError) as caught,
        ):
            _provider_and_access_token(self.account, raise_on_rejected_refresh=True)
        self.assertFalse(caught.exception.retryable)


class FirstCommentOnADeadTokenTest(AccountStateTestCase):
    def test_a_first_comment_is_not_retried_on_a_token_the_platform_called_dead(self):
        from apps.publisher.engine import _is_retryable_first_comment_failure

        assert not _is_retryable_first_comment_failure(AuthRejectedError("190", status_code=400))
        assert not _is_retryable_first_comment_failure(ReconnectRequiredError("dead", retryable=False))
        assert _is_retryable_first_comment_failure(RateLimitError("slow down"))
