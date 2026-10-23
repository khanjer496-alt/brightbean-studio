"""Background tasks for client reports."""

import logging

from background_task import background

logger = logging.getLogger(__name__)

SWEEP_INTERVAL_SECONDS = 5 * 60
SWEEP_BATCH_LIMIT = 20


@background(schedule=0)
def execute_report_run(run_id):
    """Build one run now (queued by "Run now"); the sweep would pick it up anyway."""
    from .services import execute_run

    execute_run(run_id)


@background(schedule=0)
def run_due_reports():
    """Create runs for slots that have come, then build whatever is waiting.

    Registered on a 5-minute repeating schedule. Nothing may escape: a task that
    keeps raising is dropped by django-background-tasks, and with it every
    later report.
    """
    from .services import due_run_ids, execute_run, reap_stale_runs, schedule_due_runs

    try:
        reap_stale_runs()
        created = schedule_due_runs()
        built = 0
        for run_id in due_run_ids(SWEEP_BATCH_LIMIT):
            try:
                built += bool(execute_run(run_id))
            except Exception:
                logger.exception("Report run %s failed unexpectedly", run_id)
    except Exception:
        logger.exception("Report sweep failed")
        return
    if created or built:
        logger.info("reports: %s scheduled, %s built", created, built)
