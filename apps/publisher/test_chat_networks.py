"""Telegram, Discord, Slack and Reddit through the real publish engine, HTTP mocked.

The engine builds the provider from the account (credentials, per-account
values, per-post settings), calls ``publish_post`` and sorts a failure into
retry, permanent or ambiguous. These tests drive that whole path and stub only
the HTTP layer, so a mismatch between what the engine hands over and what a
provider reads would show up here.
"""

from unittest.mock import MagicMock, patch

import httpx
from django.test import TestCase

from apps.composer.models import PlatformPost, Post
from apps.organizations.models import Organization
from apps.publisher.engine import PublishEngine
from apps.social_accounts.error_messages import PUBLISH_AMBIGUOUS_MESSAGE, PUBLISH_RATE_LIMIT_MESSAGE
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

CREDENTIALS = {
    "telegram": {"bot_token": "123:ABC"},
    "discord": {"client_id": "app", "client_secret": "shh", "bot_token": "bot"},
    "slack": {"client_id": "app", "client_secret": "shh"},
    "reddit": {"client_id": "app", "client_secret": "shh", "user_agent": "web:com.test:v1 (by /u/tester)"},
}


def ok(body):
    return MagicMock(status_code=200, json=MagicMock(return_value=body), headers={})


def http_error(status, body=None, headers=None):
    return httpx.Response(status, json=body or {}, headers=headers, request=httpx.Request("POST", "https://x/y"))


