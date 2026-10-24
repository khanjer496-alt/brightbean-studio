"""Publish-time derived copies of library images.

A network can refuse a file the library accepted: Instagram takes JPEG only,
Bluesky takes images up to 1 MB. Rather than block the post, the publisher
makes a converted copy for that one network. The library's original is never
modified, and the copy lives under its own storage prefix so deleting the
asset removes it too (:func:`delete_derived`).
"""

from __future__ import annotations

import contextlib
import logging
import os
import tempfile
from dataclasses import dataclass

from django.core.files.base import File
from django.core.files.storage import default_storage

from .services import open_image

logger = logging.getLogger(__name__)

DERIVED_PREFIX = "media_library/derived"

_FORMAT_BY_MIME = {
    "image/jpeg": "jpeg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
}

# JPEG quality steps tried in order, then the longest side shrinks by
# ``_SHRINK_FACTOR`` per round until the file fits or gets too small to be worth sending.
_QUALITY_STEPS = (92, 85, 75, 65, 55)
_SHRINK_FACTOR = 0.85
_MIN_SIDE = 320


class CannotFitImageError(Exception):
    """An image could not be brought under the byte limit without shrinking it past usefulness."""


@dataclass(frozen=True)
class DerivedImage:
    """A converted copy of one asset: where it is stored, where it is on local disk, and its size."""

    storage_name: str
    local_path: str
    width: int
    height: int


def image_format(asset) -> str:
    """The Pillow-style format name of a still ``asset``, from the mime type the library sniffed."""
    return _FORMAT_BY_MIME.get((asset.mime_type or "").lower(), "")


def derivation_needed(asset, rules) -> bool:
    """Whether publishing ``asset`` under ``rules`` needs a converted copy (wrong format, or too many bytes)."""
    if rules is None or asset.media_type != "image":
        return False
    fmt = image_format(asset)
    if rules.image_formats is not None and fmt and fmt not in rules.image_formats:
        return True
    return bool(
        rules.compress_images and rules.max_image_bytes is not None and (asset.file_size or 0) > rules.max_image_bytes
    )


def derive_image(asset, rules, source_path: str) -> DerivedImage:
    """Write the JPEG copy of the image at ``source_path`` that ``rules`` accept, and store it.

    The copy is a JPEG (alpha flattened onto white). It is recompressed, and
    shrunk if it has to be, only when ``rules.compress_images`` sets a byte
    limit; otherwise it is saved once at high quality. Raises
    ``CannotFitImageError`` when no size above ``_MIN_SIDE`` fits the limit.
    """
    max_bytes = rules.max_image_bytes if rules.compress_images else None
    fd, local_path = tempfile.mkstemp(suffix=".jpg", prefix="pd_derived_")
    os.close(fd)
    try:
        width, height = _write_jpeg(source_path, local_path, max_bytes)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(local_path)
        raise
    return DerivedImage(_store(asset, local_path, max_bytes), local_path, width, height)


def _write_jpeg(source_path: str, dest_path: str, max_bytes: int | None) -> tuple[int, int]:
    from PIL import Image, ImageOps

    with open_image(source_path) as src:
        # Phones store the rotation as an EXIF flag; baking it in keeps the picture upright once the metadata goes.
        img = ImageOps.exif_transpose(src)
        if img.mode in ("RGBA", "LA", "P"):
            rgba = img.convert("RGBA")
            flat = Image.new("RGB", rgba.size, (255, 255, 255))
            flat.paste(rgba, mask=rgba.getchannel("A"))
            img = flat
        elif img.mode != "RGB":
            img = img.convert("RGB")

        while True:
            for quality in _QUALITY_STEPS if max_bytes else _QUALITY_STEPS[:1]:
                img.save(dest_path, format="JPEG", quality=quality, optimize=True)
                if max_bytes is None or os.path.getsize(dest_path) <= max_bytes:
                    return img.size
            width, height = img.size
            if min(width, height) * _SHRINK_FACTOR < _MIN_SIDE:
                raise CannotFitImageError(f"Cannot fit a {width}x{height} image under {max_bytes} bytes")
            img = img.resize((int(width * _SHRINK_FACTOR), int(height * _SHRINK_FACTOR)), Image.LANCZOS)


def _store(asset, local_path: str, max_bytes: int | None) -> str:
    """Save the copy under the asset's derived prefix, reusing a copy an earlier publish stored."""
    name = f"{DERIVED_PREFIX}/{asset.id}/jpeg-{max_bytes or 'full'}.jpg"
    if not default_storage.exists(name):
        with open(local_path, "rb") as f:
            name = default_storage.save(name, File(f))
    return name


def delete_derived(asset) -> None:
    """Best-effort removal of every derived copy of ``asset`` from storage."""
    prefix = f"{DERIVED_PREFIX}/{asset.id}"
    try:
        _dirs, files = default_storage.listdir(prefix)
    except (FileNotFoundError, NotImplementedError, OSError):
        return
    for name in files:
        try:
            default_storage.delete(f"{prefix}/{name}")
        except Exception:
            logger.warning("Could not delete derived media %s/%s", prefix, name, exc_info=True)
