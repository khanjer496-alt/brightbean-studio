"""Contextual hints: small dismissible tips shown on a page's first visit.

A lighter alternative to a product-tour library. Each hint is keyed, shown
until the user closes it, and remembered per user on the server.
"""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from apps.onboarding.models import DismissedHint

# Hints are for people still finding their way: an account older than this
# never sees one, so shipping hints does not greet existing users with tips.
HINT_WINDOW = timedelta(days=30)

HINTS = {
    "composer": {
        "title": "Write once, post everywhere",
        "body": "Pick your channels, write a caption and choose a time. Your draft saves as you type.",
    },
    "calendar": {
        "title": "Your content calendar",
        "body": "Posts appear on the day they go out. Click a day to add one, or drag a post to a new time.",
    },
}


def hint_for(user, key):
    """The hint ``key`` for ``user``, or ``None`` if unknown, closed or out of window."""
    hint = HINTS.get(key)
    if hint is None or not getattr(user, "is_authenticated", False):
        return None
    if timezone.now() - user.created_at > HINT_WINDOW:
        return None
    if DismissedHint.objects.filter(user=user, key=key).exists():
        return None
    return {"key": key, **hint}


def dismiss(user, key):
    """Remember that ``user`` closed hint ``key``. Returns False for an unknown key."""
    if key not in HINTS:
        return False
    DismissedHint.objects.get_or_create(user=user, key=key)
    return True
