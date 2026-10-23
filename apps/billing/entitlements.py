"""What an organization's plan lets it do — the one place that decides.

Views, the Agent API, MCP and the model guard in ``apps.billing.signals`` all
call into here; nothing else compares plan names. With
``settings.BILLING_ENABLED`` off (every self-hosted install) every check
passes and nothing is queried, so existing deployments behave exactly as
before.

Limits stop *adding* — connecting another channel, creating or restoring a
workspace, scheduling another post. An organization that is over a limit
after a downgrade keeps everything it has; it just can't add more until it is
back under, and ``PlanLimitError`` says what to remove.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from .plans import FEATURE_LABELS, TRIAL_PLAN, Feature, Plan, lowest_plan_with

# PlatformPost statuses that use up a month's scheduled-post allowance: once a
# post is committed to the publisher it counts, whatever happens to it next.
COUNTED_POST_STATUSES = ("scheduled", "publishing", "published", "failed")


class PlanLimitError(Exception):
    """An action the organization's current plan does not allow.

    ``str(exc)`` is written for the person who tried: what the limit is, and
    what to remove or upgrade.
    """

    def __init__(self, message: str, *, code: str, feature: str = ""):
        super().__init__(message)
        self.message = message
        self.code = code
        self.feature = feature

    @property
    def upgrade_url(self) -> str:
        return reverse("billing:overview")


@dataclass(frozen=True)
class Entitlements:
    """Limits and features in force for one organization. ``None`` = unlimited."""

    plan: Plan | None
    max_channels: int | None
    max_workspaces: int | None
    max_scheduled_posts_per_month: int | None
    max_api_calls_per_day: int | None
    max_ai_requests_per_day: int | None
    features: frozenset[str]
    unlimited: bool = False

    def has(self, feature: Feature | str) -> bool:
        return self.unlimited or str(feature) in self.features


UNLIMITED = Entitlements(
    plan=None,
    max_channels=None,
    max_workspaces=None,
    max_scheduled_posts_per_month=None,
    max_api_calls_per_day=None,
    max_ai_requests_per_day=None,
    features=frozenset(),
    unlimited=True,
)


def billing_enabled() -> bool:
    return bool(getattr(settings, "BILLING_ENABLED", False))


def get_subscription(org):
    """The organization's ``Subscription``, starting its trial on first use."""
    from .models import Subscription

    sub, _created = Subscription.objects.get_or_create(
        organization=org,
        defaults={
            "plan": TRIAL_PLAN,
            "status": Subscription.Status.TRIALING,
            "trial_ends_at": timezone.now() + dt.timedelta(days=settings.BILLING_TRIAL_DAYS),
        },
    )
    return sub


def get_entitlements(org) -> Entitlements:
    if not billing_enabled() or org is None:
        return UNLIMITED
    plan = get_subscription(org).effective_plan()
    return Entitlements(
        plan=plan,
        max_channels=plan.max_channels,
        max_workspaces=plan.max_workspaces,
        max_scheduled_posts_per_month=plan.max_scheduled_posts_per_month,
        max_api_calls_per_day=plan.max_api_calls_per_day,
        max_ai_requests_per_day=plan.max_ai_requests_per_day,
        features=frozenset(str(f) for f in plan.features),
    )


# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------


def month_bounds(when: dt.datetime | None = None) -> tuple[dt.datetime, dt.datetime]:
    """Start (inclusive) and end (exclusive) of ``when``'s calendar month, in UTC."""
    when = (when or timezone.now()).astimezone(dt.UTC)
    start = when.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
    return start, end


def channel_count(org) -> int:
    from apps.social_accounts.models import SocialAccount

    return SocialAccount.objects.filter(workspace__organization=org).count()


def workspace_count(org) -> int:
    from apps.workspaces.models import Workspace

    return Workspace.objects.filter(organization=org, is_archived=False).count()


def scheduled_post_count(org, when: dt.datetime | None = None) -> int:
    """Posts (one per channel) going out in ``when``'s month."""
    from apps.composer.models import PlatformPost

    start, end = month_bounds(when)
    return PlatformPost.objects.filter(
        post__workspace__organization=org,
        status__in=COUNTED_POST_STATUSES,
        scheduled_at__gte=start,
        scheduled_at__lt=end,
    ).count()


def api_call_count(org, when: dt.datetime | None = None) -> int:
    """Agent API and MCP calls the organization made in ``when``'s UTC day."""
    from apps.api.usage import org_usage_in_day

    return org_usage_in_day(org.pk, when or timezone.now())


