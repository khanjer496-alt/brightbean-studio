"""Publish-time converted copies of images: originals are untouched, limits are met."""

import os
import uuid

import pytest
from django.core.files.storage import default_storage
from PIL import Image

from apps.media_library.derivatives import (
    CannotFitImageError,
    delete_derived,
    derivation_needed,
    derive_image,
    image_format,
)
from apps.media_library.models import MediaAsset
from providers.types import MediaRules

JPEG_ONLY = MediaRules(image_formats=frozenset({"jpeg"}))
ONE_MB = MediaRules(max_image_bytes=1_000_000, compress_images=True)


@pytest.fixture(autouse=True)
def tmp_media(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path / "media")
    settings.MEDIA_URL = "/media/"


def _asset(mime="image/png", media_type="image", size=1000):
    return MediaAsset(id=uuid.uuid4(), media_type=media_type, mime_type=mime, file_size=size, filename="a")


def _noise_png(path, side=1600):
    img = Image.effect_noise((side, side), 90).convert("RGB")
    img.save(path, format="PNG")
    return path


def test_image_format_comes_from_the_sniffed_mime():
    assert image_format(_asset("image/webp")) == "webp"
    assert image_format(_asset("application/x")) == ""


def test_conversion_needed_only_for_a_format_the_rules_refuse():
    assert derivation_needed(_asset("image/png"), JPEG_ONLY)
    assert derivation_needed(_asset("image/webp"), JPEG_ONLY)
    assert not derivation_needed(_asset("image/jpeg"), JPEG_ONLY)
    # Animated GIFs would lose their frames, videos aren't images, no rules means no change.
    assert not derivation_needed(_asset("image/gif", media_type="gif"), JPEG_ONLY)
    assert not derivation_needed(_asset("video/mp4", media_type="video"), JPEG_ONLY)
    assert not derivation_needed(_asset("image/png"), None)
    assert not derivation_needed(_asset("image/png"), MediaRules())


def test_oversize_needs_compression_only_when_the_rule_opts_in():
    assert derivation_needed(_asset("image/jpeg", size=2_000_000), ONE_MB)
    assert not derivation_needed(_asset("image/jpeg", size=900_000), ONE_MB)
    assert not derivation_needed(_asset("image/jpeg", size=2_000_000), MediaRules(max_image_bytes=1_000_000))


def test_png_with_alpha_becomes_a_jpeg_and_the_source_is_untouched(tmp_path):
    src = tmp_path / "in.png"
    Image.new("RGBA", (40, 20), (255, 0, 0, 0)).save(src)
    before = src.read_bytes()
    asset = _asset()

    derived = derive_image(asset, JPEG_ONLY, str(src))
    try:
        with Image.open(derived.local_path) as out:
            assert out.format == "JPEG"
            assert out.size == (40, 20)
            # Fully transparent pixels are flattened onto white, not black.
            assert out.getpixel((1, 1))[0] > 240 and out.getpixel((1, 1))[2] > 240
        assert (derived.width, derived.height) == (40, 20)
        assert default_storage.exists(derived.storage_name)
        assert derived.storage_name.startswith(f"media_library/derived/{asset.id}/")
        assert src.read_bytes() == before
    finally:
        os.unlink(derived.local_path)


def test_webp_converts(tmp_path):
    src = tmp_path / "in.webp"
    Image.new("RGB", (30, 30), "blue").save(src, format="WEBP")
    derived = derive_image(_asset("image/webp"), JPEG_ONLY, str(src))
    try:
        with Image.open(derived.local_path) as out:
            assert out.format == "JPEG"
    finally:
        os.unlink(derived.local_path)


def test_oversize_is_recompressed_under_the_limit(tmp_path):
    src = _noise_png(tmp_path / "big.png")
    assert src.stat().st_size > 1_000_000
    derived = derive_image(_asset(size=src.stat().st_size), ONE_MB, str(src))
    try:
        assert os.path.getsize(derived.local_path) <= 1_000_000
        assert derived.width > 0 and derived.height > 0
    finally:
        os.unlink(derived.local_path)


def test_an_image_that_cannot_fit_raises(tmp_path):
    src = _noise_png(tmp_path / "big.png", side=900)
    rules = MediaRules(max_image_bytes=500, compress_images=True)
    with pytest.raises(CannotFitImageError):
        derive_image(_asset(), rules, str(src))


def test_the_stored_copy_is_reused_and_removed_with_the_asset(tmp_path):
    src = tmp_path / "in.png"
    Image.new("RGB", (10, 10), "red").save(src)
    asset = _asset()
    first = derive_image(asset, JPEG_ONLY, str(src))
    second = derive_image(asset, JPEG_ONLY, str(src))
    os.unlink(first.local_path)
    os.unlink(second.local_path)
    assert first.storage_name == second.storage_name

    delete_derived(asset)
    assert not default_storage.exists(first.storage_name)
    # Nothing stored is fine too.
    delete_derived(_asset())
