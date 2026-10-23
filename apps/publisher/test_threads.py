"""Tests for publishing a thread's later parts.

The failure these guard against is the expensive one: a thread that stops at
part 3 of 5 and is then "retried" from the top, posting parts 1 and 2 a second
time on a live account. Every part has its own row, its own retry budget and a
claim, so a resume picks up exactly where the thread stopped.
"""

from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.utils import timezone

from apps.composer.models import PlatformPost, PlatformPostPart, Post, PostPart
from apps.organizations.models import Organization
from apps.publisher.engine import (
    THREAD_PART_MAX_RETRIES,
    THREAD_PART_RETRY_BACKOFF,
    PublishEngine,
    _publish_thread_part_task,
    resume_thread,
)
from apps.social_accounts.error_messages import THREAD_PART_AMBIGUOUS_MESSAGE
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace
from providers.bluesky import BlueskyProvider
from providers.exceptions import APIError, PublishError, RateLimitError
from providers.facebook import FacebookProvider
from providers.mastodon import MastodonProvider
from providers.threads import ThreadsProvider
from providers.types import PublishResult

PartStatus = PlatformPostPart.Status
ThreadStatus = PlatformPost.ThreadStatus


def _response(body):
    return MagicMock(json=MagicMock(return_value=body), status_code=200, headers={})


