"""Background tasks for engagement automations."""

import logging

from background_task import background

logger = logging.getLogger(__name__)

# How often posts with a waiting rule are looked at (each provider spaces its own reads further apart).
CHECK_INTERVAL_SECONDS = 10 * 60
# How often automations waiting out their undo window are released.
RELEASE_INTERVAL_SECONDS = 60


@background(schedule=0)
def check_plug_rules() -> None:
    """Watch published posts and fire the rules whose count was reached."""
    from .services import check_rules

    result = check_rules()
    if result.fired or result.failed:
        logger.info(
            "Plugs: %d post(s) read, %d fired, %d expired, %d failed",
            result.read,
            result.fired,
            result.expired,
            result.failed,
        )


@background(schedule=0)
def release_due_plugs() -> None:
    """Act on automations whose undo window ended, and retry the ones that failed in passing."""
    from .services import release_due

    done = release_due()
    if done:
        logger.info("Plugs: %d automation(s) released", done)
