"""Platform media rules: what each provider declares and how the composer uses it.

Every provider states its platform's limits on ``SocialProvider.media_rules``;
``media_rule_issues`` checks a post's media against the rules for the post
type the publisher would choose. Errors stop the post being scheduled —
in the composer, the service layer the API and MCP use, and the calendar —
while warnings are only shown.
"""

from datetime import timedelta

from django.contrib.sessions.middleware import SessionMiddleware
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.composer.models import PlatformPost, Post, PostMedia
from apps.composer.services import create_post, media_rule_issues, transition_platform_post
from apps.composer.views import preview
from apps.media_library.models import MediaAsset
from apps.social_accounts.models import SocialAccount
from providers import PROVIDER_REGISTRY, media_rules_for
from providers.types import PostType

from .test_unsplash import ComposerTestCase

MB = 1024 * 1024


def _asset(media_type="image", *, width=1080, height=1080, duration=0, size=MB, name=None):
    """An unsaved MediaAsset carrying just the metadata the rules read."""
    return MediaAsset(
        file=f"test/{name or media_type}",
        filename=name or f"{media_type}.{'mp4' if media_type == 'video' else 'jpg'}",
        media_type=media_type,
        width=width,
        height=height,
        duration=duration,
        file_size=size,
    )


def _video(**kwargs):
    kwargs.setdefault("width", 1080)
    kwargs.setdefault("height", 1920)
    kwargs.setdefault("duration", 30)
    return _asset("video", **kwargs)


def _issues(platform, assets, **extra):
    account = SocialAccount(platform=platform)
    return media_rule_issues(account, assets, platform_extra=extra or None)


def _errors(platform, assets, **extra):
    return [i.message for i in _issues(platform, assets, **extra) if i.is_error]


def _warnings(platform, assets, **extra):
    return [i.message for i in _issues(platform, assets, **extra) if not i.is_error]


class ProviderDeclarationsTest(TestCase):
    def test_every_rule_cites_its_source(self):
        for platform, provider_cls in PROVIDER_REGISTRY.items():
            for post_type, rules in provider_cls.media_rules.items():
                with self.subTest(platform=platform, post_type=post_type):
                    self.assertTrue(rules.source.startswith("https://"), rules)

    def test_rules_are_only_declared_for_post_types_the_provider_supports(self):
        for platform, provider_cls in PROVIDER_REGISTRY.items():
            supported = set(provider_cls().supported_post_types)
            with self.subTest(platform=platform):
                self.assertLessEqual(set(provider_cls.media_rules), supported)

    def test_unknown_platform_or_post_type_has_no_rules(self):
        self.assertIsNone(media_rules_for("myspace", PostType.IMAGE))
        self.assertIsNone(media_rules_for("devto", PostType.IMAGE))


class InstagramRulesTest(TestCase):
    def test_feed_image_aspect_ratio(self):
        for platform in ("instagram", "instagram_login"):
            with self.subTest(platform=platform):
                self.assertEqual(_errors(platform, [_asset(width=1080, height=1350)]), [])  # 4:5
                self.assertEqual(_errors(platform, [_asset(width=1910, height=1000)]), [])  # 1.91:1
                (error,) = _errors(platform, [_asset(width=1080, height=1920)])  # 9:16
                self.assertIn("is 9:16", error)
                self.assertIn("between 4:5 and 1.91:1", error)

    def test_feed_image_size(self):
        self.assertIn("up to 8.0\xa0MB", _errors("instagram", [_asset(width=1080, height=1080, size=9 * MB)])[0])

    def test_carousel_takes_twenty_items(self):
        self.assertEqual(_errors("instagram", [_asset()] * 20), [])
        (error,) = _errors("instagram", [_asset()] * 21)
        self.assertIn("at most 20 items in a carousel", error)

    def test_carousel_shape_only_warns(self):
        assets = [_asset(), _asset(width=1080, height=1920)]
        self.assertEqual(_errors("instagram", assets), [])
        self.assertTrue(any("9:16" in w for w in _warnings("instagram", assets)))

    def test_reel_length(self):
        self.assertEqual(_errors("instagram", [_video(duration=30)]), [])
        self.assertIn("at least 3s in a Reel", _errors("instagram", [_video(duration=2)])[0])
        self.assertIn("at most 15 min in a Reel", _errors("instagram", [_video(duration=16 * 60)])[0])

    def test_landscape_reel_warns(self):
        self.assertEqual(_errors("instagram", [_video(width=1920, height=1080)]), [])
        self.assertIn("recommends 9:16", _warnings("instagram", [_video(width=1920, height=1080)])[0])

    def test_story_video_length(self):
        assets = [_video(duration=61)]
        self.assertIn("at most 60s in a Story", _errors("instagram", assets, post_type="story")[0])

    def test_gif_in_a_carousel_warns(self):
        assets = [_asset(), _asset("gif")]
        self.assertEqual(_errors("instagram", assets), [])
        self.assertIn("doesn't take GIF files in a carousel", _warnings("instagram", assets)[0])