class ThreadTestMixin:
    platform = "mastodon"

    def setUp(self):
        self.org = Organization.objects.create(name="Org")
        self.workspace = Workspace.objects.create(organization=self.org, name="WS")
        self.account = SocialAccount.objects.create(
            workspace=self.workspace,
            platform=self.platform,
            account_platform_id="acct-1",
            account_name="Account",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        self.post = Post.objects.create(workspace=self.workspace, caption="Part one")
        self.platform_post = PlatformPost.objects.create(
            post=self.post,
            social_account=self.account,
            status=PlatformPost.Status.PUBLISHED,
            platform_post_id="root-1",
            published_at=timezone.now(),
        )

    def _add_parts(self, *captions, delays=None):
        delays = delays or [0] * len(captions)
        return [
            PostPart.objects.create(post=self.post, position=i, caption=caption, delay_seconds=delay)
            for i, (caption, delay) in enumerate(zip(captions, delays, strict=True), start=1)
        ]

    def _drain(self, provider, *, queue=None):
        """Run queued part tasks in order, as the worker would, recording each schedule."""
        queue = queue if queue is not None else []
        scheduled = []

        def enqueue(pp_id, part_id, schedule=0):
            queue.append((pp_id, part_id))
            scheduled.append(schedule)

        with (
            patch("apps.publisher.engine._provider_and_access_token", return_value=(provider, "token")),
            patch("apps.publisher.engine._publish_thread_part_task", side_effect=enqueue),
        ):
            while queue:
                pp_id, part_id = queue.pop(0)
                _publish_thread_part_task.task_function(pp_id, part_id)
        return scheduled

    def _start(self, provider):
        """Kick the thread off the way a fresh publish does, then drain the queue."""
        queue = []
        with patch(
            "apps.publisher.engine._publish_thread_part_task",
            side_effect=lambda pp_id, part_id, schedule=0: queue.append((pp_id, part_id)),
        ) as kickoff:
            PublishEngine()._maybe_start_thread(self.platform_post)
        first_schedule = kickoff.call_args.kwargs.get("schedule") if kickoff.called else None
        return first_schedule, self._drain(provider, queue=queue)

    def _states(self):
        return list(PlatformPostPart.objects.filter(platform_post=self.platform_post).order_by("part__position"))


class MastodonReplyChainTest(ThreadTestMixin, TestCase):
    platform = "mastodon"

    def test_each_part_replies_to_the_one_before_it(self):
        self._add_parts("Part two", "Part three")
        provider = MastodonProvider(credentials={"instance_url": "https://mastodon.example"})
        statuses = iter(["s-2", "s-3"])

        def fake_request(method, url, **kwargs):
            assert url == "https://mastodon.example/api/v1/statuses"
            return _response({"id": next(statuses), "url": "https://mastodon.example/@a/1"})

        with patch.object(MastodonProvider, "_request", side_effect=fake_request) as request:
            self._start(provider)

        replies = [call.kwargs["data"]["in_reply_to_id"] for call in request.call_args_list]
        self.assertEqual(replies, ["root-1", "s-2"])
        # One idempotency key per part, so a retry can't post the same part twice.
        keys = {call.kwargs["headers"]["Idempotency-Key"] for call in request.call_args_list}
        self.assertEqual(len(keys), 2)
        self.assertEqual([s.platform_part_id for s in self._states()], ["s-2", "s-3"])
        self.assertTrue(all(s.status == PartStatus.PUBLISHED for s in self._states()))
        self.platform_post.refresh_from_db()
        self.assertEqual(self.platform_post.thread_status, ThreadStatus.COMPLETE)


class BlueskyReplyChainTest(ThreadTestMixin, TestCase):
    platform = "bluesky"

    def test_replies_name_the_root_and_the_parent(self):
        self._add_parts("Part two", "Part three")
        self.platform_post.platform_post_id = "at://did:plc:me/app.bsky.feed.post/root"
        # ``platform_extra`` is author input; a ``cid`` there must not be trusted
        # as the root's — the root is looked up on the PDS.
        self.platform_post.platform_extra = {"cid": "forged"}
        self.platform_post.save()
        provider = BlueskyProvider()
        created = iter(
            [
                {"uri": "at://did:plc:me/app.bsky.feed.post/p2", "cid": "cid-2"},
                {"uri": "at://did:plc:me/app.bsky.feed.post/p3", "cid": "cid-3"},
            ]
        )
        records = []

        def fake_request(method, url, **kwargs):
            if url.endswith("getSession"):
                return _response({"did": "did:plc:me", "handle": "me.bsky.social"})
            if url.endswith("getRecord"):
                return _response({"uri": "at://did:plc:me/app.bsky.feed.post/root", "cid": "cid-root"})
            assert url.endswith("createRecord")
            records.append(kwargs["json"]["record"])
            return _response(next(created))

        with patch.object(BlueskyProvider, "_request", side_effect=fake_request):
            self._start(provider)

        self.assertEqual(
            records[0]["reply"],
            {
                "root": {"uri": "at://did:plc:me/app.bsky.feed.post/root", "cid": "cid-root"},
                "parent": {"uri": "at://did:plc:me/app.bsky.feed.post/root", "cid": "cid-root"},
            },
        )
        self.assertEqual(records[1]["reply"]["root"]["cid"], "cid-root")
        self.assertEqual(
            records[1]["reply"]["parent"], {"uri": "at://did:plc:me/app.bsky.feed.post/p2", "cid": "cid-2"}
        )
        self.assertEqual(self._states()[1].url, "https://bsky.app/profile/me.bsky.social/post/p3")

    def test_a_root_without_its_cid_is_looked_up(self):
        self._add_parts("Part two")
        self.platform_post.platform_post_id = "at://did:plc:me/app.bsky.feed.post/root"
        self.platform_post.save()
        provider = BlueskyProvider()
        records = []

        def fake_request(method, url, **kwargs):
            if url.endswith("getRecord"):
                self.assertEqual(kwargs["params"]["rkey"], "root")
                return _response({"cid": "looked-up"})
            if url.endswith("getSession"):
                return _response({"did": "did:plc:me", "handle": "me"})
            records.append(kwargs["json"]["record"])
            return _response({"uri": "at://did:plc:me/app.bsky.feed.post/p2", "cid": "cid-2"})

        with patch.object(BlueskyProvider, "_request", side_effect=fake_request):
            self._start(provider)

        self.assertEqual(records[0]["reply"]["root"]["cid"], "looked-up")


class ThreadsReplyChainTest(ThreadTestMixin, TestCase):
    platform = "threads"

    def test_each_part_is_a_reply_container(self):
        self._add_parts("Part two", "Part three")
        provider = ThreadsProvider()
        published = iter(["t-2", "t-3"])
        containers = []

        def fake_request(method, url, **kwargs):
            if url.endswith("/me"):
                return _response({"id": "user-1"})
            if url.endswith("/threads"):
                containers.append(kwargs["data"])
                return _response({"id": f"container-{len(containers)}"})
            assert url.endswith("/threads_publish")
            return _response({"id": next(published)})

        with (
            patch.object(ThreadsProvider, "_request", side_effect=fake_request),
            patch.object(ThreadsProvider, "get_profile", return_value=MagicMock(platform_id="user-1")),
        ):
            self._start(provider)

        self.assertEqual([c["reply_to_id"] for c in containers], ["root-1", "t-2"])
        self.assertEqual([c["text"] for c in containers], ["Part two", "Part three"])


class FacebookCommentsModeTest(ThreadTestMixin, TestCase):
    platform = "facebook"

    def test_later_parts_are_comments_on_the_first_post(self):
        self._add_parts("Part two", "Part three")
        provider = FacebookProvider(credentials={"page_id": "acct-1"})
        comments = iter(["c-2", "c-3"])
        targets = []

        def fake_request(method, url, **kwargs):
            targets.append(url)
            return _response({"id": next(comments)})

        with patch.object(FacebookProvider, "_request", side_effect=fake_request):
            self._start(provider)

        # Both comment on the root, not on each other — a comments platform
        # has no reply chain.
        self.assertEqual(len(set(targets)), 1)
        self.assertIn("root-1", targets[0])
        self.assertEqual([s.platform_part_id for s in self._states()], ["c-2", "c-3"])

    def test_an_ambiguous_comment_is_reconciled_before_it_is_sent_again(self):
        (part,) = self._add_parts("Part two")
        provider = MagicMock(spec=FacebookProvider)
        provider.thread = FacebookProvider.thread
        provider.supported_post_types = FacebookProvider().supported_post_types
        provider.needs_local_media = True
        provider.publish_thread_part.side_effect = APIError("timeout", platform="Facebook", ambiguous_write=True)
        provider.find_thread_part.return_value = "c-2"

        with patch("apps.publisher.engine._can_reconcile_thread_parts", return_value=True):
            _first, scheduled = self._start(provider)

        # Retried (on the backoff), and the retry found the comment instead of
        # posting a second one.
        self.assertEqual(scheduled, [THREAD_PART_RETRY_BACKOFF[0]])
        provider.publish_thread_part.assert_called_once()
        state = self._states()[0]
        self.assertEqual(state.status, PartStatus.PUBLISHED)
        self.assertEqual(state.platform_part_id, "c-2")


class ThreadEngineTest(ThreadTestMixin, TestCase):
    platform = "mastodon"

    def _provider(self, side_effect):
        provider = MagicMock()
        provider.thread = MastodonProvider.thread
        provider.supported_post_types = MastodonProvider(credentials={}).supported_post_types
        provider.needs_local_media = True
        provider.publish_thread_part.side_effect = side_effect
        return provider

    def test_a_single_post_starts_no_thread(self):
        with patch("apps.publisher.engine._publish_thread_part_task") as task:
            PublishEngine()._maybe_start_thread(self.platform_post)
        task.assert_not_called()
        self.platform_post.refresh_from_db()
        self.assertEqual(self.platform_post.thread_status, ThreadStatus.NONE)

    def test_delays_are_task_schedules_not_sleeps(self):
        self._add_parts("two", "three", delays=[90, 600])
        provider = self._provider(lambda *a, **k: PublishResult(platform_post_id="x"))

        first, scheduled = self._start(provider)

        # The first later part waits its delay after the root; the next waits
        # its own after that — each as the queued task's schedule.
        self.assertEqual(first, 90)
        self.assertEqual(scheduled, [600])

    def test_part_three_failing_keeps_parts_one_and_two_and_resumes_without_duplicates(self):
        parts = self._add_parts("two", "three", "four", "five")
        calls = []

        def publish(token, content, *, root, parent):
            calls.append(content.text)
            if content.text == "three" and calls.count("three") == 1:
                raise PublishError("rejected", platform="Mastodon", retryable=False)
            return PublishResult(platform_post_id=f"id-{content.text}")

        provider = self._provider(publish)
        self._start(provider)

        statuses = [s.status for s in self._states()]
        self.assertEqual(statuses, [PartStatus.PUBLISHED, PartStatus.FAILED, PartStatus.PENDING, PartStatus.PENDING])
        self.platform_post.refresh_from_db()
        self.assertEqual(self.platform_post.status, PlatformPost.Status.PUBLISHED)
        self.assertEqual(self.platform_post.thread_status, ThreadStatus.PARTIAL)
        self.assertIn("part 3", self.platform_post.thread_error)

        queue = []
        with patch(
            "apps.publisher.engine._publish_thread_part_task",
            side_effect=lambda pp_id, part_id, schedule=0: queue.append((pp_id, part_id)),
        ):
            resume_thread(self.platform_post)
        self.assertEqual(queue, [(str(self.platform_post.id), str(parts[1].id))])
        self._drain(provider, queue=queue)

        # "two" went out once; "three" was retried once by the resume; nothing
        # before the failed part was sent again.
        self.assertEqual(calls, ["two", "three", "three", "four", "five"])
        self.platform_post.refresh_from_db()
        self.assertEqual(self.platform_post.thread_status, ThreadStatus.COMPLETE)
        # Part four replied to part three's resumed id, not a stale one.
        self.assertEqual(self._states()[2].status, PartStatus.PUBLISHED)

    def test_a_retryable_failure_retries_on_the_backoff_then_stops(self):
        self._add_parts("two")
        provider = self._provider(RateLimitError("slow down", platform="Mastodon"))

        _first, scheduled = self._start(provider)

        self.assertEqual(scheduled, THREAD_PART_RETRY_BACKOFF[:THREAD_PART_MAX_RETRIES])
        self.assertEqual(provider.publish_thread_part.call_count, THREAD_PART_MAX_RETRIES + 1)
        state = self._states()[0]
        self.assertEqual(state.status, PartStatus.FAILED)
        self.platform_post.refresh_from_db()
        self.assertEqual(self.platform_post.thread_status, ThreadStatus.PARTIAL)

    def test_an_ambiguous_write_is_held_back_never_resent(self):
        self._add_parts("two", "three")
        provider = self._provider(APIError("timeout", platform="Mastodon", ambiguous_write=True))

        self._start(provider)

        provider.publish_thread_part.assert_called_once()
        states = self._states()
        self.assertEqual(states[0].status, PartStatus.UNCONFIRMED)
        self.assertEqual(states[0].publish_error, THREAD_PART_AMBIGUOUS_MESSAGE)
        self.assertEqual(states[1].status, PartStatus.PENDING)
        self.platform_post.refresh_from_db()
        self.assertEqual(self.platform_post.thread_status, ThreadStatus.PARTIAL)

    def test_an_unclassified_provider_error_counts_as_ambiguous(self):
        self._add_parts("two")
        provider = self._provider(KeyError("id"))

        self._start(provider)

        provider.publish_thread_part.assert_called_once()
        self.assertEqual(self._states()[0].status, PartStatus.UNCONFIRMED)

    def test_a_duplicate_task_for_a_claimed_part_does_nothing(self):
        (part,) = self._add_parts("two")
        state = PlatformPostPart.objects.create(platform_post=self.platform_post, part=part, status=PartStatus.POSTING)
        provider = self._provider(lambda *a, **k: PublishResult(platform_post_id="x"))

        self._drain(provider, queue=[(str(self.platform_post.id), str(part.id))])

        provider.publish_thread_part.assert_not_called()
        state.refresh_from_db()
        self.assertEqual(state.status, PartStatus.POSTING)

    def test_a_part_whose_earlier_part_is_not_live_is_skipped(self):
        parts = self._add_parts("two", "three")
        provider = self._provider(lambda *a, **k: PublishResult(platform_post_id="x"))

        self._drain(provider, queue=[(str(self.platform_post.id), str(parts[1].id))])

        provider.publish_thread_part.assert_not_called()

    def test_a_thread_starts_only_once(self):
        self._add_parts("two")
        with patch("apps.publisher.engine._publish_thread_part_task") as task:
            PublishEngine()._maybe_start_thread(self.platform_post)
            self.platform_post.refresh_from_db()
            PublishEngine()._maybe_start_thread(self.platform_post)
        task.assert_called_once()
        self.assertEqual(self.platform_post.thread_status, ThreadStatus.PENDING)

    def test_a_stalled_part_is_held_back_by_the_sweep(self):
        (part,) = self._add_parts("two")
        state = PlatformPostPart.objects.create(platform_post=self.platform_post, part=part, status=PartStatus.POSTING)
        PlatformPostPart.objects.filter(pk=state.pk).update(updated_at=timezone.now() - timezone.timedelta(hours=1))

        settled = PublishEngine().reap_stalled_thread_parts()

        self.assertEqual(settled, 1)
        state.refresh_from_db()
        self.assertEqual(state.status, PartStatus.UNCONFIRMED)
        self.platform_post.refresh_from_db()
        self.assertEqual(self.platform_post.thread_status, ThreadStatus.PARTIAL)

    def test_resume_of_an_unconfirmed_part_needs_confirmation(self):
        (part,) = self._add_parts("Part two")
        PlatformPost.objects.filter(pk=self.platform_post.pk).update(thread_status=ThreadStatus.PARTIAL)
        self.platform_post.refresh_from_db()
        PlatformPostPart.objects.create(platform_post=self.platform_post, part=part, status=PartStatus.UNCONFIRMED)
        with patch("apps.publisher.engine._publish_thread_part_task") as task:
            with self.assertRaisesMessage(ValueError, "may already be live"):
                resume_thread(self.platform_post)
            task.assert_not_called()
            resume_thread(self.platform_post, confirm_unconfirmed=True)
        task.assert_called_once_with(str(self.platform_post.id), str(part.id), schedule=0)
        self.assertEqual(self._states()[0].status, PartStatus.PENDING)

    def test_resume_refuses_a_thread_that_is_not_stopped(self):
        self._add_parts("two")
        with self.assertRaises(ValueError):
            resume_thread(self.platform_post)

    def test_per_account_caption_override_is_what_publishes(self):
        (part,) = self._add_parts("two")
        PlatformPostPart.objects.create(platform_post=self.platform_post, part=part, platform_specific_caption="mine")
        provider = self._provider(lambda *a, **k: PublishResult(platform_post_id="x"))

        self._start(provider)

        self.assertEqual(provider.publish_thread_part.call_args.args[1].text, "mine")


class ThreadPublishGateTest(ThreadTestMixin, TestCase):
    platform = "tiktok"

    def test_a_thread_on_a_platform_without_threads_fails_before_the_first_post(self):
        self._add_parts("two")
        self.platform_post.status = PlatformPost.Status.PUBLISHING
        self.platform_post.platform_post_id = ""
        self.platform_post.save()

        with patch.object(PublishEngine, "_dispatch_to_provider") as dispatch:
            result = PublishEngine()._publish_platform_post(self.platform_post)

        dispatch.assert_not_called()
        self.assertFalse(result["success"])
        self.platform_post.refresh_from_db()
        self.assertEqual(self.platform_post.status, PlatformPost.Status.FAILED)
        self.assertIn("can't publish a thread", self.platform_post.publish_error)
