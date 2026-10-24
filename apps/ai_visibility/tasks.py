"""Background work for AI visibility reports."""

from __future__ import annotations

import logging
from datetime import timedelta

from background_task import background
from django.utils import timezone

logger = logging.getLogger(__name__)

SWEEP_INTERVAL_SECONDS = 86400  # a profile is due once a week; the sweep checks daily so a down worker catches up
SWEEP_BUDGET = timedelta(minutes=20)


@background(schedule=0)
def run_due_visibility_reports() -> None:
    """Daily sweep: run a report for every active profile that has gone a week without one."""
    from . import services

    stats = services.run_due(deadline=timezone.now() + SWEEP_BUDGET)
    if stats["ran"] or stats["skipped"]:
        logger.info("ai visibility: %s report(s) run, %s skipped", stats["ran"], stats["skipped"])


@background(schedule=0)
def execute_visibility_run(run_id: str) -> None:
    """Build one queued report (a manual "Run now")."""
    from .models import VisibilityRun
    from .services import execute_run

    run = VisibilityRun.objects.select_related("profile__workspace__organization").filter(pk=run_id).first()
    if run is None or run.status != VisibilityRun.Status.QUEUED:
        return
    execute_run(run)