class FacebookRulesTest(TestCase):
    def test_reel_length(self):
        reel = {"post_type": "reel"}
        self.assertEqual(_errors("facebook", [_video(duration=60)], **reel), [])
        self.assertIn("at most 90s in a Reel", _errors("facebook", [_video(duration=120)], **reel)[0])
        self.assertIn("at least 3s", _errors("facebook", [_video(duration=2)], **reel)[0])

    def test_reel_resolution_only_warns(self):
        small = [_video(width=360, height=640)]
        self.assertEqual(_errors("facebook", small, post_type="reel"), [])
        self.assertIn("at least 540x960px", _warnings("facebook", small, post_type="reel")[0])

    def test_a_regular_video_is_not_held_to_reel_limits(self):
        self.assertEqual(_issues("facebook", [_video(duration=600, width=1920, height=1080)]), [])

    def test_photo_post_takes_ten_photos(self):
        self.assertIn("at most 10 items", _errors("facebook", [_asset()] * 11)[0])

    def test_large_photo_only_warns(self):
        self.assertEqual(_errors("facebook", [_asset(size=6 * MB)]), [])
        self.assertTrue(_warnings("facebook", [_asset(size=6 * MB)]))


class TikTokRulesTest(TestCase):
    def test_length(self):
        self.assertEqual(_issues("tiktok", [_video(duration=60)]), [])
        self.assertIn("at most 10 min", _errors("tiktok", [_video(duration=11 * 60)])[0])
        # Under 3 seconds is not a firm refusal in TikTok's docs.
        self.assertEqual(_errors("tiktok", [_video(duration=2)]), [])
        self.assertIn("at least 3s", _warnings("tiktok", [_video(duration=2)])[0])

    def test_resolution(self):
        self.assertIn("at least 360x360px", _errors("tiktok", [_video(width=320, height=568)])[0])
        self.assertIn("at most 4096x4096px", _errors("tiktok", [_video(width=2160, height=7680)])[0])

    def test_photo_posts_take_up_to_35_images_and_no_video(self):
        """Photo mode: images only, at most 35."""
        self.assertEqual(_issues("tiktok", [_asset()]), [])
        self.assertIn("at most 35 items", _errors("tiktok", [_asset() for _ in range(36)])[0])
        self.assertIn("doesn't take", _errors("tiktok", [_asset(), _video()])[0])


class YouTubeRulesTest(TestCase):
    def test_long_video_warns(self):
        self.assertEqual(_errors("youtube", [_video(duration=20 * 60)]), [])
        self.assertIn("at most 15 min", _warnings("youtube", [_video(duration=20 * 60)])[0])

    def test_short_limits_warn(self):
        short = {"post_type": "short"}
        landscape = [_video(width=1920, height=1080, duration=200)]
        self.assertEqual(_errors("youtube", landscape, **short), [])
        warnings = _warnings("youtube", landscape, **short)
        self.assertTrue(any("no wider than 1:1" in w for w in warnings))
        self.assertTrue(any("at most 3 min in a Short" in w for w in warnings))
        self.assertEqual(_issues("youtube", [_video(duration=170)], **short), [])

    def test_only_one_video_is_published(self):
        self.assertIn("Only the first item", _warnings("youtube", [_video(), _video()])[0])


class LinkedInRulesTest(TestCase):
    def test_extra_images_warn_that_only_the_first_is_published(self):
        for platform in ("linkedin_personal", "linkedin_company"):
            with self.subTest(platform=platform):
                self.assertEqual(_errors(platform, [_asset()] * 3), [])
                self.assertIn("Only the first item is published", _warnings(platform, [_asset()] * 3)[0])

    def test_more_than_twenty_images_is_refused(self):
        self.assertIn("at most 20 items", _errors("linkedin_personal", [_asset()] * 21)[0])

    def test_video_length(self):
        self.assertIn("at least 3s", _errors("linkedin_company", [_video(duration=2)])[0])
        self.assertIn("at most 30 min", _errors("linkedin_company", [_video(duration=31 * 60)])[0])
        self.assertTrue(_warnings("linkedin_company", [_video(size=600 * MB)]))


