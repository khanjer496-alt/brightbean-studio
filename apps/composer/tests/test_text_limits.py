"""A caption over a platform's limits is refused where it is scheduled and again where it is published.

The composer's counter warns, but the API, the MCP tools, the calendar and the publisher have to
hold the same line, in the platform's own unit (``SocialProvider.text_length_unit``). Nothing is
cut to fit.
"""

from django.utils import timezone

from apps.composer.models import PlatformPost, Post
from apps.composer.services import create_post, require_publishable, text_blocker
from apps.publisher.engine import PublishEngine
from apps.social_accounts.models import SocialAccount

from .test_unsplash import ComposerTestCase

FAMILY = "👨‍👩‍👧‍👦"


class TextBlockerTest(ComposerTestCase):
    def _account(self, platform):
        return SocialAccount.objects.create(
            workspace=self.workspace,
            platform=platform,
            account_platform_id=f"{platform}-text",
            account_name=platform,
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )

    def test_bluesky_counts_graphemes_so_300_families_fit(self):
        account = self._account("bluesky")
        # 100 families are 100 graphemes (700 code points); 300 of them fit the 300 limit but are 7,500 bytes
        # against the lexicon's 3,000-byte cap.
        self.assertEqual(text_blocker(account, FAMILY * 100), "")
        self.assertEqual(text_blocker(account, "日" * 300), "")
        self.assertIn("bytes", text_blocker(account, FAMILY * 300))
        self.assertIn("301", text_blocker(account, "a" * 301))

    def test_mastodon_counts_a_link_as_23(self):
        account = self._account("mastodon")
        link = "https://example.com/" + "p" * 200
        self.assertEqual(text_blocker(account, "a" * 476 + " " + link), "")
        self.assertIn("501", text_blocker(account, "a" * 477 + " " + link))

    def test_threads_refuses_a_sixth_link_and_counts_emoji_bytes(self):
        account = self._account("threads")
        links = " ".join(f"https://example.com/{n}" for n in range(6))
        self.assertIn("6 different links", text_blocker(account, links))
        self.assertEqual(text_blocker(account, links.rsplit(" ", 1)[0]), "")
        self.assertIn("504", text_blocker(account, "😀" * 126))

    def test_instagram_refuses_31_hashtags(self):
        account = self._account("instagram")
        self.assertEqual(text_blocker(account, " ".join(f"#tag{n}" for n in range(30))), "")
        self.assertIn("31 hashtags", text_blocker(account, " ".join(f"#tag{n}" for n in range(31))))

    def test_links_still_to_be_shortened_count_at_their_short_length(self):
        account = self._account("bluesky")
        self.assertEqual(text_blocker(account, "x" * 400, counting=lambda text: text[:100]), "")


class SchedulingRefusesTest(ComposerTestCase):
    def setUp(self):
        super().setUp()
        self.account = SocialAccount.objects.create(
            workspace=self.workspace,
            platform="bluesky",
            account_platform_id="bsky-1",
            account_name="Bluesky",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )

    def test_a_scheduled_post_over_the_limit_is_refused_with_the_platform_named(self):
        with self.assertRaisesMessage(ValueError, "Bluesky takes at most 300"):
            create_post(
                workspace=self.workspace,
                social_account=self.account,
                caption="a" * 301,
                status="scheduled",
                scheduled_at=timezone.now() + timezone.timedelta(days=1),
            )

    def test_the_platform_override_is_the_caption_that_is_checked(self):
        post = create_post(
            workspace=self.workspace,
            social_account=self.account,
            caption="a" * 301,
            platform_overrides={self.account.id: {"caption": "short"}},
            status="scheduled",
            scheduled_at=timezone.now() + timezone.timedelta(days=1),
        )
        self.assertEqual(post.platform_posts.get().effective_caption, "short")

    def test_a_draft_may_be_over_the_limit(self):
        post = create_post(workspace=self.workspace, social_account=self.account, caption="a" * 301)
        self.assertEqual(post.platform_posts.get().status, "draft")

    def test_scheduling_a_saved_draft_is_refused(self):
        post = Post.objects.create(workspace=self.workspace, caption="a" * 301)
        platform_post = PlatformPost.objects.create(post=post, social_account=self.account)
        with self.assertRaisesMessage(ValueError, "301 characters"):
            require_publishable(platform_post)

    def test_the_publisher_fails_the_row_permanently_instead_of_cutting_it(self):
        post = Post.objects.create(workspace=self.workspace, caption="a" * 301)
        platform_post = PlatformPost.objects.create(
            post=post,
            social_account=self.account,
            status=PlatformPost.Status.PUBLISHING,
            scheduled_at=timezone.now(),
        )
        result = PublishEngine()._publish_platform_post(platform_post)
        self.assertFalse(result["success"])
        self.assertIn("takes at most 300", result["error"])
        platform_post.refresh_from_db()
        self.assertEqual(platform_post.status, PlatformPost.Status.FAILED)
