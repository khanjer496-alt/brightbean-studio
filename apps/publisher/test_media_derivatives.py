"""Alt text, converted copies and the URL preflight, as the engine hands them to a provider."""

import os
from unittest.mock import MagicMock, patch

import pytest
from django.core.files.storage import default_storage
from PIL import Image

from apps.publisher.test_media_handling import _attachment, _dispatch_mocks
from providers.exceptions import PublishError
from providers.types import PostType


@pytest.fixture(autouse=True)
def tmp_media(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path / "media")
    settings.MEDIA_URL = "/media/"
    settings.APP_URL = "https://app.example.com"


def _png(tmp_path, name="a.png", size=(60, 30)):
    path = tmp_path / name
    Image.new("RGB", size, "green").save(path, format="PNG")
    return str(path)


def _image_attachment(asset_id, *, mime, alt="", post_alt="", width=60, height=30, size=500, filename="a.png"):
    pm = _attachment(asset_id, filename=filename)
    pm.alt_text = post_alt
    asset = pm.media_asset
    asset.mime_type = mime
    asset.alt_text = alt
    asset.width = width
    asset.height = height
    asset.file_size = size
    return pm


def _dispatch(platform, attachments, *, local, tmp_path, source=None):
    engine, platform_post, provider = _dispatch_mocks(
        platform, needs_local_media=local, attachments=attachments, supported=(PostType.TEXT, PostType.IMAGE)
    )
    provider.fetches_media_by_url = False
    with (
        patch("apps.publisher.engine.get_provider", return_value=provider),
        patch("apps.publisher.engine._resolve_publish_credentials", return_value={}),
        patch("apps.publisher.engine._SharedMediaCache.path_for", return_value=source),
        patch("apps.publisher.engine.download_to_path"),
    ):
        engine._dispatch_to_provider(platform_post)
    return provider.publish_post.call_args.args[1]


def test_alt_text_prefers_the_posts_own_over_the_library_assets(tmp_path):
    content = _dispatch(
        "mastodon",
        [
            _image_attachment("a1", mime="image/jpeg", alt="library alt", post_alt="post alt"),
            _image_attachment("a2", mime="image/jpeg", alt="library only"),
            _image_attachment("a3", mime="image/jpeg"),
        ],
        local=False,
        tmp_path=tmp_path,
    )
    assert content.media_alt == ["post alt", "library only", ""]
    assert content.media_dimensions == [(60, 30)] * 3
    assert content.alt_text(0) == "post alt"
    assert content.alt_text(7) == ""


def test_a_png_is_converted_to_a_stored_jpeg_for_instagram(tmp_path):
    source = _png(tmp_path)
    content = _dispatch(
        "instagram",
        [_image_attachment("a1", mime="image/png", width=0, height=0)],
        local=False,
        tmp_path=tmp_path,
        source=source,
    )
    url = content.media_urls[0]
    assert url.startswith("https://app.example.com/media/media_library/derived/")
    assert url.endswith(".jpg")
    assert content.media_dimensions == [(60, 30)]
    name = url.removeprefix("https://app.example.com/media/")
    assert default_storage.exists(name)
    with Image.open(default_storage.path(name)) as stored:
        assert stored.format == "JPEG"
    # The original PNG was only read.
    with Image.open(source) as original:
        assert original.format == "PNG"


def test_a_jpeg_is_left_alone_for_instagram(tmp_path):
    content = _dispatch(
        "instagram",
        [_image_attachment("a1", mime="image/jpeg", filename="a.jpg")],
        local=False,
        tmp_path=tmp_path,
        source=None,
    )
    assert content.media_urls == ["https://cdn.example/a.jpg"]


def test_a_local_provider_gets_the_derived_file_and_it_is_cleaned_up(tmp_path):
    source = _png(tmp_path, size=(2000, 2000))
    seen = {}

    engine, platform_post, provider = _dispatch_mocks(
        "bluesky",
        needs_local_media=True,
        attachments=[_image_attachment("a1", mime="image/png", size=3_000_000)],
        supported=(PostType.TEXT, PostType.IMAGE),
    )
    provider.fetches_media_by_url = False

    def capture(token, content):
        seen["path"] = content.media_files[0]
        seen["exists"] = os.path.exists(content.media_files[0])
        seen["dims"] = content.media_dimensions[0]
        return provider.publish_post.return_value

    provider.publish_post.side_effect = capture
    with (
        patch("apps.publisher.engine.get_provider", return_value=provider),
        patch("apps.publisher.engine._resolve_publish_credentials", return_value={}),
        patch("apps.publisher.engine._SharedMediaCache.path_for", return_value=source),
    ):
        engine._dispatch_to_provider(platform_post)

    assert seen["exists"] and seen["path"] != source and seen["path"].endswith(".jpg")
    assert seen["dims"] == (2000, 2000)
    assert not os.path.exists(seen["path"])
    assert os.path.exists(source)


