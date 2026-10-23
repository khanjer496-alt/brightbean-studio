"""Thread parts: the data model, the platform checks, and the composer.

A post's own caption and media are its first part, so a post with no
``PostPart`` rows is a single post exactly as before threads existed. Later
parts are checked per account against the platform's ``ThreadCapability``
before anything is scheduled, so a thread the platform can't carry never
publishes its first part alone.
"""

import json
from datetime import timedelta
from unittest.mock import patch

from django.urls import reverse
from django.utils import timezone

from apps.approvals.policy import publish_fingerprint
from apps.composer.models import PlatformPost, PlatformPostPart, Post, PostPart, PostPartMedia
from apps.composer.services import (
    ThreadPartSpec,
    clone_post,
    create_post,
    parse_thread_parts,
    platform_post_thread_blocker,
    set_post_parts,
    spec_thread_parts,
    thread_blocker,
    thread_change_blocker,
    transition_platform_post,
)
from apps.media_library.models import MediaAsset
from apps.social_accounts.models import SocialAccount

from .test_unsplash import ComposerTestCase

MB = 1024 * 1024


class _ThreadMixin:
    def _account(self, platform):
        return SocialAccount.objects.create(
            workspace=self.workspace,
            platform=platform,
            account_platform_id=f"{platform}-thread",
            account_name=f"{platform} account",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )

    def _asset(self, media_type="image", **kwargs):
        defaults = dict(width=1080, height=1080, duration=0, file_size=MB)
        defaults.update(kwargs)
        return MediaAsset.objects.create(
            organization=self.org,
            workspace=self.workspace,
            file=f"test/{media_type}-{MediaAsset.objects.count()}",
            filename=f"{media_type}.{'mp4' if media_type == 'video' else 'jpg'}",
            media_type=media_type,
            mime_type="video/mp4" if media_type == "video" else "image/jpeg",
            **defaults,
        )

    def _blocker(self, platform, *parts):
        return thread_blocker(SocialAccount(platform=platform), list(parts))


