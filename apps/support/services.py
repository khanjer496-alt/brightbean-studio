"""Impersonation lifecycle, announcement selection and the publish-failure summary."""

import datetime as dt
import re

from allauth.account.authentication import get_authentication_records
from allauth.account.internal.flows.reauthentication import get_reauthentication_flows
from django.core.cache import cache
from django.db.models import Count, F, Max, Q
from django.utils import timezone

from apps.api.limits import _client_ip
from apps.billing.entitlements import billing_enabled
from apps.billing.models import Subscription
from apps.credentials.models import PlatformCredential
from apps.members.models import OrgMembership
from apps.publisher.models import PublishLog

from .models import Announcement, AnnouncementDismissal, ImpersonationSession

SESSION_KEY = "support_impersonation"
# Staff must have signed in (or re-entered a password / 2FA code) this recently to start.
REAUTH_WINDOW = dt.timedelta(minutes=10)
# A "view as" session ends on its own after this long, however busy the tab is.
SESSION_LIFETIME = dt.timedelta(minutes=30)

# "Is any announcement unexpired?" is asked on every page; the answer is cached and dropped whenever one is saved.
ANNOUNCEMENTS_PENDING_KEY = "support:announcements_pending"
ANNOUNCEMENTS_PENDING_TTL = 60


class ImpersonationRefusedError(Exception):
    """The target can't be viewed; the message is safe to show staff."""


def has_recent_authentication(request) -> bool:
    """Whether this session authenticated, or re-authenticated, within ``REAUTH_WINDOW``."""
    cutoff = timezone.now().timestamp() - REAUTH_WINDOW.total_seconds()
    return any(record.get("at", 0) >= cutoff for record in get_authentication_records(request))


def can_reauthenticate(user) -> bool:
    """False for a staff account with neither a password nor 2FA, which has nothing to re-enter."""
    return bool(get_reauthentication_flows(user))


def check_target(staff_user, target) -> None:
    if target.pk == staff_user.pk:
        raise ImpersonationRefusedError("You can't view as yourself.")
    if target.is_staff or target.is_superuser:
        raise ImpersonationRefusedError("Staff accounts can't be viewed as.")
    if not target.is_active:
        raise ImpersonationRefusedError("That account is deactivated.")


def start_impersonation(request, target, reason: str) -> ImpersonationSession:
    staff_user = request.user
    check_target(staff_user, target)
    now = timezone.now()
    membership = OrgMembership.objects.filter(user=target).select_related("organization").first()
    record = ImpersonationSession.objects.create(
        staff_user=staff_user,
        staff_email=staff_user.email,
        target_user=target,
        target_email=target.email,
        target_organization=membership.organization if membership else None,
        reason=reason,
        ip_address=_client_ip(request) or None,
        started_at=now,
        expires_at=now + SESSION_LIFETIME,
    )
    request.session[SESSION_KEY] = {"record_id": str(record.id), "target_id": str(target.pk)}
    return record


def end_impersonation(request, reason: str) -> None:
    """Close the audit row and drop the session marker; safe to call when nothing is active."""
    state = request.session.pop(SESSION_KEY, None)
    if state:
        ImpersonationSession.objects.filter(id=state["record_id"], ended_at__isnull=True).update(
            ended_at=timezone.now(), end_reason=reason
        )


def org_support_access(org, limit: int = 5):
    """The latest times support viewed this organization's members, for its owners to see."""
    return ImpersonationSession.objects.filter(target_organization=org).order_by("-started_at")[:limit]


# ---------------------------------------------------------------------------
# Announcements
# ---------------------------------------------------------------------------


def organization_plan_slug(org) -> str | None:
    """The plan the organization is on, or None when plans aren't enforced."""
    if org is None or not billing_enabled():
        return None
    subscription = Subscription.objects.filter(organization=org).first()
    return subscription.effective_plan().slug if subscription else None


