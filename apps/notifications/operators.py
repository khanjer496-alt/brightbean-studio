"""Alerts for the people who run the service, not for a workspace's members."""

from __future__ import annotations

import logging
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone

from .engine import notify
from .models import EventType, Notification

logger = logging.getLogger(__name__)

# One alert per key in this window, so a failing platform doesn't send one per post.
ALERT_COOLDOWN = timedelta(hours=6)


def alert_staff(*, key: str, title: str, body: str) -> int:
    """Notify every active staff user, unless ``key`` was already alerted within ``ALERT_COOLDOWN``.

    Returns how many users were notified.
    """
    recent = Notification.objects.filter(
        event_type=EventType.PLATFORM_API_ALERT,
        data__alert_key=key,
        created_at__gte=timezone.now() - ALERT_COOLDOWN,
    )
    if recent.exists():
        return 0
    notified = 0
    for user in get_user_model().objects.filter(is_staff=True, is_active=True):
        if notify(
            user=user,
            event_type=EventType.PLATFORM_API_ALERT,
            title=title,
            body=body,
            data={"alert_key": key},
        ):
            notified += 1
    return notified
