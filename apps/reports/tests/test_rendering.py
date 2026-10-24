"""Building the figures, drawing them as HTML and PDF, and the AI summary."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.template.loader import render_to_string

from apps.ai_writer.models import AIRequestLog
from apps.billing.tests.support import set_plan
from apps.reports import branding, periods
from apps.reports.builder import build_report_data, fallback_summary
from apps.reports.models import DEFAULT_SECTIONS, SECTION_KEYS
from apps.reports.pdf import clean, is_rtl, render_pdf

pytestmark = pytest.mark.django_db


def build(definition, **kwargs):
    return build_report_data(definition, periods.resolve_range(definition), **kwargs)


def html(data):
    return render_to_string("reports/_report.html", {"data": data})


class TestBuilder:
    def test_sections_and_period(self, definition, seeded):
        definition.sections = list(SECTION_KEYS)
        data = build(definition, with_ai=False)
        assert [k["key"] for k in data["kpis"]][:3] == ["followers", "new_followers", "exposure"]
        assert data["networks"] and data["trends"] and data["activity"]
        assert data["top_posts"][0]["caption"] == "Fresh beans just landed"
        assert data["period"]["days"] == 30 and data["period"]["compare"]

    def test_unselected_sections_are_left_out(self, definition, seeded):
        definition.sections = ["summary"]
        data = build(definition, with_ai=False)
        assert data["trends"] == [] and data["networks"] == [] and data["ai_summary"] is None

    def test_account_selection_limits_the_report(self, definition, seeded):
        ig, _li = seeded
        definition.social_accounts.add(ig)
        assert [n["name"] for n in build(definition, with_ai=False)["networks"]] == ["Instagram"]

    def test_data_is_json_serialisable(self, definition, seeded):
        import json

        definition.sections = list(SECTION_KEYS)
        json.dumps(build(definition, with_ai=False))


class TestHtml:
    def test_contains_every_selected_section(self, definition, seeded):
        definition.sections = list(SECTION_KEYS)
        page = html(build(definition, with_ai=False))
        for heading in (
            "What changed and what to do next",
            "Summary",
            "Trends",
            "Results by network",
            "Top posts",
            "Posting activity",
        ):
            assert heading in page
        assert "<svg" in page and "Fresh beans just landed" in page
        assert "Bright Agency" in page and "#aa2255" in page

    def test_skips_sections_not_chosen(self, definition, seeded):
        definition.sections = ["summary"]
        page = html(build(definition, with_ai=False))
        assert "Top posts" not in page and "<svg" not in page

    def test_captions_are_escaped(self, definition, seeded, workspace):
        from datetime import timedelta

        from django.utils import timezone

        from apps.analytics.tests.helpers import make_post

        when = timezone.now() - timedelta(days=2)
        make_post(
            workspace,
            seeded[0],
            when,
            "<script>alert(1)</script>",
            snapshots={"views": [(when.date(), 9000, when + timedelta(hours=5))]},
        )
        definition.sections = ["top_posts"]
        page = html(build(definition, with_ai=False))
        assert "<script>alert(1)" not in page and "&lt;script&gt;" in page

    def test_product_credit_hidden_when_white_labelled(self, definition, seeded, settings):
        settings.BRAND_NAME = "PostDelegate"
        assert "Prepared with PostDelegate" in html(build(definition, with_ai=False))
        definition.white_label = True
        page = html(build(definition, with_ai=False))
        assert "PostDelegate" not in page

    def test_no_inline_style_attributes(self, definition, seeded):
        definition.sections = list(SECTION_KEYS)
        assert " style=" not in html(build(definition, with_ai=False))


class TestPdf:
    def test_valid_pdf_with_every_section(self, definition, seeded):
        definition.sections = list(SECTION_KEYS)
        pdf = render_pdf(build(definition, with_ai=False))
        assert pdf.startswith(b"%PDF-") and pdf.rstrip().endswith(b"%%EOF")
        assert len(pdf) > 3000

    def test_unicode_captions_do_not_break_it(self, definition, seeded, workspace):
        from datetime import timedelta

        from django.utils import timezone

        from apps.analytics.tests.helpers import make_post

        make_post(workspace, seeded[0], timezone.now() - timedelta(days=1), "Café “quotes” — 日本語 🎉")
        assert render_pdf(build(definition, with_ai=False)).startswith(b"%PDF-")

    def test_text_keeps_arabic_emoji_and_punctuation(self):
        assert clean("It’s “fine” — ok… 🎉") == "It’s “fine” — ok… 🎉"
        assert clean("قهوة\nجديدة  ☕️") == "قهوة جديدة ☕️"
        # No bundled font draws CJK yet: it is left out rather than drawn as empty boxes.
        assert clean("日本語 ok") == "ok"

    def test_rtl_detection(self):
        assert is_rtl("قهوة جديدة New") and is_rtl("123 قهوة")
        assert not is_rtl("New قهوة") and not is_rtl("123")

    def test_arabic_text_is_embedded_shaped(self, definition, seeded, workspace):
        """The PDF embeds the Arabic font and draws the joined (presentation) glyphs, not isolated letters."""
        from datetime import timedelta

        from django.utils import timezone

        from apps.analytics.tests.helpers import make_post

        definition.name = "تقرير شهري"
        make_post(workspace, seeded[0], timezone.now() - timedelta(days=1), "قهوة جديدة وصلت اليوم ☕")
        pdf = render_pdf(build(definition, with_ai=False))
        assert b"NotoSansArabic" in pdf and b"NotoEmoji" in pdf and b"NotoSans" in pdf

    def test_empty_workspace_still_renders(self, definition):
        assert render_pdf(build(definition, with_ai=False)).startswith(b"%PDF-")


class TestCharts:
    def test_gridline_values_keep_halves(self):
        from apps.reports.charts import compact

        assert [compact(v) for v in (0, 0.5, 2.5, 1000, 1500, 2_500_000)] == ["0", "0.5", "2.5", "1k", "1.5k", "2.5M"]

    def test_post_counts_use_whole_gridlines(self):
        from apps.reports.charts import column_chart

        svg = column_chart("Posts", ["2026-10-01", "2026-10-02"], [1, 0], "#0b6bcb")
        assert ">0.5<" not in svg and ">1<" in svg and ">2<" in svg


class TestBranding:
    def test_pale_accent_is_darkened_to_be_readable(self):
        pale = "#ffee99"
        fixed = branding.readable_accent(pale)
        assert branding.contrast_on_white(fixed) >= branding.MIN_CONTRAST

    def test_dark_accent_unchanged_and_garbage_falls_back(self):
        assert branding.readable_accent("#112233") == "#112233"
        assert branding.readable_accent("nope") == branding.DEFAULT_ACCENT

    def test_logo_is_fetched_safely_and_embedded(self, definition, seeded):
        import io

        import httpx
        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (400, 200), "red").save(buf, format="PNG")
        response = httpx.Response(200, content=buf.getvalue(), headers={"content-type": "image/png"})
        definition.workspace.organization.logo_url = "https://cdn.example.com/logo.png"
        with patch("apps.composer.views._safe_fetch_feed", return_value=(response, "x")) as fetch:
            data = build(definition, with_ai=False)
        assert data["branding"]["logo"].startswith("data:image/png;base64,")
        assert fetch.call_args.kwargs["content_types"] == branding.LOGO_TYPES
        assert render_pdf(data).startswith(b"%PDF-")

    def test_unsafe_logo_url_is_ignored(self, definition, seeded):
        definition.workspace.organization.logo_url = "http://169.254.169.254/logo.png"
        assert build(definition, with_ai=False)["branding"]["logo"] == ""


class TestAiSummary:
    @pytest.fixture
    def with_key(self, settings):
        settings.ANTHROPIC_API_KEY = "sk-ant-test"

    def test_summary_uses_ai_with_brand_voice_and_logs_it(self, definition, seeded, with_key, fake_claude, workspace):
        workspace.brand_voice_tone = "Dry humour."
        workspace.save()
        fake_claude.queue({"text": "Views rose.\nPost more."})
        data = build(definition)
        assert data["ai_summary"] == {"text": "Views rose.\nPost more.", "source": "ai"}
        assert "Dry humour." in fake_claude.systems[0]
        prompt = fake_claude.prompts[0]
        assert "Views & impressions" in prompt and "Fresh beans just landed" in prompt
        # Metrics and top-post captions only: no account names or handles.
        assert "IG Main" not in prompt and "LI Page" not in prompt
        log = AIRequestLog.objects.get()
        assert (log.action, log.status) == ("report_summary", "ok")

    def test_falls_back_without_ai_configured(self, definition, seeded, fake_claude):
        data = build(definition)
        assert data["ai_summary"]["source"] == "fallback"
        assert "Views and impressions were" in data["ai_summary"]["text"]
        assert fake_claude.calls == []

    def test_falls_back_when_provider_fails(self, definition, seeded, with_key, fake_claude):
        from apps.ai_writer.client import AIRateLimitError

        fake_claude.queue(AIRateLimitError())
        assert build(definition)["ai_summary"]["source"] == "fallback"

    def test_falls_back_without_the_organizations_own_ai(self, definition, seeded, workspace, fake_claude, settings):
        settings.BILLING_ENABLED = True
        settings.ANTHROPIC_API_KEY = "sk-ant-test"
        set_plan(workspace.organization, "ultimate")
        fake_claude.queue({"text": "Fine."})
        build(definition)
        assert AIRequestLog.objects.count() == 1
        # Hosted mode: the platform key never powers customer AI. No call, honest fallback.
        settings.AI_PLATFORM_KEY_FOR_CUSTOMERS = False
        data = build(definition)
        assert data["ai_summary"]["source"] == "fallback" and len(fake_claude.calls) == 1

    def test_preview_never_calls_the_provider(self, definition, seeded, with_key, fake_claude):
        assert build(definition, with_ai=False)["ai_summary"]["source"] == "preview"
        assert fake_claude.calls == []

    def test_fallback_summary_with_no_figures(self):
        assert "No figures" in fallback_summary({"kpis": [], "top_posts": []})


def test_default_sections_are_valid():
    assert set(DEFAULT_SECTIONS) <= set(SECTION_KEYS)