class PinterestRulesTest(TestCase):
    def test_two_by_three_is_recommended(self):
        self.assertEqual(_issues("pinterest", [_asset(width=1000, height=1500)]), [])
        (warning,) = _warnings("pinterest", [_asset(width=1000, height=1000)])
        self.assertIn("recommends 2:3", warning)
        self.assertEqual(_errors("pinterest", [_asset(width=1000, height=1000)]), [])

    def test_video_length(self):
        self.assertIn("at least 4s", _errors("pinterest", [_video(width=1000, height=1500, duration=3)])[0])
        self.assertIn("at most 15 min", _errors("pinterest", [_video(width=1000, height=1500, duration=16 * 60)])[0])


class OtherPlatformRulesTest(TestCase):
    def test_threads(self):
        self.assertIn("at most 20 items", _errors("threads", [_asset()] * 21)[0])
        self.assertIn("at most 5 min", _errors("threads", [_video(duration=6 * 60)])[0])
        self.assertIn("up to 8.0\xa0MB", _errors("threads", [_asset(size=9 * MB)])[0])

    def test_bluesky(self):
        self.assertIn("up to 976.6\xa0KB", _errors("bluesky", [_asset(size=2 * MB)])[0])
        self.assertIn("Only the first 4 items", _warnings("bluesky", [_asset(size=500_000)] * 5)[0])
        self.assertIn("at most 3 min", _errors("bluesky", [_video(duration=200, size=50 * MB)])[0])

    def test_google_business(self):
        self.assertIn("at least 250x250px", _errors("google_business", [_asset(width=200, height=200)])[0])
        self.assertIn("up to 5.0\xa0MB", _errors("google_business", [_asset(size=6 * MB)])[0])

    def test_mastodon_limits_vary_by_server_so_only_warn(self):
        assets = [_asset(size=20 * MB)] * 5
        self.assertEqual(_errors("mastodon", assets), [])
        self.assertEqual(len(_warnings("mastodon", assets)), 1 + 5)

    def test_text_only_platforms_and_unmeasured_media_are_not_checked(self):
        self.assertEqual(_issues("devto", [_asset(size=50 * MB)]), [])
        # 0x0 / 0s: the library hasn't measured it yet, so nothing is claimed.
        self.assertEqual(_issues("instagram", [_asset(width=0, height=0)]), [])
        self.assertEqual(_issues("instagram", [_video(width=0, height=0, duration=0)]), [])

    def test_attachments_without_a_file_are_ignored(self):
        missing = _asset(width=1080, height=1920)
        missing.file = ""
        self.assertEqual(_issues("instagram", [missing]), [])


class _WorkspaceMixin:
    def _account(self, platform, workspace=None):
        return SocialAccount.objects.create(
            workspace=workspace or self.workspace,
            platform=platform,
            account_platform_id=f"{platform}-rules",
            account_name=f"{platform} account",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )

    def _saved_asset(self, media_type="image", **kwargs):
        defaults = dict(width=1080, height=1080, duration=0, file_size=MB)
        defaults.update(kwargs)
        return MediaAsset.objects.create(
            organization=self.org,
            workspace=self.workspace,
            file=f"test/{media_type}",
            filename=f"{media_type}.{'mp4' if media_type == 'video' else 'jpg'}",
            media_type=media_type,
            mime_type="video/mp4" if media_type == "video" else "image/jpeg",
            **defaults,
        )


