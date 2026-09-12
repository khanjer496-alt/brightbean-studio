from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from apps.composer.models import ContentCategory, PlatformPost, Post
from apps.composer.tests import test_csv_upload_size
from apps.social_accounts.models import SocialAccount


class CSVMediaRejectionTests(TestCase):
    def setUp(self):
        test_csv_upload_size.CSVUploadSizeCapTests.setUp(self)

    def put_session(self, headers, rows, mapping=None):
        session = self.client.session
        session[f"csv_import_{self.workspace.id}"] = {"headers": headers, "rows": rows}
        if mapping is not None:
            session[f"csv_mapping_{self.workspace.id}"] = mapping
        session.save()

    def endpoint(self, name):
        return reverse(f"composer:{name}", kwargs={"workspace_id": self.workspace.id})

    def assert_media_rejected(self, response):
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "CSV media URLs are not supported yet")
        self.assertContains(response, "Media Library")
        self.assertEqual(response.context["valid_count"], 0)
        self.assertFalse(Post.objects.filter(workspace=self.workspace).exists())
        self.assertNotIn(f"csv_mapping_{self.workspace.id}", self.client.session)

    def test_populated_media_column_rejected_at_upload_without_fetch_or_stale_mapping(self):
        self.put_session(["caption"], [["old import"]], {"caption": 0})
        for header in ("media_url", "Image URL", "video_urls", "media_url_1"):
            with self.subTest(header=header), patch("apps.composer.views.httpx.get") as fetch:
                upload = SimpleUploadedFile("media.csv", f"caption,{header}\nHello,http://127.0.0.1/private\n".encode())
                response = self.client.post(self.url, {"csv_file": upload})
                self.assert_media_rejected(response)
                fetch.assert_not_called()

    def test_empty_media_columns_allow_text_import_but_are_not_advertised_as_mappable(self):
        upload = SimpleUploadedFile("text.csv", b"caption,media_url\nHello,\n")
        response = self.client.post(self.url, {"csv_file": upload})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("media_url", response.context["field_choices"])
        self.assertNotContains(response, 'name="map_media_url"')
        preview = self.client.post(self.endpoint("csv_preview"), {"map_caption": "0"})
        self.assertEqual(preview.context["valid_count"], 1)
        imported = self.client.post(self.endpoint("csv_confirm_import"))
        self.assertEqual(imported.context["created_count"], 1)
        self.assertEqual(Post.objects.get(workspace=self.workspace).caption, "Hello")

    def test_forged_media_mapping_rejected_even_for_unrecognized_header(self):
        self.put_session(["caption", "custom"], [["Hello", "http://127.0.0.1/private"]])
        response = self.client.post(self.endpoint("csv_preview"), {"map_caption": "0", "map_media_url": "1"})
        self.assert_media_rejected(response)

    def test_confirm_rejects_legacy_mapping_and_unmapped_media_columns(self):
        for header, mapping in (("custom", {"caption": 0, "media_url": 1}), ("image_url", {"caption": 0})):
            with self.subTest(header=header):
                self.put_session(["caption", header], [["Hello", "http://127.0.0.1/private"]], mapping)
                response = self.client.post(self.endpoint("csv_confirm_import"))
                self.assert_media_rejected(response)

    def test_row_failure_does_not_leave_a_partial_post_or_category(self):
        SocialAccount.objects.create(
            workspace=self.workspace,
            platform="facebook",
            account_platform_id="fake-csv-account",
            account_name="Test",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        self.put_session(
            ["caption", "platform", "category"],
            [["Hello", "facebook", "CSV category"]],
            {"caption": 0, "platforms": 1, "category": 2},
        )
        with patch.object(PlatformPost.objects, "get_or_create", side_effect=RuntimeError("test child failure")):
            response = self.client.post(self.endpoint("csv_confirm_import"))
        self.assertEqual(response.context["error_count"], 1)
        self.assertEqual(response.context["created_count"], 0)
        self.assertFalse(Post.objects.filter(workspace=self.workspace).exists())
        self.assertFalse(ContentCategory.objects.filter(workspace=self.workspace, name="CSV category").exists())
