"""The report as a PDF, drawn from the stored snapshot with fpdf2.

fpdf2 is pure Python apart from the HarfBuzz shaping wheel, so it needs no
system libraries and no headless browser (the Docker image ships neither).
Charts are the same static SVGs the web page shows, embedded as vector graphics.

Text is set in the bundled Noto fonts (SIL OFL 1.1, ``apps/reports/fonts``):
Noto Sans for Latin, Greek and Cyrillic, Noto Sans Arabic and monochrome Noto
Emoji as fallbacks. HarfBuzz joins Arabic letters and fpdf2's bidi pass orders
right-to-left runs, so an Arabic caption reads as written. A paragraph that
starts in a right-to-left script is right-aligned. Characters none of the fonts
has (CJK, for now) are left out rather than drawn as empty boxes.
"""

from __future__ import annotations

import base64
import io
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

from fpdf import FPDF

from . import charts
from .models import (
    SECTION_ACTIVITY,
    SECTION_AI_SUMMARY,
    SECTION_INBOX,
    SECTION_LINK_CLICKS,
    SECTION_NETWORKS,
    SECTION_SUMMARY,
    SECTION_TOP_POSTS,
    SECTION_TRENDS,
)

MARGIN = 15
CONTENT_W = 210 - 2 * MARGIN
INK = (31, 41, 55)
MUTED = (107, 114, 128)
LINE = (229, 231, 235)
GOOD = (21, 128, 61)
BAD = (185, 28, 28)

FONT_DIR = Path(__file__).resolve().parent / "fonts"
FONT = "NotoSans"
# (family, style, file). The first family is the body font; the others are fallbacks, in order.
FONT_FILES = (
    (FONT, "", "NotoSans-Regular.ttf"),
    (FONT, "B", "NotoSans-Bold.ttf"),
    ("NotoSansArabic", "", "NotoSansArabic-Regular.ttf"),
    ("NotoSansArabic", "B", "NotoSansArabic-Bold.ttf"),
    ("NotoEmoji", "", "NotoEmoji-Regular.ttf"),
)
FALLBACK_FONTS = ("NotoSansArabic", "NotoEmoji")
# Format and joiner characters HarfBuzz consumes even where a font's cmap lacks them.
_SHAPING_MARKS = frozenset({0x200C, 0x200D, 0x200E, 0x200F, 0xFE0E, 0xFE0F})
_RTL = frozenset({"R", "AL"})
_LTR = frozenset({"L"})


@lru_cache(maxsize=1)
def _coverage() -> frozenset[int]:
    """Every code point at least one bundled font can draw."""
    from fontTools.ttLib import TTFont

    points: set[int] = set(_SHAPING_MARKS)
    for _family, _style, name in FONT_FILES:
        with TTFont(FONT_DIR / name, lazy=True) as font:
            points.update(font.getBestCmap())
    return frozenset(points)


def clean(text) -> str:
    """``text`` on one line, keeping only characters the bundled fonts can draw."""
    covered = _coverage()
    kept = (" " if ch.isspace() else ch for ch in str(text if text is not None else ""))
    return " ".join("".join(ch for ch in kept if ch == " " or ord(ch) in covered).split())


def is_rtl(text: str) -> bool:
    """Whether the first strongly directional character is right-to-left (Arabic, Hebrew…)."""
    for ch in text:
        kind = unicodedata.bidirectional(ch)
        if kind in _RTL:
            return True
        if kind in _LTR:
            return False
    return False


def _align(text: str, default: str = "L") -> str:
    return "R" if is_rtl(text) else default


def _rgb(hex_color: str) -> tuple[int, int, int]:
    return tuple(int(hex_color[i : i + 2], 16) for i in (1, 3, 5))  # type: ignore[return-value]


def _svg(markup: str) -> io.BytesIO:
    """The chart for the PDF: the accessible <title>/<desc> only matter on the web page."""
    return io.BytesIO(re.sub(r"<(title|desc)>.*?</\1>", "", markup).encode())


def _image(data_uri: str) -> io.BytesIO | None:
    if not data_uri or "," not in data_uri:
        return None
    try:
        return io.BytesIO(base64.b64decode(data_uri.split(",", 1)[1]))
    except ValueError:
        return None


