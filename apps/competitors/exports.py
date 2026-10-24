"""Streamed CSV exports of the competitor comparison and its history.

Built from the same :mod:`services` functions the page uses, so a file never
holds a number the page wouldn't show. Figures a network doesn't publish are
empty cells, not 0.
"""

from __future__ import annotations

import csv
from collections.abc import Iterator, Sequence

from django.http import StreamingHttpResponse
from django.utils.text import slugify

from apps.analytics.exports import _cell, _Echo
from apps.analytics.ranges import AnalyticsRange
from apps.social_accounts.models import SocialAccount

from . import services
from .models import Competitor, CompetitorSnapshot

EXPORT_KINDS = {
    "comparison": "Comparison (CSV)",
    "history": "Daily history (CSV)",
}


def _comparison_rows(workspace, accounts: Sequence[SocialAccount], rng: AnalyticsRange) -> Iterator[list]:
    yield [
        "network",
        "who",
        "name",
        "handle",
        "followers",
        "follower growth %",
        "posts per week",
        "avg engagements per post",
        "last read",
    ]
    for group in services.comparison(workspace, rng, accounts)["groups"]:
        for row in (*group["you"], *group["competitors"]):
            yield [
                group["network"].name,
                "you" if row["kind"] == "you" else "competitor",
                row["label"],
                row["handle"],
                row["followers"],
                row["growth"],
                row["posts_per_week"],
                row["avg_engagement"],
                row["last_synced"].isoformat() if row["last_synced"] else "",
            ]


def _history_rows(workspace, accounts: Sequence[SocialAccount], rng: AnalyticsRange) -> Iterator[list]:
    yield ["date", "network", "handle", "followers", "posts", "avg engagements per post", "posts per week"]
    competitors = {c.id: c for c in Competitor.objects.filter(workspace=workspace)}
    snaps = CompetitorSnapshot.objects.filter(
        competitor_id__in=competitors, date__gte=rng.start, date__lte=rng.end
    ).order_by("date", "competitor__platform", "competitor__handle")
    for snap in snaps.iterator():
        competitor = competitors[snap.competitor_id]
        network = services.network_for(competitor.platform)
        yield [
            snap.date.isoformat(),
            network.name if network else competitor.platform,
            competitor.handle,
            snap.followers,
            snap.posts_count,
            snap.avg_engagement,
            snap.posts_per_week,
        ]


_BUILDERS = {"comparison": _comparison_rows, "history": _history_rows}
assert set(_BUILDERS) == set(EXPORT_KINDS)


def csv_response(kind: str, workspace, accounts: Sequence[SocialAccount], rng: AnalyticsRange) -> StreamingHttpResponse:
    writer = csv.writer(_Echo())
    stream = (writer.writerow([_cell(c) for c in row]) for row in _BUILDERS[kind](workspace, accounts, rng))
    response = StreamingHttpResponse(stream, content_type="text/csv; charset=utf-8")
    name = f"{slugify(workspace.name) or 'workspace'}-competitors-{kind}-{rng.start}-{rng.end}.csv"
    response["Content-Disposition"] = f'attachment; filename="{name}"'
    response["Cache-Control"] = "private, no-store"
    return response