def ai_request_count(org, when: dt.datetime | None = None) -> int:
    """AI writing-assistant requests on the platform key in ``when``'s UTC day.

    Only answered calls count: a timeout or provider outage doesn't use up
    the allowance.
    """
    from apps.ai_writer.models import AIRequestLog

    start = (when or timezone.now()).astimezone(dt.UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return AIRequestLog.objects.filter(
        organization=org,
        used_organization_key=False,
        status=AIRequestLog.Status.OK,
        created_at__gte=start,
        created_at__lt=start + dt.timedelta(days=1),
    ).count()


def usage_summary(org) -> list[dict]:
    """Usage against each limit, for the Plan & billing page."""
    ent = get_entitlements(org)
    rows = [
        ("channels", "Channels", channel_count(org), ent.max_channels, "Disconnect channels"),
        ("workspaces", "Workspaces", workspace_count(org), ent.max_workspaces, "Archive workspaces"),
        (
            "posts",
            "Scheduled posts this month",
            scheduled_post_count(org),
            ent.max_scheduled_posts_per_month,
            "Move posts to next month or back to drafts",
        ),
        # Resets every UTC day, so there is never anything to remove.
        ("api_calls", "API & MCP calls today", api_call_count(org), ent.max_api_calls_per_day, ""),
        ("ai_requests", "AI requests today", ai_request_count(org), ent.max_ai_requests_per_day, ""),
    ]
    summary = []
    for key, label, used, limit, remedy in rows:
        over_by = max(used - limit, 0) if limit is not None and remedy else 0
        summary.append(
            {
                "key": key,
                "label": label,
                "used": used,
                "limit": limit,
                # Where the usage meter turns amber.
                "warn_at": int(limit * 0.8) if limit else 0,
                "at_limit": limit is not None and used >= limit,
                "over_by": over_by,
                "remedy": remedy,
            }
        )
    return summary


# ---------------------------------------------------------------------------
# Checks — each raises PlanLimitError, or returns None when allowed.
# ---------------------------------------------------------------------------


def require_feature(org, feature: Feature) -> None:
    ent = get_entitlements(org)
    if ent.has(feature):
        return
    label = FEATURE_LABELS[feature]
    upgrade = lowest_plan_with(feature)
    target = f" Upgrade to {upgrade.name} or higher to use it." if upgrade else ""
    raise PlanLimitError(
        f"{label} isn't included in your {ent.plan.name if ent.plan else 'current'} plan.{target}",
        code="feature_not_in_plan",
        feature=str(feature),
    )


def check_can_add_channel(org) -> None:
    ent = get_entitlements(org)
    if ent.max_channels is None:
        return
    used = channel_count(org)
    if used < ent.max_channels:
        return
    raise PlanLimitError(
        f"Your {ent.plan.name} plan includes {ent.max_channels} channels and this organization has {used}. "
        f"{_remove_hint(used, ent.max_channels, 'channel', 'Disconnect')} or upgrade your plan to connect more.",
        code="channel_limit",
    )


def check_can_add_workspace(org) -> None:
    ent = get_entitlements(org)
    if ent.max_workspaces is None:
        return
    used = workspace_count(org)
    if used < ent.max_workspaces:
        return
    raise PlanLimitError(
        f"Your {ent.plan.name} plan includes {ent.max_workspaces} active "
        f"workspace{'s' if ent.max_workspaces != 1 else ''} and this organization has {used}. "
        f"{_remove_hint(used, ent.max_workspaces, 'workspace', 'Archive')} or upgrade your plan to add more.",
        code="workspace_limit",
    )


def check_can_schedule_posts(org, count: int = 1, when: dt.datetime | None = None) -> None:
    """Refuse when ``count`` more posts would exceed ``when``'s monthly allowance."""
    if count <= 0:
        return
    ent = get_entitlements(org)
    if ent.max_scheduled_posts_per_month is None:
        return
    used = scheduled_post_count(org, when)
    if used + count <= ent.max_scheduled_posts_per_month:
        return
    month = month_bounds(when)[0].strftime("%B %Y")
    raise PlanLimitError(
        f"Your {ent.plan.name} plan includes {ent.max_scheduled_posts_per_month} scheduled posts per month "
        f"and {month} already has {used}. Save this as a draft, schedule it for another month, "
        f"or upgrade for unlimited posts.",
        code="post_limit",
    )


def ai_requests_remaining(org) -> int | None:
    """AI requests left on the platform key today, or None when the plan has no cap."""
    ent = get_entitlements(org)
    if ent.max_ai_requests_per_day is None:
        return None
    return max(ent.max_ai_requests_per_day - ai_request_count(org), 0)


def check_can_use_ai(org, count: int = 1) -> None:
    """Refuse unless ``count`` more AI requests on the platform key fit in today's plan cap.

    ``count`` is the most provider calls one action can make (adapting a
    caption for five accounts may need a "shorten" pass for each), reserved
    up front so an action can't run the organization past its allowance.
    """
    remaining = ai_requests_remaining(org)
    if remaining is None or remaining >= count:
        return
    ent = get_entitlements(org)
    if remaining == 0:
        message = (
            f"Your {ent.plan.name} plan includes {ent.max_ai_requests_per_day:,} AI requests a day and this "
            f"organization has used them all."
        )
    else:
        message = (
            f"That can take up to {count} AI requests and this organization has {remaining} left today on "
            f"the {ent.plan.name} plan. Try it with fewer accounts or posts."
        )
    raise PlanLimitError(
        f"{message} The allowance resets at midnight UTC — upgrade your plan or add your own AI provider "
        f"key to keep writing with AI today.",
        code="ai_request_limit",
    )


def _remove_hint(used: int, limit: int, noun: str, verb: str) -> str:
    to_remove = used - limit + 1
    return f"{verb} {to_remove} {noun}{'s' if to_remove != 1 else ''}"