class ThreadModelTest(_ThreadMixin, ComposerTestCase):
    def test_a_post_without_parts_is_a_single_post(self):
        post = Post.objects.create(workspace=self.workspace, caption="one")
        pp = PlatformPost.objects.create(post=post, social_account=self._account("bluesky"))
        self.assertEqual(post.thread_part_count, 1)
        self.assertFalse(post.is_thread)
        self.assertEqual(pp.thread_status, PlatformPost.ThreadStatus.NONE)

    def test_single_post_fingerprint_is_unchanged_by_the_thread_feature(self):
        """Approvals recorded before threads existed must stay valid."""
        post = Post.objects.create(workspace=self.workspace, caption="one")
        pp = PlatformPost.objects.create(post=post, social_account=self._account("bluesky"))
        before = publish_fingerprint(pp)
        set_post_parts(post, [ThreadPartSpec(caption="two")])
        self.assertNotEqual(publish_fingerprint(pp), before)
        set_post_parts(post, [])
        self.assertEqual(publish_fingerprint(pp), before)

    def test_set_post_parts_orders_the_finisher_last(self):
        post = Post.objects.create(workspace=self.workspace, caption="one")
        set_post_parts(
            post,
            [
                ThreadPartSpec(caption="Follow for more", is_finisher=True),
                ThreadPartSpec(caption="two"),
                ThreadPartSpec(caption="three", delay_seconds=60),
            ],
        )
        parts = list(post.thread_parts.all())
        self.assertEqual([p.caption for p in parts], ["two", "three", "Follow for more"])
        self.assertEqual([p.position for p in parts], [1, 2, 3])
        self.assertEqual(parts[1].delay_seconds, 60)
        self.assertEqual(post.thread_part_count, 4)

    def test_two_finishers_are_refused(self):
        post = Post.objects.create(workspace=self.workspace, caption="one")
        with self.assertRaises(ValueError):
            set_post_parts(post, [ThreadPartSpec(is_finisher=True), ThreadPartSpec(is_finisher=True)])

    def test_overrides_are_stored_only_for_the_posts_accounts(self):
        post = Post.objects.create(workspace=self.workspace, caption="one")
        bluesky = self._account("bluesky")
        other = self._account("mastodon")
        pp = PlatformPost.objects.create(post=post, social_account=bluesky)
        set_post_parts(
            post,
            [ThreadPartSpec(caption="two", caption_overrides={str(bluesky.id): "bsky two", str(other.id): "x"})],
        )
        (state,) = PlatformPostPart.objects.all()
        self.assertEqual(state.platform_post, pp)
        self.assertEqual(state.effective_caption, "bsky two")

    def test_part_media_must_be_in_the_workspace(self):
        post = Post.objects.create(workspace=self.workspace, caption="one")
        with self.assertRaises(ValueError):
            set_post_parts(post, [ThreadPartSpec(media_asset_ids=["00000000-0000-0000-0000-000000000000"])])

    def test_a_thread_that_started_publishing_cannot_be_replaced(self):
        post = Post.objects.create(workspace=self.workspace, caption="one")
        pp = PlatformPost.objects.create(post=post, social_account=self._account("bluesky"))
        set_post_parts(post, [ThreadPartSpec(caption="two")])
        PlatformPostPart.objects.create(
            platform_post=pp, part=post.thread_parts.get(), status=PlatformPostPart.Status.PUBLISHED
        )
        with self.assertRaises(ValueError):
            set_post_parts(post, [ThreadPartSpec(caption="changed")])

    def test_a_thread_whose_first_post_is_live_cannot_be_replaced_during_the_first_delay(self):
        # The first post is live and part 2 is queued an hour out, so no part
        # row exists yet: replacing the parts would orphan that queued task.
        post = Post.objects.create(workspace=self.workspace, caption="one")
        PlatformPost.objects.create(
            post=post,
            social_account=self._account("bluesky"),
            status="published",
            thread_status=PlatformPost.ThreadStatus.PENDING,
        )
        with self.assertRaisesMessage(ValueError, "already started publishing"):
            set_post_parts(post, [ThreadPartSpec(caption="changed")])

    def test_a_partial_thread_reads_partially_published_without_changing_status(self):
        post = Post.objects.create(workspace=self.workspace, caption="one")
        pp = PlatformPost.objects.create(
            post=post,
            social_account=self._account("bluesky"),
            status="published",
            thread_status=PlatformPost.ThreadStatus.PARTIAL,
        )
        self.assertEqual(pp.status, "published")
        self.assertEqual(pp.display_status, "partially_published")
        self.assertEqual(pp.display_status_label, "Partially published")
        self.assertEqual(post.status, "published")
        self.assertEqual(post.display_status_label, "Partially published")
        pp.thread_status = PlatformPost.ThreadStatus.COMPLETE
        self.assertEqual(pp.display_status, "published")

    def test_clone_copies_parts_media_and_overrides(self):
        post = Post.objects.create(workspace=self.workspace, caption="one")
        bluesky = self._account("bluesky")
        PlatformPost.objects.create(post=post, social_account=bluesky)
        image = self._asset()
        set_post_parts(
            post,
            [ThreadPartSpec(caption="two", media_asset_ids=[image.id], caption_overrides={str(bluesky.id): "b"})],
        )
        copy = clone_post(post)
        (part,) = copy.thread_parts.all()
        self.assertEqual(part.caption, "two")
        self.assertEqual(list(part.media_attachments.values_list("media_asset_id", flat=True)), [image.id])
        self.assertEqual(PlatformPostPart.objects.get(part=part).platform_specific_caption, "b")

    def test_parse_refuses_a_delay_over_a_day(self):
        with self.assertRaises(ValueError):
            parse_thread_parts([{"caption": "x", "delay_seconds": 86401}])
        with self.assertRaises(ValueError):
            parse_thread_parts({"caption": "x"})


