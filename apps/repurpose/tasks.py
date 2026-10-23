"""Background task: make a queued repurposing job's provider calls."""

from background_task import background


@background(schedule=0)
def execute_repurpose_job(job_id):
    from .services import execute_job

    execute_job(job_id)
