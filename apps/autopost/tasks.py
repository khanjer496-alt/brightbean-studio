"""Background task for feed auto-posting."""

import logging

from background_task import background

logger = logging.getLogger(__name__)

# How often the feeds due for a read are looked at. Each feed is read at most every ``services.POLL_INTERVAL``.
POLL_INTERVAL_SECONDS = 10 * 60


@background(schedule=0)
def poll_feeds() -> None:
    """Read the feeds that are due and make posts from their new items."""
    from .services import poll_due

    polled = poll_due()
    if polled:
        logger.info("Feed autopost: %d feed(s) read", polled)