class ChatNetworkPublishMixin:
    platform = ""
    account_fields: dict = {}
    platform_extra: dict = {}

    def setUp(self):
        self.org = Organization.objects.create(name="Org")
        self.workspace = Workspace.objects.create(organization=self.org, name="WS")
        self.account = SocialAccount.objects.create(
            workspace=self.workspace,
            platform=self.platform,
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
            **{
                "account_platform_id": "dest-1",
                "account_name": "Destination",
                "oauth_access_token": "stored-token",
                **self.account_fields,
            },
        )
        self.post = Post.objects.create(workspace=self.workspace, caption="Hello world", title="A title")
        self.platform_post = PlatformPost.objects.create(
            post=self.post,
            social_account=self.account,
            status=PlatformPost.Status.PUBLISHING,
            platform_extra=self.platform_extra,
        )
        patcher = patch(
            "apps.publisher.engine.resolve_platform_credentials", return_value=dict(CREDENTIALS[self.platform])
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def publish(self, **patch_kwargs):
        from providers import PROVIDER_REGISTRY

        provider_cls = PROVIDER_REGISTRY[self.platform]
        with patch.object(provider_cls, "_request", **patch_kwargs) as request:
            result = PublishEngine()._publish_platform_post(self.platform_post)
        self.platform_post.refresh_from_db()
        return result, request


class TelegramPublishTest(ChatNetworkPublishMixin, TestCase):
    platform = "telegram"
    account_fields = {"account_platform_id": "-1001", "oauth_access_token": "-1001", "account_handle": "@news"}

    def test_publishes_to_the_stored_chat_with_the_environment_bot(self):
        result, request = self.publish(return_value=ok({"ok": True, "result": {"message_id": 5}}))
        assert result["success"]
        self.assertEqual(request.call_args.args[1], "https://api.telegram.org/bot123:ABC/sendMessage")
        self.assertEqual(request.call_args.kwargs["json"]["chat_id"], "-1001")
        self.assertEqual(self.platform_post.status, PlatformPost.Status.PUBLISHED)
        self.assertEqual(self.platform_post.platform_post_id, "5")

    def test_flood_control_is_retried_later(self):
        body = {"ok": False, "description": "Too Many Requests", "parameters": {"retry_after": 12}}
        from providers.telegram import TelegramProvider

        error = TelegramProvider(CREDENTIALS["telegram"])._error_for_response(http_error(429, body))
        self.publish(side_effect=error)
        self.assertEqual(self.platform_post.status, PlatformPost.Status.SCHEDULED)
        self.assertEqual(self.platform_post.retry_count, 1)
        self.assertEqual(self.platform_post.publish_error, PUBLISH_RATE_LIMIT_MESSAGE)

    def test_a_removed_bot_fails_at_once_with_a_clear_sentence(self):
        from providers.telegram import TelegramProvider

        body = {"ok": False, "error_code": 403, "description": "Forbidden: bot was kicked from the channel chat"}
        self.publish(side_effect=TelegramProvider(CREDENTIALS["telegram"])._error_for_response(http_error(403, body)))
        self.assertEqual(self.platform_post.status, PlatformPost.Status.FAILED)
        self.assertEqual(self.platform_post.retry_count, 0)
        self.assertIn("removed from this chat", self.platform_post.publish_error)

    def test_a_timeout_after_the_send_is_not_retried(self):
        with patch("providers.base.httpx.Client") as client:
            client.return_value.__enter__.return_value.request.side_effect = httpx.ReadTimeout("slow")
            PublishEngine()._publish_platform_post(self.platform_post)
        self.platform_post.refresh_from_db()
        self.assertEqual(self.platform_post.status, PlatformPost.Status.FAILED)
        self.assertEqual(self.platform_post.publish_error, PUBLISH_AMBIGUOUS_MESSAGE)
        self.assertEqual(self.platform_post.retry_count, 0)


class DiscordPublishTest(ChatNetworkPublishMixin, TestCase):
    platform = "discord"
    account_fields = {"account_platform_id": "c1", "oauth_access_token": "c1", "webhook_target_id": "g1"}

    def test_publishes_to_the_channel_and_links_the_message(self):
        result, request = self.publish(return_value=ok({"id": "m1"}))
        assert result["success"]
        self.assertEqual(request.call_args.args[1], "https://discord.com/api/v10/channels/c1/messages")
        self.assertEqual(request.call_args.kwargs["headers"]["Authorization"], "Bot bot")
        self.assertEqual(self.platform_post.platform_post_id, "m1")
        self.assertEqual(self.platform_post.status, PlatformPost.Status.PUBLISHED)

    def test_missing_permissions_fail_at_once(self):
        from providers.discord import DiscordProvider

        error = DiscordProvider(CREDENTIALS["discord"])._error_for_response(
            http_error(403, {"message": "Missing Permissions", "code": 50013})
        )
        self.publish(side_effect=error)
        self.assertEqual(self.platform_post.status, PlatformPost.Status.FAILED)
        self.assertEqual(self.platform_post.retry_count, 0)
        self.assertIn("Send Messages", self.platform_post.publish_error)


class SlackPublishTest(ChatNetworkPublishMixin, TestCase):
    platform = "slack"
    account_fields = {"account_platform_id": "C1", "oauth_access_token": "xoxb-1"}

    def test_publishes_with_the_workspace_token_to_the_accounts_channel(self):
        result, request = self.publish(
            side_effect=[ok({"ok": True, "ts": "1.1"}), ok({"ok": True, "permalink": "https://acme.slack.com/p"})]
        )
        assert result["success"]
        first = request.call_args_list[0]
        self.assertEqual(first.kwargs["access_token"], "xoxb-1")
        self.assertEqual(first.kwargs["json"]["channel"], "C1")
        self.assertEqual(self.platform_post.platform_post_id, "1.1")

    def test_a_revoked_app_fails_at_once(self):
        self.publish(return_value=ok({"ok": False, "error": "token_revoked"}))
        self.assertEqual(self.platform_post.status, PlatformPost.Status.FAILED)
        self.assertEqual(self.platform_post.retry_count, 0)


class RedditPublishTest(ChatNetworkPublishMixin, TestCase):
    platform = "reddit"
    account_fields = {"account_platform_id": "u1", "oauth_access_token": "at", "account_handle": "alice"}
    platform_extra = {"subreddit": "python", "flair_id": "f1", "link_url": "https://example.com/a", "post_kind": "link"}

    def test_the_post_settings_reach_the_provider(self):
        result, request = self.publish(
            return_value=ok({"json": {"errors": [], "data": {"id": "abc", "name": "t3_abc", "url": "https://r/x"}}})
        )
        assert result["success"]
        form = request.call_args.kwargs["data"]
        self.assertEqual(form["sr"], "python")
        self.assertEqual(form["title"], "A title")
        self.assertEqual(form["kind"], "link")
        self.assertEqual(form["url"], "https://example.com/a")
        self.assertEqual(form["flair_id"], "f1")
        self.assertEqual(self.platform_post.platform_post_id, "t3_abc")

    def test_a_missing_subreddit_is_refused_before_any_request(self):
        self.platform_post.platform_extra = {}
        self.platform_post.save()
        result, request = self.publish(side_effect=AssertionError("no request"))
        assert not result["success"]
        self.assertEqual(self.platform_post.status, PlatformPost.Status.FAILED)
        self.assertEqual(self.platform_post.retry_count, 0)
        self.assertIn("Subreddit is required", self.platform_post.publish_error)

    def test_the_posting_cooldown_is_retried_after_its_wait(self):
        body = {"json": {"errors": [["RATELIMIT", "try again in 9 minutes.", "ratelimit"]], "ratelimit": 540.0}}
        self.publish(return_value=ok(body))
        self.assertEqual(self.platform_post.status, PlatformPost.Status.SCHEDULED)
        self.assertEqual(self.platform_post.retry_count, 1)

    def test_a_subreddit_that_does_not_exist_fails_at_once(self):
        self.publish(return_value=ok({"json": {"errors": [["SUBREDDIT_NOEXIST", "x", "sr"]]}}))
        self.assertEqual(self.platform_post.status, PlatformPost.Status.FAILED)
        self.assertEqual(self.platform_post.retry_count, 0)
        self.assertIn("doesn't exist", self.platform_post.publish_error)
