"""Template helpers that draw a stored report's charts."""

from __future__ import annotations

from datetime import date
from itertools import zip_longest

from django import template
from django.utils.safestring import mark_safe

from apps.reports import charts

register = template.Library()


@register.simple_tag
def trend_chart(trend: dict, accent: str):
    """Static SVG line chart for one stored trend (safe: built from numbers and escaped text)."""
    return mark_safe(  # noqa: S308
        charts.line_chart(f"{trend['label']} per day", trend["labels"], trend["current"], trend["previous"], accent)
    )


@register.simple_tag
def activity_chart(activity: dict, accent: str):
    return mark_safe(  # noqa: S308
        charts.column_chart("Posts per day", activity["labels"], activity["counts"], accent)
    )


@register.simple_tag
def trend_rows(trend: dict) -> list[tuple[str, str, str]]:
    """``(day, this period, previous period)`` strings for the chart's table view."""

    def show(value):
        return "–" if value is None else f"{value:,.0f}"

    return [
        (f"{date.fromisoformat(day):%b %-d, %Y}", show(cur), show(prev))
        for day, cur, prev in zip_longest(trend["labels"], trend["current"], trend["previous"] or [], fillvalue=None)
        if day
    ]