class _Report(FPDF):
    def __init__(self, data: dict, total_pages: int | None = None):
        super().__init__(format="A4", unit="mm")
        self.data = data
        # fpdf2's "{nb}" alias is patched in after layout, which shaped text runs don't survive,
        # so the page count comes from a first pass instead.
        self.total_pages = total_pages
        self.accent = _rgb(data["branding"]["accent"])
        for family, style, name in FONT_FILES:
            self.add_font(family, style, FONT_DIR / name)
        self.set_fallback_fonts(list(FALLBACK_FONTS), exact_match=False)
        self.set_text_shaping(True)
        self.set_margins(MARGIN, MARGIN, MARGIN)
        self.set_auto_page_break(auto=True, margin=20)
        self.set_title(clean(data["title"]))
        self.set_author(clean(data["branding"]["agency"]))
        self.set_creator(clean(data["branding"]["product"]) or clean(data["branding"]["agency"]))
        self.set_lang("en")

    # fpdf2 switches to a fallback font inside a text run without telling the
    # page state, so the next run in the "same" font would draw its glyphs with
    # the fallback (garbled text after an Arabic or emoji run). Every text call
    # therefore re-selects its font explicitly.
    def cell(self, *args, **kwargs):
        self.current_font_is_set_on_page = False
        return super().cell(*args, **kwargs)

    def multi_cell(self, *args, **kwargs):
        self.current_font_is_set_on_page = False
        return super().multi_cell(*args, **kwargs)

    def footer(self):
        self.set_y(-14)
        self.set_font(FONT, "", 8)
        self.set_text_color(*MUTED)
        credit = self.data["branding"]["product"]
        left = clean(f"{self.data['branding']['agency']} | {self.data['workspace']}")
        self.cell(CONTENT_W / 2, 6, left, align="L")
        right = f"Page {self.page_no()}" + (f" of {self.total_pages}" if self.total_pages else "")
        if credit:
            right = f"Prepared with {clean(credit)} | " + right
        self.cell(CONTENT_W / 2, 6, right, align="R")


def _heading(pdf: _Report, text: str):
    if pdf.will_page_break(30):
        pdf.add_page()
    pdf.ln(4)
    pdf.set_font(FONT, "B", 13)
    pdf.set_text_color(*INK)
    pdf.cell(0, 7, clean(text), new_x="LMARGIN", new_y="NEXT")
    pdf.set_draw_color(*pdf.accent)
    pdf.set_line_width(0.6)
    pdf.line(MARGIN, pdf.get_y(), MARGIN + 18, pdf.get_y())
    pdf.ln(3)