class ServiceEnforcementTest(_WorkspaceMixin, TestCase):
    """create_post / transition_platform_post — the REST and MCP scheduling path."""

    def setUp(self):
        from apps.organizations.models import Organization
        from apps.workspaces.models import Workspace

        self.org = Organization.objects.create(name="Rules Org")
        self.workspace = Workspace.objects.create(organization=self.org, name="Rules WS")
        self.instagram = self._account("instagram")
        self.when = timezone.now() + timedelta(hours=1)

    def test_scheduling_media_that_breaks_a_rule_is_refused(self):
        tall = self._saved_asset(width=1080, height=1920)
        with self.assertRaisesMessage(ValueError, "between 4:5 and 1.91:1"):
            create_post(
                workspace=self.workspace,
                social_account=self.instagram,
                caption="x",
                media_asset_ids=[tall.id],
                scheduled_at=self.when,
                status="scheduled",
            )
        self.assertFalse(Post.objects.exists())

    def test_a_draft_is_not_held_to_the_rules(self):
        tall = self._saved_asset(width=1080, height=1920)
        post = create_post(
            workspace=self.workspace, social_account=self.instagram, caption="x", media_asset_ids=[tall.id]
        )
        with self.assertRaisesMessage(ValueError, "between 4:5 and 1.91:1"):
            transition_platform_post(post.platform_posts.get(), "scheduled", scheduled_at=self.when)

    def test_warnings_do_not_block(self):
        wide_reel = self._saved_asset("video", width=1920, height=1080, duration=30)
        post = create_post(
            workspace=self.workspace,
            social_account=self.instagram,
            caption="x",
            media_asset_ids=[wide_reel.id],
            scheduled_at=self.when,
            status="scheduled",
        )
        self.assertEqual(post.platform_posts.get().status, "scheduled")


class ComposerEnforcementTest(_WorkspaceMixin, ComposerTestCase):
    def setUp(self):
        super().setUp()
        self.instagram = self._account("instagram")
        self.linkedin = self._account("linkedin_personal")
        self.save_url = reverse("composer:save_post", kwargs={"workspace_id": self.workspace.id})
        self.day = (timezone.localtime(timezone.now()) + timedelta(days=2)).date()

    def _post_with(self, *assets):
        post = Post.objects.create(workspace=self.workspace, author=self.user, caption="media rules")
        for position, asset in enumerate(assets):
            PostMedia.objects.create(post=post, media_asset=asset, position=position)
        return post

    def _save(self, post, action, *accounts):
        url = reverse("composer:save_post_edit", kwargs={"workspace_id": self.workspace.id, "post_id": post.id})
        return self.client.post(
            url,
            {
                "action": action,
                "caption": "media rules",
                "tags": "",
                "selected_accounts": ",".join(str(a.id) for a in accounts),
                "scheduled_date": self.day.isoformat(),
                "scheduled_time": "09:00",
            },
            HTTP_HX_REQUEST="true",
        )

    def test_scheduling_is_refused_with_the_reason(self):
        post = self._post_with(self._saved_asset(width=1080, height=1920))

        response = self._save(post, "schedule", self.instagram, self.linkedin)

        self.assertEqual(response.status_code, 400)
        self.assertIn("between 4:5 and 1.91:1", response.json()["errors"]["media"])
        self.assertFalse(PlatformPost.objects.filter(post=post, status="scheduled").exists())

    def test_saving_a_draft_is_not_blocked(self):
        post = self._post_with(self._saved_asset(width=1080, height=1920))
        self.assertEqual(self._save(post, "save_draft", self.instagram).status_code, 204)

    def test_warnings_do_not_block_scheduling(self):
        post = self._post_with(*[self._saved_asset() for _ in range(3)])
        self.assertEqual(self._save(post, "schedule", self.linkedin).status_code, 204)

    def test_facebook_reel_choice_applies_reel_rules(self):
        facebook = self._account("facebook")
        post = self._post_with(self._saved_asset("video", width=1080, height=1920, duration=120))
        url = reverse("composer:save_post_edit", kwargs={"workspace_id": self.workspace.id, "post_id": post.id})
        data = {
            "action": "schedule",
            "caption": "media rules",
            "tags": "",
            "selected_accounts": str(facebook.id),
            "scheduled_date": self.day.isoformat(),
            "scheduled_time": "09:00",
        }

        self.assertEqual(self.client.post(url, data, HTTP_HX_REQUEST="true").status_code, 204)
        data[f"ps_panel_{facebook.id}"] = "1"
        data[f"ps_post_type_{facebook.id}"] = "reel"
        PlatformPost.objects.filter(post=post).update(status="draft")
        response = self.client.post(url, data, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 400)
        self.assertIn("at most 90s in a Reel", response.json()["errors"]["media"])

    def test_preview_lists_errors_and_warnings_per_account(self):
        bluesky = self._account("bluesky")
        post = self._post_with(self._saved_asset(width=1080, height=1920, file_size=2 * MB), self._saved_asset())
        request = RequestFactory().post(
            reverse("composer:preview", kwargs={"workspace_id": self.workspace.id}),
            {
                "caption": "x",
                "selected_accounts": f"{self.instagram.id},{bluesky.id}",
                "_autosave_post_id": str(post.id),
            },
        )
        request.user = self.user
        SessionMiddleware(lambda req: None).process_request(request)

        html = preview(request, self.workspace.id).content.decode()

        self.assertIn('id="composer-media-issues"', html)
        self.assertIn('hx-swap-oob="innerHTML"', html)
        # Bluesky refuses the 2 MB image; Instagram only warns about the carousel shape.
        self.assertIn("bluesky account", html)
        self.assertIn("Bluesky takes files up to", html)
        self.assertIn("Can't schedule", html)
        self.assertIn("instagram account", html)
        self.assertIn("Check media", html)

    def test_preview_without_media_has_no_issues(self):
        request = RequestFactory().post(
            reverse("composer:preview", kwargs={"workspace_id": self.workspace.id}),
            {"caption": "x", "selected_accounts": str(self.instagram.id)},
        )
        request.user = self.user
        SessionMiddleware(lambda req: None).process_request(request)

        html = preview(request, self.workspace.id).content.decode()

        self.assertIn('id="composer-media-issues"', html)
        self.assertNotIn("Check media", html)

    def test_compose_page_renders_an_empty_issue_slot(self):
        html = self.client.get(reverse("composer:compose", kwargs={"workspace_id": self.workspace.id})).content.decode()
        self.assertEqual(html.count('id="composer-media-issues"'), 1)
        self.assertNotIn("hx-swap-oob", html.split('id="composer-media-issues"')[1][:200])


