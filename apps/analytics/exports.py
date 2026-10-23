"""Streamed CSV exports of the analytics rollups.

Every export is built from the same :mod:`rollup` functions the pages use, over
the accounts the caller already resolved inside the workspace, so a file can
never contain a number the page wouldn't show or an account the viewer can't
see. Figures an account doesn't report are written as empty cells, not 0.
"""

from __future__ import annotations

import csv
from collections.abc import Callable, Iterator, Sequence
from itertools import batched
from typing import Any

from django.http import StreamingHttpResponse
from django.utils.text import slugify

from apps.social_accounts.models import SocialAccount

from . import rollup
from .metrics import ACCOUNT_ONLY, METRICS, post_metrics_for
from .ranges import AnalyticsRange

EXPORT_KINDS: dict[str, str] = {
    "overview": "Daily totals (CSV)",
    "accounts": "Per account (CSV)",
    "posts": "Posts (CSV)",
}
POST_CHUNK = 500
CAPTION_LIMIT = 280
# A cell opening with one of these is executed as a formula by spreadsheet apps.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


class _Echo:
    """csv.writer target that hands each formatted row straight back."""

    def write(self, value: str) -> str:
        return value


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".")
    text = str(value)
    return f"'{text}" if text.startswith(_FORMULA_PREFIXES) else text


def _overview_rows(accounts: Sequence[SocialAccount], rng: AnalyticsRange) -> Iterator[list]:
    rows = rollup.collect(accounts, rng)
    keys = list(rollup.SERIES_LABELS)
    trends = {key: rollup.trend(rows, rng, key) for key in keys}
    posts = rollup.daily_posts(accounts, rng)
    header = ["date", *[rollup.SERIES_LABELS[k] for k in keys], "Posts published"]
    if rng.compare:
        header += ["previous_date", *[f"Previous {rollup.SERIES_LABELS[k]}" for k in keys]]
    yield header
    days, previous_days = rng.day_list(), rng.day_list(rng.previous_start, rng.previous_end)
    for i, day in enumerate(days):
        line = [day.isoformat(), *[trends[k]["datasets"][0]["values"][i] for k in keys], posts.get(day, 0)]
        if rng.compare:
            line += [previous_days[i].isoformat(), *[trends[k]["datasets"][1]["values"][i] for k in keys]]
        yield line


def _account_rows(accounts: Sequence[SocialAccount], rng: AnalyticsRange) -> Iterator[list]:
    rows = rollup.collect(accounts, rng)
    labels = [label for _, label, _ in rollup.KPI_SPECS]
    header = ["account", "platform", "analytics_status", *labels]
    if rng.compare:
        header += [f"{label} change %" for label in labels]
    yield header
    for row in rows:
        cells = rollup.account_cells(row, rng.compare)
        line = [
            row.account.account_name,
            row.account.platform,
            row.unavailable or "ok",
            *[c["value"] for c in cells],
        ]
        if rng.compare:
            line += [c["delta"] for c in cells]
        yield line


def _post_rows(accounts: Sequence[SocialAccount], rng: AnalyticsRange) -> Iterator[list]:
    platforms = {a.platform for a in accounts}
    metric_keys = [m for m in METRICS if m not in ACCOUNT_ONLY and any(m in post_metrics_for(p) for p in platforms)]
    yield [
        "published_at",
        "account",
        "platform",
        "format",
        "caption",
        "Views & impressions",
        "Engagements",
        "Engagement rate %",
        *[METRICS[m]["label"] for m in metric_keys],
    ]
    queryset = rollup.posts_queryset(accounts, rng).prefetch_related("post__media_attachments__media_asset")
    for chunk in batched(queryset.iterator(chunk_size=POST_CHUNK), POST_CHUNK):
        for row in rollup.post_rows(chunk, rng.tz):
            yield [
                row["published_at"].isoformat(timespec="minutes"),
                row["account"].account_name,
                row["account"].platform,
                row["format"],
                row["caption"][:CAPTION_LIMIT],
                row["exposure"],
                row["engagements"],
                row["rate"],
                *[row["stats"].get(m) for m in metric_keys],
            ]


_BUILDERS: dict[str, Callable[[Sequence[SocialAccount], AnalyticsRange], Iterator[list]]] = {
    "overview": _overview_rows,
    "accounts": _account_rows,
    "posts": _post_rows,
}
assert set(_BUILDERS) == set(EXPORT_KINDS)


def csv_response(kind: str, workspace, accounts: Sequence[SocialAccount], rng: AnalyticsRange) -> StreamingHttpResponse:
    writer = csv.writer(_Echo())
    stream = (writer.writerow([_cell(c) for c in row]) for row in _BUILDERS[kind](accounts, rng))
    response = StreamingHttpResponse(stream, content_type="text/csv; charset=utf-8")
    name = f"{slugify(workspace.name) or 'workspace'}-analytics-{kind}-{rng.start}-{rng.end}.csv"
    response["Content-Disposition"] = f'attachment; filename="{name}"'
    response["Cache-Control"] = "private, no-store"
    return response
