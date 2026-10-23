"""Competitor snapshots, run hourly by the worker; each competitor is read once a day in its own hour."""

from __future__ import annotations

import logging

from background_task import background

logger = logging.getLogger(__name__)

SNAPSHOT_INTERVAL_SECONDS = 3600  # hourly; services.due_hour spreads the day's reads


@background(schedule=0)
def snapshot_competitors() -> None:
    """Hourly cron: read the competitors whose hour has come and that have no snapshot today; prune old history."""
    from . import services

    stats = services.snapshot_all()
    pruned = services.prune_snapshots()
    if stats["read"] or stats["failed"] or pruned:
        logger.info(
            "competitor snapshots: %s read, %s failed, %s skipped, %s waiting, %s old rows pruned",
            stats["read"],
            stats["failed"],
            stats["skipped"],
            stats["waiting"],
            pruned,
        )