class CropPresetTest(_WorkspaceMixin, ComposerTestCase):
    def test_image_editor_offers_crop_to_fit_presets(self):
        asset = self._saved_asset()
        html = self.client.get(
            reverse("media_library:asset_edit", kwargs={"workspace_id": self.workspace.id, "asset_id": asset.id})
        ).content.decode()
        self.assertIn("Crop to fit", html)
        for label in ("'1:1'", "'4:5'", "'9:16'", "'16:9'", "'2:3'"):
            self.assertIn(label, html)
        self.assertIn("setAspectRatio", html)


class PublishTimeEnforcementTest(_WorkspaceMixin, TestCase):
    """The publisher re-checks blocking media rules before it uploads anything."""

    def setUp(self):
        from apps.organizations.models import Organization
        from apps.workspaces.models import Workspace

        self.org = Organization.objects.create(name="Publish Rules Org")
        self.workspace = Workspace.objects.create(organization=self.org, name="Publish Rules WS")
        self.instagram = self._account("instagram")

    def _publishing_row(self, asset):
        post = Post.objects.create(workspace=self.workspace, caption="x", scheduled_at=timezone.now())
        PostMedia.objects.create(post=post, media_asset=asset, position=0)
        # Written straight to the table: the media broke the rule after it was
        # scheduled, or the row reached the publisher some other way.
        return PlatformPost.objects.create(
            post=post, social_account=self.instagram, status=PlatformPost.Status.PUBLISHING
        )

    def test_a_blocking_rule_fails_the_row_with_the_composer_message(self):
        from unittest.mock import patch

        from apps.publisher.engine import PublishEngine

        pp = self._publishing_row(self._saved_asset(width=1080, height=1920))
        with patch.object(PublishEngine, "_dispatch_to_provider") as dispatch:
            result = PublishEngine()._publish_platform_post(pp)

        self.assertFalse(result["success"])
        dispatch.assert_not_called()
        pp.refresh_from_db()
        self.assertEqual(pp.status, PlatformPost.Status.FAILED)
        self.assertIn("between 4:5 and 1.91:1", pp.publish_error)

    def test_a_warning_still_publishes(self):
        from unittest.mock import patch

        from apps.publisher.engine import PublishEngine

        pp = self._publishing_row(self._saved_asset("video", width=1920, height=1080, duration=30))
        success = {"success": True, "platform_post_id": "remote-1", "status_code": 200, "response": {}}
        with patch.object(PublishEngine, "_dispatch_to_provider", return_value=success) as dispatch:
            result = PublishEngine()._publish_platform_post(pp)

        self.assertTrue(result["success"])
        dispatch.assert_called_once()