def any_announcements() -> bool:
    """Whether anything could be showing; cached so ordinary pages don't pay a query for the usual "no"."""
    pending = cache.get(ANNOUNCEMENTS_PENDING_KEY)
    if pending is None:
        now = timezone.now()
        pending = (
            Announcement.objects.filter(is_active=True).filter(Q(ends_at__isnull=True) | Q(ends_at__gt=now)).exists()
        )
        cache.set(ANNOUNCEMENTS_PENDING_KEY, pending, ANNOUNCEMENTS_PENDING_TTL)
    return pending


def announcements_for(user, org) -> list[Announcement]:
    """Live announcements this user has not dismissed and whose audience includes their organization."""
    if not any_announcements():
        return []
    now = timezone.now()
    live = (
        Announcement.objects.filter(is_active=True, starts_at__lte=now)
        .filter(Q(ends_at__isnull=True) | Q(ends_at__gt=now))
        .exclude(dismissals__user=user)
    )
    plan_slug = organization_plan_slug(org)
    return [a for a in live if not a.plan_slugs or plan_slug in a.plan_slugs]


def dismiss_announcement(user, announcement_id) -> None:
    announcement = Announcement.objects.filter(id=announcement_id).first()
    if announcement:
        AnnouncementDismissal.objects.get_or_create(announcement=announcement, user=user)


# ---------------------------------------------------------------------------
# Publish failures
# ---------------------------------------------------------------------------

ERROR_WINDOWS = {"24h": dt.timedelta(hours=24), "7d": dt.timedelta(days=7)}
SAMPLE_LENGTH = 200

# Provider error text sometimes echoes the request: strip credentials and addresses before staff see it.
_SECRET_PAIR = re.compile(
    r"(?i)\b(access_token|refresh_token|token|api[_-]?key|secret|password|authorization)\b[\"']?\s*[:=]\s*\S+"
)
_BEARER = re.compile(r"(?i)\bbearer\s+\S+")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def scrub_sample(message: str) -> str:
    message = _BEARER.sub("Bearer [redacted]", message)
    message = _SECRET_PAIR.sub(lambda m: f"{m.group(1)}=[redacted]", message)
    message = _EMAIL.sub("[email]", message)
    return message[:SAMPLE_LENGTH]


def platform_error_summary(window: str) -> list[dict]:
    """Failed publish attempts in the window, grouped by platform then HTTP status.

    Newest failures first inside a platform; platforms with the most failures first, so an outage sorts to the top.
    """
    since = timezone.now() - ERROR_WINDOWS[window]
    failures = (
        PublishLog.objects.filter(created_at__gte=since)
        .exclude(error_message="")
        .annotate(platform=F("platform_post__social_account__platform"))
    )
    groups = failures.values("platform", "status_code").annotate(
        failures=Count("id"),
        accounts=Count("platform_post__social_account", distinct=True),
        last_seen=Max("created_at"),
    )
    latest = {
        (row["platform"], row["status_code"]): row
        for row in failures.order_by("platform", "status_code", "-created_at")
        .distinct("platform", "status_code")
        .values("platform", "status_code", "error_message", "platform_post__social_account__account_handle")
    }
    labels = dict(PlatformCredential.Platform.choices)
    by_platform: dict[str, dict] = {}
    for group in groups:
        sample = latest[(group["platform"], group["status_code"])]
        entry = by_platform.setdefault(
            group["platform"],
            {
                "platform": group["platform"],
                "label": labels.get(group["platform"], group["platform"]),
                "failures": 0,
                "codes": [],
            },
        )
        entry["failures"] += group["failures"]
        entry["codes"].append(
            {
                "status_code": group["status_code"],
                "failures": group["failures"],
                "accounts": group["accounts"],
                "last_seen": group["last_seen"],
                "sample": scrub_sample(sample["error_message"]),
                "account_handle": sample["platform_post__social_account__account_handle"],
            }
        )
    for entry in by_platform.values():
        entry["codes"].sort(key=lambda code: code["failures"], reverse=True)
    return sorted(by_platform.values(), key=lambda entry: entry["failures"], reverse=True)
