"""Background tasks for outbound webhooks."""

import datetime as dt
import logging

from background_task import background
from django.utils import timezone

logger = logging.getLogger(__name__)

# Later attempts are made by this sweep, not by scheduling a task per retry,
# so a restart or a lost task never strands a delivery.
RETRY_SWEEP_INTERVAL_SECONDS = 60
RETRY_SWEEP_BATCH_LIMIT = 200

LOG_RETENTION_DAYS = 30
PRUNE_INTERVAL_SECONDS = 24 * 60 * 60
# Rows per DELETE, so the sweep never holds a lock the delivery path has to wait on.
_PRUNE_CHUNK = 5_000


@background(schedule=0)
def deliver_webhook(delivery_id):
    """Make the first attempt for a freshly queued delivery."""
    from .services import attempt_delivery

    attempt_delivery(delivery_id)


@background(schedule=0)
def retry_due_webhook_deliveries():
    """Attempt every pending delivery whose retry time has come.

    Registered on a 1-minute repeating schedule. Nothing may escape: a task
    that keeps raising is dropped by django-background-tasks, and with it every
    later retry.
    """
    from .services import attempt_delivery, due_delivery_ids

    try:
        sent = 0
        for delivery_id in due_delivery_ids(RETRY_SWEEP_BATCH_LIMIT):
            try:
                sent += attempt_delivery(delivery_id)
            except Exception:
                logger.exception("Webhook delivery %s failed unexpectedly", delivery_id)
    except Exception:
        logger.exception("Webhook retry sweep failed")
        return
    if sent:
        logger.info("Made %d webhook delivery attempt(s) from the retry sweep", sent)


@background(schedule=0)
def prune_webhook_deliveries():
    """Delete delivery log rows older than the retention window.

    Rows still pending are kept: they can only be that old if the sweep is
    behind, and deleting them would drop an event nobody has seen fail.
    """
    from .models import DeliveryStatus, WebhookDelivery

    cutoff = timezone.now() - dt.timedelta(days=LOG_RETENTION_DAYS)
    stale = WebhookDelivery.objects.filter(created_at__lt=cutoff).exclude(status=DeliveryStatus.PENDING)
    deleted = 0
    try:
        while pks := list(stale.values_list("pk", flat=True)[:_PRUNE_CHUNK]):
            count, _ = WebhookDelivery.objects.filter(pk__in=pks).delete()
            deleted += count
    except Exception:
        logger.exception("Webhook log prune failed")
        return
    if deleted:
        logger.info("Pruned %d webhook delivery log row(s) older than %d days", deleted, LOG_RETENTION_DAYS)
