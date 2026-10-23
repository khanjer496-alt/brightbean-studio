"""Engagement alerts: tell the team when a post is outperforming its account.

A post is flagged when the engagement it collected in its first 24 hours is at
least :data:`ALERT_MULTIPLE` times the account's typical first-day engagement
(the median over its earlier posts). The check is bulk: one query for the
candidate posts, one for the account histories and one for their snapshots,
however many accounts there are.

"First 24 hours" is read from daily cumulative snapshots: for each metric, the
latest snapshot captured no later than 24h after publishing. The same rule
scores the post and its history, so the comparison is like for like even though
the snapshots are daily.

Notifications go through :func:`apps.notifications.engine.notify`, which owns
per-user preferences, channels and quiet hours.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta
from statistics import median

from django.conf import settings
from django.utils import timezone

from apps.billing.entitlements import get_entitlements
from apps.billing.plans import Feature
from apps.composer.models import PlatformPost
from apps.members.models import WorkspaceMembership
from apps.notifications.engine import notify
from apps.notifications.models import EventType
from apps.social_accounts.models import AnalyticsPlatformConfig, SocialAccount

from . import services
from .constants import NATIVE_ONLY_PLATFORMS
from .metrics import ENGAGEMENT_PARTS
from .models import EngagementAlert, PostInsightsSnapshot

logger = logging.getLogger(__name__)

ALERT_MULTIPLE = 3.0
MIN_BASELINE_POSTS = 5
MIN_ENGAGEMENT = 10
BASELINE_DAYS = 90
FIRST_WINDOW = timedelta(hours=24)
# Posts older than this have left their first day behind; anything newer that
# hasn't been flagged yet is checked again on every run.
CANDIDATE_AGE = timedelta(hours=48)


def first_day_engagement(rows: list[tuple[str, object, float, datetime]], published_at: datetime) -> float | None:
    """Sum over engagement metrics of the last snapshot taken within 24h of publishing.

    ``rows`` are ``(metric, date, value, captured_at)`` for one post. ``None``
    when no snapshot fits the window, i.e. nothing was measured in the first day.
    """
    deadline = published_at + FIRST_WINDOW
    latest: dict[str, tuple[object, float]] = {}
    for metric, day, value, captured_at in rows:
        if captured_at > deadline:
            continue
        if metric not in latest or day > latest[metric][0]:
            latest[metric] = (day, value)
    return sum(value for _, value in latest.values()) if latest else None


def find_outperformers(now: datetime | None = None) -> list[tuple[PlatformPost, float, float]]:
    """Unflagged recent posts beating ``ALERT_MULTIPLE`` x their account's median.

    Returns ``(post, first_day_engagement, baseline)``.
    """
    now = now or timezone.now()
    enabled = AnalyticsPlatformConfig.enabled_platforms()
    platforms = [
        p for p in enabled if services.analytics_availability(p, enabled) is None and p not in NATIVE_ONLY_PLATFORMS
    ]
    candidates = list(
        PlatformPost.objects.filter(
            status=PlatformPost.Status.PUBLISHED,
            published_at__gte=now - CANDIDATE_AGE,
            published_at__lte=now,
            engagement_alert__isnull=True,
            social_account__platform__in=platforms,
            social_account__connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        ).select_related("social_account", "post", "post__workspace")
    )
    if not candidates:
        return []

    history: dict[object, list[PlatformPost]] = defaultdict(list)
    for post in PlatformPost.objects.filter(
        social_account_id__in={c.social_account_id for c in candidates},
        status=PlatformPost.Status.PUBLISHED,
        published_at__gte=now - timedelta(days=BASELINE_DAYS),
        published_at__lte=now,
    ).only("id", "social_account_id", "published_at"):
        history[post.social_account_id].append(post)

    snapshots: dict[object, list] = defaultdict(list)
    ids = [p.id for posts in history.values() for p in posts]
    for post_id, metric, day, value, captured_at in PostInsightsSnapshot.objects.filter(
        platform_post_id__in=ids, metric_key__in=ENGAGEMENT_PARTS
    ).values_list("platform_post_id", "metric_key", "date", "value", "captured_at"):
        snapshots[post_id].append((metric, day, value, captured_at))

    day_one = {
        p.id: first_day_engagement(snapshots.get(p.id, []), p.published_at) for posts in history.values() for p in posts
    }
    hits = []
    for post in candidates:
        engagement = day_one.get(post.id)
        if engagement is None or engagement < MIN_ENGAGEMENT:
            continue
        earlier = [
            day_one[p.id]
            for p in history[post.social_account_id]
            if p.id != post.id and p.published_at < post.published_at and day_one.get(p.id) is not None
        ]
        if len(earlier) < MIN_BASELINE_POSTS:
            continue
        baseline = median(earlier)
        if baseline > 0 and engagement >= ALERT_MULTIPLE * baseline:
            hits.append((post, engagement, baseline))
    return hits


def _recipients(workspace):
    return [
        m.user
        for m in WorkspaceMembership.objects.filter(workspace=workspace).select_related("user", "custom_role")
        if m.effective_permissions.get("view_analytics", False)
    ]


def send_engagement_alerts(now: datetime | None = None) -> int:
    """Flag and announce new outperformers; returns how many posts were alerted."""
    entitled: dict[object, bool] = {}
    alerted = 0
    for post, engagement, baseline in find_outperformers(now):
        workspace = post.post.workspace
        org = workspace.organization
        if org.pk not in entitled:
            entitled[org.pk] = get_entitlements(org).has(Feature.ANALYTICS)
        if not entitled[org.pk]:
            continue
        # The unique key is the lock: only the run that creates the row notifies.
        _, created = EngagementAlert.objects.get_or_create(
            platform_post=post,
            defaults={"social_account": post.social_account, "engagement": engagement, "baseline": baseline},
        )
        if not created:
            continue
        alerted += 1
        account = post.social_account
        title = f"{account.account_name} has a post doing {engagement / baseline:.1f}x better than usual"
        body = (
            f"{engagement:,.0f} engagements in its first 24 hours, against a typical {baseline:,.0f} for "
            f"{account.account_name}. {settings.BRAND_NAME} calculates this from the engagement the platform reports."
        )
        for user in _recipients(workspace):
            try:
                notify(
                    user,
                    EventType.ENGAGEMENT_ALERT,
                    title=title,
                    body=body,
                    data={
                        "workspace_id": str(workspace.id),
                        "post_id": str(post.post_id),
                        "platform_post_id": str(post.id),
                        "social_account_id": str(account.id),
                    },
                )
            except Exception:
                logger.exception("Could not send an engagement alert for PlatformPost %s", post.id)
    return alerted
