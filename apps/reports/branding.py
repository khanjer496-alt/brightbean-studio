"""The agency's look on a report: logo, accent colour and the product credit."""

from __future__ import annotations

import base64
import hashlib
import io
import logging

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

DEFAULT_ACCENT = "#0b6bcb"
MIN_CONTRAST = 3.0  # WCAG minimum for graphical objects and large text, against white
LOGO_MAX_BYTES = 1024 * 1024
LOGO_BOX = (240, 96)
LOGO_MAX_PIXELS = 25_000_000
LOGO_CACHE_SECONDS = 600
LOGO_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})


def _luminance(hex_color: str) -> float:
    channels = [int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast_on_white(hex_color: str) -> float:
    return 1.05 / (_luminance(hex_color) + 0.05)


def readable_accent(color: str) -> str:
    """``color`` darkened until it reads on white (charts, headings, links).

    A pale brand colour stays recognisable but never ends up as a line nobody can see.
    """
    value = (color or "").strip()
    if len(value) != 7 or not value.startswith("#"):
        return DEFAULT_ACCENT
    try:
        int(value[1:], 16)
    except ValueError:
        return DEFAULT_ACCENT
    value = value.lower()
    for _ in range(40):
        if contrast_on_white(value) >= MIN_CONTRAST:
            return value
        value = "#" + "".join(f"{int(int(value[i : i + 2], 16) * 0.92):02x}" for i in (1, 3, 5))
    return DEFAULT_ACCENT


def text_on(color: str) -> str:
    """Black or white, whichever reads better on ``color``."""
    return "#ffffff" if contrast_on_white(color) >= 4.5 else "#111827"


def fetch_logo(url: str) -> str:
    """The organization's logo as a small PNG data URI, or ``""`` if it can't be had.

    Fetched once when a report is built and stored in the snapshot, so a report
    never depends on the logo host staying up, and the PDF can embed it. The
    fetch goes through the SSRF-safe reader the RSS feeds use.
    """
    if not url:
        return ""
    key = "report-logo:" + hashlib.sha256(url.encode()).hexdigest()
    cached = cache.get(key)
    if cached is not None:
        return cached
    logo = _download_logo(url)
    cache.set(key, logo, LOGO_CACHE_SECONDS)
    return logo


def _download_logo(url: str) -> str:
    try:
        from PIL import Image

        from apps.composer.views import _safe_fetch_feed

        response, _ = _safe_fetch_feed(
            url,
            {"Accept": "image/*", "User-Agent": f"{settings.BRAND_SHORT_NAME} Report Builder/1.0"},
            max_bytes=LOGO_MAX_BYTES,
            content_types=LOGO_TYPES,
        )
        if response is None or response.status_code >= 400:
            return ""
        image = Image.open(io.BytesIO(response.content))
        # A small file can still decode to an enormous bitmap; a logo never needs one.
        if image.width * image.height > LOGO_MAX_PIXELS:
            return ""
        image.thumbnail(LOGO_BOX)
        out = io.BytesIO()
        image.convert("RGBA").save(out, format="PNG", optimize=True)
        return "data:image/png;base64," + base64.b64encode(out.getvalue()).decode()
    except Exception:
        logger.info("Could not fetch the report logo", exc_info=True)
        return ""


def logo_from_asset(asset) -> str:
    """A media library image as a small PNG data URI (see :func:`fetch_logo`), or ``""``."""
    try:
        from PIL import Image

        with asset.file.open("rb") as handle:
            image = Image.open(io.BytesIO(handle.read(LOGO_MAX_BYTES * 8)))
        if image.width * image.height > LOGO_MAX_PIXELS:
            return ""
        image.thumbnail(LOGO_BOX)
        out = io.BytesIO()
        image.convert("RGBA").save(out, format="PNG", optimize=True)
        return "data:image/png;base64," + base64.b64encode(out.getvalue()).decode()
    except Exception:
        logger.info("Could not read the brand kit logo", exc_info=True)
        return ""


def branding_for(definition) -> dict:
    from apps.workspaces import brand_kit

    workspace = definition.workspace
    org = workspace.organization
    kit = brand_kit.resolve(workspace)
    # Reports sit on white paper: the logo meant for light backgrounds, else the primary one.
    asset = kit.logo("on_light", "primary")
    logo = logo_from_asset(asset) if asset else ""
    accent = readable_accent(definition.accent_color or kit.primary_color or workspace.primary_color or DEFAULT_ACCENT)
    return {
        "agency": org.name,
        "logo": logo or fetch_logo(org.logo_url),
        "accent": accent,
        "accent_text": text_on(accent),
        "white_label": definition.white_label,
        # Shown in the footer unless the report is white-labelled.
        "product": "" if definition.white_label else settings.BRAND_NAME,
    }