class ThreadBlockerTest(_ThreadMixin, ComposerTestCase):
    def test_platforms_without_threads_refuse_them(self):
        for platform in ("tiktok", "pinterest", "google_business", "devto"):
            with self.subTest(platform=platform):
                self.assertIn("can't publish a thread", self._blocker(platform, ("two", [])))

    def test_no_parts_is_never_blocked(self):
        self.assertEqual(self._blocker("tiktok"), "")

    def test_each_part_is_held_to_the_platforms_per_part_limit(self):
        self.assertEqual(self._blocker("bluesky", ("x" * 300, [])), "")
        self.assertIn(
            "Part 2 is 301 characters; Bluesky takes at most 300 per post", self._blocker("bluesky", ("x" * 301, []))
        )
        self.assertIn("500 per post", self._blocker("threads", ("ok", []), ("x" * 501, [])))
        self.assertIn("Part 3", self._blocker("threads", ("ok", []), ("x" * 501, [])))
        # A comment has its own limit, shorter than the post's on LinkedIn.
        self.assertIn("1250 per comment", self._blocker("linkedin_company", ("x" * 1251, [])))

    def test_linkedin_counts_escaped_characters(self):
        self.assertIn("per comment", self._blocker("linkedin_company", ("(" * 700, [])))

    def test_too_many_parts(self):
        parts = [("x", [])] * 25
        self.assertIn("at most 25 parts", self._blocker("mastodon", *parts))
        self.assertIn("at most 10 parts", self._blocker("facebook", *[("x", [])] * 10))

    def test_an_empty_part_is_refused(self):
        self.assertIn("Part 2 of the thread is empty", self._blocker("mastodon", ("  ", [])))

    def test_comments_cannot_carry_media(self):
        image = self._asset()
        self.assertIn("can't carry media", self._blocker("facebook", ("two", [image])))

    def test_reply_chain_part_media_follows_the_media_rules(self):
        long_video = self._asset("video", width=1080, height=1920, duration=10 * 60)
        reason = self._blocker("bluesky", ("two", [long_video]))
        self.assertTrue(reason.startswith("Part 2: "), reason)
        self.assertEqual(self._blocker("bluesky", ("two", [self._asset(file_size=MB // 2)])), "")

    def test_comments_need_an_account_that_can_comment(self):
        with patch.object(SocialAccount, "supports_first_comment", return_value=False):
            self.assertIn("can't post comments", self._blocker("linkedin_personal", ("two", [])))

    def test_a_comments_thread_refuses_a_first_comment(self):
        # Both would be comments on the first post, queued on different
        # delays, so the first comment could land mid-thread.
        account = SocialAccount(platform="facebook")
        reason = thread_blocker(account, [("two", [])], first_comment="Link in bio")
        self.assertIn("can't also post a first comment", reason)
        self.assertEqual(thread_blocker(account, [("two", [])], first_comment="  "), "")
        # A reply chain keeps its first comment.
        self.assertEqual(thread_blocker(SocialAccount(platform="bluesky"), [("two", [])], first_comment="hi"), "")

    def test_spec_parts_use_the_accounts_override(self):
        account = self._account("bluesky")
        specs = [ThreadPartSpec(caption="x" * 400, caption_overrides={str(account.id): "short"})]
        self.assertEqual(spec_thread_parts(account, specs, {}), [("short", [])])


class ThreadSchedulingTest(_ThreadMixin, ComposerTestCase):
    def setUp(self):
        super().setUp()
        self.when = timezone.now() + timedelta(days=1)

    def test_create_post_refuses_a_scheduled_thread_the_platform_cant_carry(self):
        with self.assertRaisesMessage(ValueError, "can't publish a thread"):
            create_post(
                workspace=self.workspace,
                social_account=self._account("google_business"),
                caption="x",
                scheduled_at=self.when,
                status="scheduled",
                parts=[ThreadPartSpec(caption="two")],
            )
        self.assertFalse(Post.objects.exists())

    def test_create_post_refuses_a_scheduled_comments_thread_with_a_first_comment(self):
        with self.assertRaisesMessage(ValueError, "first comment"):
            create_post(
                workspace=self.workspace,
                social_account=self._account("facebook"),
                caption="x",
                first_comment="Link below",
                scheduled_at=self.when,
                status="scheduled",
                parts=[ThreadPartSpec(caption="two")],
            )

    def test_adding_a_first_comment_to_a_scheduled_comments_thread_is_refused(self):
        post = create_post(
            workspace=self.workspace,
            social_account=self._account("facebook"),
            caption="x",
            scheduled_at=self.when,
            status="scheduled",
            parts=[ThreadPartSpec(caption="two")],
        )
        self.assertEqual(thread_change_blocker(post), "")
        self.assertIn("first comment", thread_change_blocker(post, first_comment="Link below"))
        # The publisher refuses it too, before the first post goes out.
        pp = post.platform_posts.get()
        Post.objects.filter(pk=post.pk).update(first_comment="Link below")
        pp.refresh_from_db()
        self.assertIn("first comment", platform_post_thread_blocker(pp))

    def test_create_post_keeps_a_draft_thread_for_later(self):
        post = create_post(
            workspace=self.workspace,
            social_account=self._account("tiktok"),
            caption="x",
            parts=[ThreadPartSpec(caption="two")],
        )
        self.assertEqual(post.thread_part_count, 2)

    def test_scheduling_a_draft_rechecks_the_thread(self):
        post = create_post(
            workspace=self.workspace,
            social_account=self._account("bluesky"),
            caption="x",
            parts=[ThreadPartSpec(caption="y" * 301)],
        )
        with self.assertRaisesMessage(ValueError, "301 characters"):
            transition_platform_post(post.platform_posts.get(), "scheduled", scheduled_at=self.when)

    def test_a_reply_chain_thread_with_part_media_schedules(self):
        image = self._asset()
        post = create_post(
            workspace=self.workspace,
            social_account=self._account("mastodon"),
            caption="x",
            scheduled_at=self.when,
            status="scheduled",
            parts=[ThreadPartSpec(caption="two", media_asset_ids=[image.id], delay_seconds=300)],
        )
        part = post.thread_parts.get()
        self.assertEqual(part.delay_seconds, 300)
        self.assertTrue(PostPartMedia.objects.filter(part=part, media_asset=image).exists())


class ComposerThreadTest(_ThreadMixin, ComposerTestCase):
    def setUp(self):
        super().setUp()
        self.bluesky = self._account("bluesky")
        self.google = self._account("google_business")
        self.day = (timezone.localtime(timezone.now()) + timedelta(days=2)).date()

    def _save(self, action, parts, *accounts, post=None, extra=None):
        if post is None:
            url = reverse("composer:save_post", kwargs={"workspace_id": self.workspace.id})
        else:
            url = reverse("composer:save_post_edit", kwargs={"workspace_id": self.workspace.id, "post_id": post.id})
        data = {
            "action": action,
            "caption": "Part one",
            "tags": "",
            "selected_accounts": ",".join(str(a.id) for a in accounts),
            "scheduled_date": self.day.isoformat(),
            "scheduled_time": "09:00",
            **(extra or {}),
        }
        if parts is not None:
            data["thread_parts"] = json.dumps(parts)
        return self.client.post(url, data, HTTP_HX_REQUEST="true")

    def test_save_draft_stores_the_parts_and_overrides(self):
        response = self._save(
            "save_draft",
            [
                {"caption": "Part two", "delay_seconds": 60, "caption_overrides": {str(self.bluesky.id): "bsky"}},
                {"caption": "Follow for more", "is_finisher": True},
            ],
            self.bluesky,
        )
        self.assertEqual(response.status_code, 204, response.content)
        post = Post.objects.get()
        parts = list(post.thread_parts.all())
        self.assertEqual([p.caption for p in parts], ["Part two", "Follow for more"])
        self.assertTrue(parts[1].is_finisher)
        self.assertEqual(parts[0].delay_seconds, 60)
        self.assertEqual(PlatformPostPart.objects.get().platform_specific_caption, "bsky")

    def test_scheduling_refuses_a_part_over_the_platform_limit(self):
        response = self._save("schedule", [{"caption": "x" * 301}], self.bluesky)
        self.assertEqual(response.status_code, 400)
        self.assertIn("Bluesky takes at most 300", response.json()["errors"]["thread"])
        self.assertFalse(Post.objects.exists())

    def test_scheduling_refuses_a_platform_without_threads(self):
        response = self._save("schedule", [{"caption": "two"}], self.google)
        self.assertEqual(response.status_code, 400)
        self.assertIn("Google Business Profile can't publish a thread", response.json()["errors"]["thread"])

    def test_a_draft_is_never_blocked_by_its_thread(self):
        response = self._save("save_draft", [{"caption": "x" * 301}], self.bluesky)
        self.assertEqual(response.status_code, 204)

    def test_a_save_without_the_thread_editor_leaves_the_parts_alone(self):
        post = Post.objects.create(workspace=self.workspace, author=self.user, caption="Part one")
        PlatformPost.objects.create(post=post, social_account=self.bluesky)
        set_post_parts(post, [ThreadPartSpec(caption="keep me")])
        self.assertEqual(self._save("save_draft", None, self.bluesky, post=post).status_code, 204)
        self.assertEqual(list(post.thread_parts.values_list("caption", flat=True)), ["keep me"])

    def test_editing_an_approved_posts_thread_sends_it_back_for_review(self):
        post = Post.objects.create(workspace=self.workspace, author=self.user, caption="Part one")
        PlatformPost.objects.create(post=post, social_account=self.bluesky, status="approved")
        set_post_parts(post, [ThreadPartSpec(caption="two")])
        self._save("save_draft", [{"caption": "changed"}], self.bluesky, post=post)
        self.assertEqual(post.platform_posts.get().status, "pending_review")

    def test_compose_renders_the_parts_and_each_accounts_thread_capability(self):
        post = Post.objects.create(workspace=self.workspace, author=self.user, caption="Part one")
        PlatformPost.objects.create(post=post, social_account=self.bluesky)
        set_post_parts(post, [ThreadPartSpec(caption="Saved part")])
        url = reverse("composer:compose_edit", kwargs={"workspace_id": self.workspace.id, "post_id": post.id})
        html = self.client.get(url).content.decode()
        self.assertIn("Saved part", html)
        self.assertIn('name="thread_parts"', html)
        self.assertIn('"thread_mode": "reply_chain"', html)
        self.assertIn("Add to thread", html)

    def test_preview_shows_the_thread_per_network(self):
        facebook = self._account("facebook")
        response = self.client.post(
            reverse("composer:preview", kwargs={"workspace_id": self.workspace.id}),
            {
                "caption": "Part one",
                "selected_accounts": f"{self.bluesky.id},{facebook.id},{self.google.id}",
                "thread_parts": json.dumps([{"caption": "Part two"}]),
            },
        )
        html = response.content.decode()
        self.assertIn("Thread of 2 posts", html)  # Bluesky reply chain
        self.assertIn("Then 1 comment on the post", html)  # Facebook comments
        self.assertIn("can&#x27;t publish a thread", html)
        self.assertIn("Part two", html)

    def test_partial_thread_banner_and_resume(self):
        post = Post.objects.create(workspace=self.workspace, author=self.user, caption="Part one")
        pp = PlatformPost.objects.create(
            post=post,
            social_account=self.bluesky,
            status="published",
            platform_post_id="at://x",
            thread_status=PlatformPost.ThreadStatus.PARTIAL,
            thread_error="The thread stopped at part 2.",
        )
        part = PostPart.objects.create(post=post, position=1, caption="two")
        PlatformPostPart.objects.create(platform_post=pp, part=part, status=PlatformPostPart.Status.FAILED)
        html = self.client.get(
            reverse("composer:compose_edit", kwargs={"workspace_id": self.workspace.id, "post_id": post.id})
        ).content.decode()
        self.assertIn("Thread partially published", html)
        self.assertIn("Resume thread", html)

        url = reverse(
            "composer:resume_thread",
            kwargs={"workspace_id": self.workspace.id, "post_id": post.id, "platform_post_id": pp.id},
        )
        with patch("apps.publisher.engine._publish_thread_part_task") as task:
            response = self.client.post(url, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 204)
        task.assert_called_once_with(str(pp.id), str(part.id), schedule=0)
        pp.refresh_from_db()
        self.assertEqual(pp.thread_status, PlatformPost.ThreadStatus.PENDING)

    def test_resuming_an_unconfirmed_part_needs_the_authors_check(self):
        post = Post.objects.create(workspace=self.workspace, author=self.user, caption="Part one")
        pp = PlatformPost.objects.create(
            post=post,
            social_account=self.bluesky,
            status="published",
            platform_post_id="at://x",
            thread_status=PlatformPost.ThreadStatus.PARTIAL,
        )
        part = PostPart.objects.create(post=post, position=1, caption="two")
        PlatformPostPart.objects.create(platform_post=pp, part=part, status=PlatformPostPart.Status.UNCONFIRMED)
        html = self.client.get(
            reverse("composer:compose_edit", kwargs={"workspace_id": self.workspace.id, "post_id": post.id})
        ).content.decode()
        self.assertIn("I checked it isn't on Bluesky", html)

        url = reverse(
            "composer:resume_thread",
            kwargs={"workspace_id": self.workspace.id, "post_id": post.id, "platform_post_id": pp.id},
        )
        with patch("apps.publisher.engine._publish_thread_part_task") as task:
            refused = self.client.post(url, HTTP_HX_REQUEST="true")
            self.assertEqual(refused.status_code, 400)
            task.assert_not_called()
            confirmed = self.client.post(url, {"confirm_unconfirmed": "1"}, HTTP_HX_REQUEST="true")
        self.assertEqual(confirmed.status_code, 204)
        task.assert_called_once_with(str(pp.id), str(part.id), schedule=0)

    def test_thread_media_picker_lists_images_and_videos(self):
        self._asset("image")
        self._asset("video")
        response = self.client.get(reverse("composer:thread_media_picker", kwargs={"workspace_id": self.workspace.id}))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode().count("thread-media-selected"), 2)
