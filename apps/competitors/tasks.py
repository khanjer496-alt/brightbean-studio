"""Daily competitor snapshot, run by the worker like the analytics crons."""

from __future__ import annotations

import logging

from background_task import background

logger = logging.getLogger(__name__)

SNAPSHOT_INTERVAL_SECONDS = 86400  # daily


@background(schedule=0)
def snapshot_competitors() -> None:
    """Daily cron: read every tracked competitor's public numbers once and prune old history."""
    from . import services

    stats = services.snapshot_all()
    pruned = services.prune_snapshots()
    logger.info(
        "competitor snapshots: %s read, %s skipped, %s failed, %s old rows pruned",
        stats["read"],
        stats["skipped"],
        stats["failed"],
        pruned,
    )
