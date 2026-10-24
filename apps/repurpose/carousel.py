"""A branded PDF carousel from slide texts, drawn with fpdf2.

The same setup as client reports (``apps.reports.pdf``): pure Python, bundled
Noto Sans with Noto Sans Arabic and Noto Emoji as fallbacks, HarfBuzz shaping
and bidi ordering so an Arabic slide reads as written. The brand kit supplies
the colours and logo; its font names can't be embedded (the kit stores names,
not files), so the text is always set in Noto.

Slides are square pages: a title, a body, the page number and the logo.
"""

from __future__ import annotations

import io
import logging

from fpdf import FPDF

from apps.reports import pdf as report_pdf
from apps.reports.branding import DEFAULT_ACCENT, text_on

logger = logging.getLogger(__name__)

PAGE_MM = 200
MARGIN = 18
LOGO_HEIGHT_MM = 12
TITLE_PT = 30
BODY_PT = 18
LINE_FACTOR = 0.55  # mm of line height per point of font size


class _Deck(FPDF):
    def __init__(self, title: str, author: str):
        super().__init__(unit="mm", format=(PAGE_MM, PAGE_MM))
        for family, style, name in report_pdf.FONT_FILES:
            self.add_font(family, style, report_pdf.FONT_DIR / name)
        self.set_fallback_fonts(list(report_pdf.FALLBACK_FONTS), exact_match=False)
        self.set_text_shaping(True)
        self.set_margins(MARGIN, MARGIN, MARGIN)
        self.set_auto_page_break(auto=False)
        self.set_title(report_pdf.clean(title))
        self.set_author(report_pdf.clean(author))

    # fpdf2 switches fonts inside a shaped run without telling the page state;
    # re-selecting the font each call keeps the next run from drawing in the
    # fallback (see ``apps.reports.pdf._Report``).
    def cell(self, *args, **kwargs):
        self.current_font_is_set_on_page = False
        return super().cell(*args, **kwargs)

    def multi_cell(self, *args, **kwargs):
        self.current_font_is_set_on_page = False
        return super().multi_cell(*args, **kwargs)


def _text_height(pdf: _Deck, text: str, size: int, style: str, width: float) -> float:
    pdf.set_font(report_pdf.FONT, style, size)
    return pdf.multi_cell(width, size * LINE_FACTOR, text, align="L", dry_run=True, output="HEIGHT")


def _logo_bytes(logo) -> bytes | None:
    """The logo file as PNG bytes (fpdf2 reads PNG and JPEG), or None when it can't be had."""
    if logo is None or not logo.file:
        return None
    try:
        from PIL import Image

        with logo.file.open("rb") as handle:
            image = Image.open(handle)
            image.thumbnail((480, 192))
            out = io.BytesIO()
            image.convert("RGBA").save(out, format="PNG")
        return out.getvalue()
    except Exception:  # noqa: BLE001 - a broken or missing logo must never stop the carousel
        logger.info("Carousel logo could not be read", exc_info=True)
        return None


def _rgb(hex_color: str) -> tuple[int, int, int]:
    return report_pdf._rgb(hex_color)


def render_carousel_pdf(slides: list[dict], *, brand, title: str, author: str = "") -> bytes:
    """PDF bytes for ``slides`` (``[{"title", "body"}]``) in the brand kit's colours and logo."""
    colors = [c.get("hex", "") for c in brand.colors if _valid_hex(c.get("hex", ""))]
    background = colors[0] if colors else DEFAULT_ACCENT
    ink = text_on(background)
    accent = colors[1] if len(colors) > 1 else ink
    logo = _logo_bytes(brand.logo("on_dark", "primary", "mark"))

    pdf = _Deck(title, author)
    total = len(slides)
    width = PAGE_MM - 2 * MARGIN
    for number, slide in enumerate(slides, start=1):
        pdf.add_page()
        pdf.set_fill_color(*_rgb(background))
        pdf.rect(0, 0, PAGE_MM, PAGE_MM, style="F")
        if logo:
            pdf.image(io.BytesIO(logo), x=MARGIN, y=MARGIN, h=LOGO_HEIGHT_MM)
        heading = report_pdf.clean(slide.get("title", ""))
        body = report_pdf.clean(slide.get("body", ""))

        block = 0.0
        if heading:
            block += _text_height(pdf, heading, TITLE_PT, "B", width)
        if body:
            block += _text_height(pdf, body, BODY_PT, "", width)
        if heading and body:
            block += 8
        top = max(MARGIN + LOGO_HEIGHT_MM + 6, (PAGE_MM - block) / 2)
        pdf.set_y(top)
        pdf.set_x(MARGIN)
        if heading:
            pdf.set_text_color(*_rgb(ink))
            pdf.set_font(report_pdf.FONT, "B", TITLE_PT)
            pdf.multi_cell(width, TITLE_PT * LINE_FACTOR, heading, align=report_pdf._align(heading))
            pdf.ln(8)
            pdf.set_x(MARGIN)
        if body:
            pdf.set_text_color(*_rgb(ink))
            pdf.set_font(report_pdf.FONT, "", BODY_PT)
            pdf.multi_cell(width, BODY_PT * LINE_FACTOR, body, align=report_pdf._align(body))

        pdf.set_y(PAGE_MM - MARGIN)
        pdf.set_x(MARGIN)
        pdf.set_text_color(*_rgb(accent if accent != background else ink))
        pdf.set_font(report_pdf.FONT, "", 10)
        pdf.cell(width, 6, f"{number} / {total}", align="R")
    return bytes(pdf.output())


def _valid_hex(value: str) -> bool:
    return len(value) == 7 and value.startswith("#") and all(c in "0123456789abcdefABCDEF" for c in value[1:])
