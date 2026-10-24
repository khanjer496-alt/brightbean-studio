"""The branded report's "Link clicks" section: figures, web page and PDF."""

from __future__ import annotations

import datetime as dt

import pytest
from django.template.loader import render_to_string
from django.test import Client
from django.utils import timezone

from apps.links import services
from apps.links.models import LinkDailyStat
from apps.reports import periods
from apps.reports.builder import build_report_data
from apps.reports.models import SECTION_KEYS, SECTION_LINK_CLICKS, SECTIONS
from apps.reports.pdf import render_pdf

pytestmark = pytest.mark.django_db

BROWSER = {"HTTP_USER_AGENT": "Mozilla/5.0 (X11; Linux) Firefox/130.0"}


def build(definition):
    return build_report_data(definition, periods.resolve_range(definition), with_ai=False)


@pytest.fixture
def with_clicks(workspace, seeded):
    ig, li = seeded
    link = services.create_short_link(workspace, "https://example.com/offer", social_account=ig)
    other = services.create_short_link(workspace, "https://example.com/other", social_account=li)
    for i in range(3):
        Client().get(f"/s/{link.code}", REMOTE_ADDR=f"203.0.113.{i + 1}", **BROWSER)
    Client().get(f"/s/{other.code}", **BROWSER)
    Client().get(f"/s/{link.code}", HTTP_USER_AGENT="facebookexternalhit/1.1")
    # A report covers whole days up to yesterday, so move today's counters back a day.
    LinkDailyStat.objects.update(day=timezone.now().astimezone(dt.UTC).date() - dt.timedelta(days=1))
    return link, other


def test_the_section_is_offered_in_the_report_form():
    assert SECTION_LINK_CLICKS in SECTION_KEYS and ("link_clicks", "Link clicks") in SECTIONS


def test_clicks_by_account_and_link(definition, seeded, with_clicks):
    definition.sections = ["link_clicks"]
    data = build(definition)["link_clicks"]
    assert (data["clicks"], data["people"], data["previews"]) == ("4", "4", "1")
    assert [(a["platform"], a["clicks"]) for a in data["accounts"]] == [("instagram", "3"), ("linkedin_company", "1")]
    assert data["links"][0]["short_url"] == with_clicks[0].short_url and data["links"][0]["clicks"] == "3"


def test_the_report_only_counts_its_own_accounts(definition, seeded, with_clicks):
    ig, _li = seeded
    definition.sections = ["link_clicks"]
    definition.social_accounts.add(ig)
    assert build(definition)["link_clicks"]["clicks"] == "3"


def test_no_links_means_no_section(definition, seeded):
    definition.sections = ["link_clicks"]
    data = build(definition)
    assert data["link_clicks"] is None
    assert "Link clicks" not in render_to_string("reports/_report.html", {"data": data})


def test_only_selected_sections_are_built(definition, seeded, with_clicks):
    definition.sections = ["summary"]
    assert build(definition)["link_clicks"] is None


def test_html_and_pdf_carry_the_section(definition, seeded, with_clicks):
    definition.sections = ["summary", "link_clicks"]
    data = build(definition)
    html = render_to_string("reports/_report.html", {"data": data})
    assert 'id="report-links-h"' in html and with_clicks[0].short_url in html and "Link-preview fetches" in html
    assert "<caption" in html
    pdf = render_pdf(data)
    assert pdf.startswith(b"%PDF") and len(pdf) > 1000


def test_data_stays_json_serialisable(definition, seeded, with_clicks):
    import json

    definition.sections = list(SECTION_KEYS)
    json.dumps(build(definition))