def test_an_image_that_cannot_be_shrunk_fails_the_post_with_a_sentence(tmp_path):
    source = _png(tmp_path, size=(400, 400))
    with (
        patch("apps.media_library.derivatives._QUALITY_STEPS", (90,)),
        patch("apps.media_library.derivatives.os.path.getsize", return_value=10_000_000),
        pytest.raises(PublishError) as caught,
    ):
        _dispatch(
            "bluesky",
            [_image_attachment("a1", mime="image/png", size=3_000_000, filename="huge.png")],
            local=True,
            tmp_path=tmp_path,
            source=source,
        )
    assert "huge.png" in str(caught.value)
    assert caught.value.retryable is False


def test_the_preflight_runs_only_for_providers_that_fetch_by_url(tmp_path, settings):
    settings.PUBLISH_MEDIA_PREFLIGHT = True
    engine, platform_post, provider = _dispatch_mocks(
        "instagram",
        needs_local_media=False,
        attachments=[_image_attachment("a1", mime="image/jpeg", filename="a.jpg")],
        supported=(PostType.TEXT, PostType.IMAGE),
    )
    provider.fetches_media_by_url = True
    with (
        patch("apps.publisher.engine.get_provider", return_value=provider),
        patch("apps.publisher.engine._resolve_publish_credentials", return_value={}),
        patch(
            "apps.publisher.engine.check_public_media_urls",
            side_effect=PublishError("not public", platform="Instagram", retryable=False),
        ) as check,
        pytest.raises(PublishError, match="not public"),
    ):
        engine._dispatch_to_provider(platform_post)
    check.assert_called_once()
    assert check.call_args.args[0] == ["https://cdn.example/a.jpg"]
    provider.publish_post.assert_not_called()

    provider.fetches_media_by_url = False
    with (
        patch("apps.publisher.engine.get_provider", return_value=provider),
        patch("apps.publisher.engine._resolve_publish_credentials", return_value={}),
        patch("apps.publisher.engine.check_public_media_urls") as check,
    ):
        engine._dispatch_to_provider(platform_post)
    check.assert_not_called()


def test_a_thread_part_carries_alt_and_a_recompressed_copy(tmp_path):
    from apps.publisher.engine import _SharedMediaCache, _thread_part_content

    source = _png(tmp_path, size=(1500, 1500))
    asset = _image_attachment(
        "a1", mime="image/png", alt="part alt", size=3_000_000, width=1500, height=1500
    ).media_asset
    platform_post = MagicMock()
    platform_post.social_account.platform = "bluesky"
    platform_post.post.tags = []
    platform_post.platform_extra = {}
    provider = MagicMock()
    provider.thread.media_per_part = True
    provider.supported_post_types = [PostType.TEXT, PostType.IMAGE]
    provider.needs_local_media = True
    cache = _SharedMediaCache()
    part = MagicMock()
    try:
        with patch.object(_SharedMediaCache, "path_for", return_value=source):
            content = _thread_part_content(platform_post, provider, part, "caption", [asset], cache)
        assert content.media_alt == ["part alt"]
        assert content.media_files[0] != source and content.media_files[0].endswith(".jpg")
        derived = content.media_files[0]
        assert os.path.exists(derived)
    finally:
        cache.cleanup()
    assert not os.path.exists(derived)
    assert os.path.exists(source)


def test_deleting_an_asset_removes_its_derived_copies(tmp_path):
    import uuid

    from django.core.files.base import ContentFile

    from apps.media_library.derivatives import DERIVED_PREFIX
    from apps.media_library.services import delete_asset

    asset_id = uuid.uuid4()
    name = default_storage.save(f"{DERIVED_PREFIX}/{asset_id}/jpeg-full.jpg", ContentFile(b"x"))
    asset = MagicMock()
    asset.id = asset_id
    asset.versions.all.return_value = []
    with patch("apps.media_library.services._check_post_references", return_value=[]):
        delete_asset(asset)
    assert not default_storage.exists(name)