def _cover(pdf: _Report):
    data = pdf.data
    b = data["branding"]
    pdf.set_fill_color(*pdf.accent)
    pdf.rect(0, 0, 210, 34, style="F")
    text_color = (255, 255, 255) if b["accent_text"] == "#ffffff" else (17, 24, 39)
    logo = _image(b["logo"])
    x = MARGIN
    if logo is not None:
        # The logo sits on white so any logo stays legible on any accent colour.
        pdf.set_fill_color(255, 255, 255)
        pdf.rect(MARGIN, 7, 40, 20, style="F")
        pdf.image(logo, x=MARGIN + 2, y=9, w=36, h=16, keep_aspect_ratio=True)
        x = MARGIN + 46
    pdf.set_xy(x, 10)
    pdf.set_text_color(*text_color)
    pdf.set_font(FONT, "B", 16)
    pdf.cell(0, 8, clean(b["agency"]), new_x="LEFT", new_y="NEXT")
    pdf.set_x(x)
    pdf.set_font(FONT, "", 10)
    pdf.cell(0, 6, clean(data["workspace"]), new_x="LMARGIN", new_y="NEXT")
    pdf.set_y(42)
    pdf.set_text_color(*INK)
    pdf.set_font(FONT, "B", 20)
    title = clean(data["title"])
    pdf.multi_cell(0, 9, title, align=_align(title), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(FONT, "", 10)
    pdf.set_text_color(*MUTED)
    period = data["period"]
    line = f"{period['label']}  ({period['days']} days, {period['timezone']})"
    if period["compare"]:
        line += f"   Compared with {period['previous_label']}"
    pdf.multi_cell(0, 5, clean(line), new_x="LMARGIN", new_y="NEXT")


def _summary(pdf: _Report):
    summary = pdf.data["ai_summary"]
    _heading(pdf, "What changed and what to do next")
    pdf.set_font(FONT, "", 10.5)
    pdf.set_text_color(*INK)
    for paragraph in [p for p in summary["text"].split("\n") if p.strip()]:
        text = clean(paragraph)
        pdf.multi_cell(0, 5.6, text, align=_align(text), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(1.5)


def _kpis(pdf: _Report):
    _heading(pdf, "Summary")
    kpis = pdf.data["kpis"]
    cols = 3
    gap = 4
    box_w = (CONTENT_W - gap * (cols - 1)) / cols
    box_h = 24
    for i, k in enumerate(kpis):
        col = i % cols
        if col == 0:
            if pdf.will_page_break(box_h + 4):
                pdf.add_page()
            row_y = pdf.get_y()
        x = MARGIN + col * (box_w + gap)
        pdf.set_draw_color(*LINE)
        pdf.set_line_width(0.3)
        pdf.rect(x, row_y, box_w, box_h)
        pdf.set_xy(x + 3, row_y + 2.5)
        pdf.set_font(FONT, "", 8.5)
        pdf.set_text_color(*MUTED)
        pdf.cell(box_w - 6, 4, clean(k["label"]))
        pdf.set_xy(x + 3, row_y + 8)
        pdf.set_font(FONT, "B", 15)
        pdf.set_text_color(*INK)
        pdf.cell(box_w - 6, 7, clean(k["display"]))
        if k["delta_display"]:
            pdf.set_xy(x + 3, row_y + 16)
            pdf.set_font(FONT, "B", 8.5)
            pdf.set_text_color(*(GOOD if k["tone"] == "good" else BAD if k["tone"] == "bad" else MUTED))
            pdf.cell(box_w - 6, 4, clean(f"{k['delta_display']} vs previous"))
        if col == cols - 1 or i == len(kpis) - 1:
            pdf.set_y(row_y + box_h + gap)
    pdf.set_text_color(*INK)


def _trends(pdf: _Report):
    _heading(pdf, "Trends")
    accent = pdf.data["branding"]["accent"]
    for t in pdf.data["trends"]:
        if pdf.will_page_break(70 if t["total"] is not None else 14):
            pdf.add_page()
        pdf.set_font(FONT, "B", 10)
        pdf.set_text_color(*INK)
        if t["total"] is None:
            pdf.cell(0, 6, clean(f"{t['label']} per day"), new_x="LMARGIN", new_y="NEXT")
            pdf.set_font(FONT, "", 9)
            pdf.set_text_color(*MUTED)
            pdf.cell(0, 5, "Not reported by these networks in this period.", new_x="LMARGIN", new_y="NEXT")
            pdf.ln(3)
            continue
        pdf.cell(0, 6, clean(f"{t['label']} per day (total {t['total_display']})"), new_x="LMARGIN", new_y="NEXT")
        svg = charts.line_chart(t["label"], t["labels"], t["current"], t["previous"], accent)
        pdf.image(_svg(svg), x=MARGIN, w=CONTENT_W)
        pdf.set_font(FONT, "", 8)
        pdf.set_text_color(*MUTED)
        legend = "Solid line: this period."
        if t["previous"]:
            legend += " Dashed grey line: previous period."
        pdf.cell(0, 5, legend, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)


def _table(pdf: _Report, headers: list[str], widths: list[float], rows: list[list[str]]):
    pdf.set_font(FONT, "B", 8.5)
    pdf.set_text_color(*MUTED)
    pdf.set_draw_color(*LINE)
    for h, w, i in zip(headers, widths, range(len(headers)), strict=True):
        pdf.cell(w, 6, clean(h), border="B", align="L" if i == 0 else "R")
    pdf.ln()
    pdf.set_font(FONT, "", 9)
    pdf.set_text_color(*INK)
    for row in rows:
        if pdf.will_page_break(7):
            pdf.add_page()
        for value, w, i in zip(row, widths, range(len(row)), strict=True):
            pdf.cell(w, 6.5, clean(value), border="B", align="L" if i == 0 else "R")
        pdf.ln()


def _networks(pdf: _Report):
    _heading(pdf, "Results by network")
    rows = [
        [
            f"{n['name']} ({n['accounts']})",
            n["followers"],
            n["exposure"],
            n["engagements"],
            n["rate"],
            n["new_followers"],
            str(n["posts"]),
        ]
        for n in pdf.data["networks"]
    ]
    _table(
        pdf,
        ["Network (accounts)", "Followers", "Views", "Engagements", "Rate", "New followers", "Posts"],
        [48, 22, 22, 26, 18, 26, 18],
        rows,
    )


def _top_posts(pdf: _Report):
    _heading(pdf, "Top posts")
    posts = pdf.data["top_posts"]
    if not posts:
        pdf.set_font(FONT, "", 10)
        pdf.cell(0, 6, "No published posts with reported views in this period.", new_x="LMARGIN", new_y="NEXT")
        return
    for post in posts:
        if pdf.will_page_break(24):
            pdf.add_page()
        top = pdf.get_y()
        thumb = _image(post["thumb"])
        text_x = MARGIN
        if thumb is not None:
            pdf.image(thumb, x=MARGIN, y=top, w=20, h=20, keep_aspect_ratio=True)
            text_x = MARGIN + 24
        pdf.set_xy(text_x, top)
        pdf.set_font(FONT, "", 10)
        pdf.set_text_color(*INK)
        caption = clean(post["caption"])
        pdf.multi_cell(CONTENT_W - (text_x - MARGIN), 5, caption, align=_align(caption), new_x="LMARGIN", new_y="NEXT")
        pdf.set_x(text_x)
        pdf.set_font(FONT, "", 8.5)
        pdf.set_text_color(*MUTED)
        meta = (
            f"{post['network']}  |  {post['published']}  |  {post['format']}  |  "
            f"{post['exposure']} views  |  {post['engagements']} engagements  |  {post['rate']} rate"
        )
        pdf.multi_cell(CONTENT_W - (text_x - MARGIN), 4.5, clean(meta), new_x="LMARGIN", new_y="NEXT")
        pdf.set_y((max(pdf.get_y(), top + 20) if thumb is not None else pdf.get_y()) + 3)


def _activity(pdf: _Report):
    # Keep the heading, its sentence and the chart together.
    if pdf.will_page_break(90):
        pdf.add_page()
    _heading(pdf, "Posting activity")
    a = pdf.data["activity"]
    pdf.set_font(FONT, "", 10)
    pdf.set_text_color(*INK)
    summary = f"{a['total']} posts published."
    if a["busiest_weekday"]:
        summary += f" Busiest day: {a['busiest_weekday']}."
    if a["formats"]:
        summary += " Formats: " + ", ".join(f"{f['posts']} {f['format'].lower()}" for f in a["formats"]) + "."
    pdf.multi_cell(0, 5.5, clean(summary), new_x="LMARGIN", new_y="NEXT")
    svg = charts.column_chart("Posts per day", a["labels"], a["counts"], pdf.data["branding"]["accent"])
    pdf.image(_svg(svg), x=MARGIN, w=CONTENT_W)


def _inbox(pdf: _Report):
    _heading(pdf, "Inbox response")
    i = pdf.data["inbox"]
    pdf.set_font(FONT, "", 10)
    pdf.set_text_color(*INK)
    pdf.multi_cell(
        0,
        5.5,
        clean(
            f"{i['received']} messages received, {i['replied']} replied to ({i['reply_rate']}). "
            f"Median time to first reply: {i['median_response']}."
        ),
        new_x="LMARGIN",
        new_y="NEXT",
    )


def _link_clicks(pdf: _Report):
    _heading(pdf, "Link clicks")
    c = pdf.data["link_clicks"]
    pdf.set_font(FONT, "", 10)
    pdf.set_text_color(*INK)
    pdf.multi_cell(
        0,
        5.5,
        clean(
            f"{c['clicks']} clicks on shortened links, about {c['people']} people. "
            f"{c['previews']} link-preview fetches by platforms are not counted."
        ),
        new_x="LMARGIN",
        new_y="NEXT",
    )
    if c["accounts"]:
        _table(
            pdf,
            ["Account", "Clicks"],
            [130, 40],
            [[f"{row['name']} ({row['platform']})", row["clicks"]] for row in c["accounts"]],
        )
    if c["links"]:
        _table(
            pdf,
            ["Link", "Clicks"],
            [130, 40],
            [[f"{row['short_url']}  {row['destination']}", row["clicks"]] for row in c["links"]],
        )


def render_pdf(data: dict) -> bytes:
    """The finished report as PDF bytes (laid out twice: the first pass counts the pages)."""
    pages = _layout(data, None).pages_count
    return bytes(_layout(data, pages).output())


def _layout(data: dict, total_pages: int | None) -> _Report:
    sections = data["sections"]
    pdf = _Report(data, total_pages)
    pdf.add_page()
    _cover(pdf)
    if SECTION_AI_SUMMARY in sections and data.get("ai_summary"):
        _summary(pdf)
    if SECTION_SUMMARY in sections:
        _kpis(pdf)
    if SECTION_TRENDS in sections:
        _trends(pdf)
    if SECTION_NETWORKS in sections:
        _networks(pdf)
    if SECTION_TOP_POSTS in sections:
        _top_posts(pdf)
    if SECTION_ACTIVITY in sections and data.get("activity"):
        _activity(pdf)
    if SECTION_INBOX in sections and data.get("inbox"):
        _inbox(pdf)
    if SECTION_LINK_CLICKS in sections and data.get("link_clicks"):
        _link_clicks(pdf)
    return pdf
