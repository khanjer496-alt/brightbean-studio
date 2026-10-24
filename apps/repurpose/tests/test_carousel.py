"""The carousel PDF: valid, one page per slide, brand colours, Arabic, and a logo that can't be read."""

from __future__ import annotations

import io
import re

from pypdf import PdfReader

from apps.repurpose import carousel
from apps.workspaces.brand_kit import ResolvedBrandKit

SLIDES = [
    {"title": "Roast slowly", "body": "Eighteen minutes at lower heat keeps the sweetness."},
    {"title": "Taste the difference", "body": "Caramel, not smoke."},
    {"title": "Try a bag", "body": "Link in the post."},
]


def _brand(colors=None):
    return ResolvedBrandKit(colors=colors if colors is not None else [{"name": "Main", "hex": "#0b6bcb"}])


def test_one_square_page_per_slide():
    data = carousel.render_carousel_pdf(SLIDES, brand=_brand(), title="Roast", author="Cafe")
    assert data.startswith(b"%PDF-")
    reader = PdfReader(io.BytesIO(data))
    assert len(reader.pages) == 3
    assert reader.pages[0].mediabox.width == reader.pages[0].mediabox.height
    assert "Roast slowly" in reader.pages[0].extract_text()
    assert reader.metadata.title == "Roast"


def test_brand_colour_is_the_page_background():
    data = carousel.render_carousel_pdf(SLIDES[:1], brand=_brand([{"name": "x", "hex": "#ff0000"}]), title="t")
    content = PdfReader(io.BytesIO(data)).pages[0].get_contents().get_data().decode("latin-1")
    assert re.search(r"1(\.0+)? 0(\.0+)? 0(\.0+)? rg", content)


def test_arabic_text_is_shaped_and_embedded():
    slides = [{"title": "تحميص بطيء", "body": "ثماني عشرة دقيقة على حرارة منخفضة تحافظ على الحلاوة."}]
    data = carousel.render_carousel_pdf(slides, brand=_brand(), title="قهوة")
    reader = PdfReader(io.BytesIO(data))
    assert len(reader.pages) == 1
    fonts = {str(f.get("/BaseFont")) for f in reader.pages[0]["/Resources"]["/Font"].values() for f in [f.get_object()]}
    assert any("NotoSansArabic" in name for name in fonts)


def test_mixed_script_and_emoji_do_not_crash():
    slides = [{"title": "Coffee قهوة ☕", "body": "Mixed 中文 characters are left out, never boxes."}]
    assert carousel.render_carousel_pdf(slides, brand=_brand(), title="t").startswith(b"%PDF-")


def test_no_brand_colours_uses_the_default_and_a_missing_logo_is_fine():
    data = carousel.render_carousel_pdf(SLIDES, brand=_brand([]), title="t")
    assert len(PdfReader(io.BytesIO(data)).pages) == 3


def test_unreadable_logo_does_not_stop_the_carousel(db):
    class Broken:
        file = type("F", (), {"open": staticmethod(lambda mode: (_ for _ in ()).throw(OSError("gone")))})()

    brand = _brand()
    brand.logos = {"primary": Broken()}
    assert carousel.render_carousel_pdf(SLIDES, brand=brand, title="t").startswith(b"%PDF-")


def test_long_text_still_fits_on_its_page():
    slides = [{"title": "T " * 40, "body": "word " * 60}]
    data = carousel.render_carousel_pdf(slides, brand=_brand(), title="t")
    assert len(PdfReader(io.BytesIO(data)).pages) == 1
