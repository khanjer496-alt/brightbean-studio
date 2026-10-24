"""What an organization's plan lets it do — the one place that decides.

Views, the Agent API, MCP and the model guard in ``apps.billing.signals`` all
call into here; nothing else compares plan names. With
``settings.BILLING_ENABLED`` off (every self-hosted install) every check
passes and nothing is queried, so existing deployments behave exactly as
before.

Limits stop *adding* — connecting another channel, creating or restoring a
workspace, scheduling another post, inviting another member. An organization that is over a limit
after a downgrade keeps everything it has; it just can't add more until it is
back under, and ``PlanLimitError`` says what to remove.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from .plans import FEATURE_LABELS, PAID_PLANS, PLANS, TRIAL_PLAN, Feature, Plan, lowest_plan_with

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
    features: frozenset[str]
    max_members: int | None = None
    max_bio_pages: int | None = None
    shows_bio_badge: bool = False
    # Per network; 0 = benchmarking is off for the plan.
    max_competitors_per_network: int | None = 0
    max_visibility_prompts: int = 0
    max_short_links_per_month: int | None = None
    max_account_sets: int | None = None
    unlimited: bool = False

    def has(self, feature: Feature | str) -> bool:
        return self.unlimited or str(feature) in self.features


# Self-hosted installs are not plan-limited, but a report still needs a sane ceiling on what it asks the model.
UNLIMITED_VISIBILITY_PROMPTS = 15

UNLIMITED = Entitlements(
    plan=None,
    max_channels=None,
    max_workspaces=None,
    max_scheduled_posts_per_month=None,
    max_api_calls_per_day=None,
    features=frozenset(),
    max_competitors_per_network=None,
    max_visibility_prompts=UNLIMITED_VISIBILITY_PROMPTS,
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
        features=frozenset(str(f) for f in plan.features),
        max_members=plan.max_members,
        max_bio_pages=plan.max_bio_pages,
        shows_bio_badge=plan.bio_badge,
        max_competitors_per_network=plan.max_competitors_per_network,
        max_visibility_prompts=plan.max_visibility_prompts,
        max_short_links_per_month=plan.max_short_links_per_month,
        max_account_sets=plan.max_account_sets,
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

    # The example workspace's placeholder channel is not the customer's.
    return SocialAccount.objects.filter(workspace__organization=org, workspace__sample_marker__isnull=True).count()


def member_count(org) -> int:
    """People in the organization, owner and client-role users included."""
    from apps.members.models import OrgMembership

    return OrgMembership.objects.filter(organization=org).count()


def pending_invitation_count(org) -> int:
    from apps.members.models import Invitation

    return Invitation.objects.filter(organization=org, accepted_at__isnull=True, expires_at__gt=timezone.now()).count()


def bio_page_count(org) -> int:
    from apps.linkinbio.models import BioPage

    return BioPage.objects.filter(workspace__organization=org).count()


def workspace_count(org) -> int:
    from apps.workspaces.models import Workspace

    return Workspace.objects.filter(organization=org, is_archived=False, sample_marker__isnull=True).count()


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


def usage_summary(org) -> list[dict]:
    """Usage against each limit, for the Plan & billing page."""
    ent = get_entitlements(org)
    rows = [
        ("channels", "Channels", channel_count(org), ent.max_channels, "Disconnect channels"),
        ("workspaces", "Workspaces", workspace_count(org), ent.max_workspaces, "Archive workspaces"),
        ("members", "Team members", member_count(org), ent.max_members, "Remove members"),
        (
            "posts",
            "Scheduled posts this month",
            scheduled_post_count(org),
            ent.max_scheduled_posts_per_month,
            "Move posts to next month or back to drafts",
        ),
        # Resets every UTC day, so there is never anything to remove.
        ("api_calls", "API & MCP calls today", api_call_count(org), ent.max_api_calls_per_day, ""),
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


def short_links_this_month(org) -> int:
    from apps.links.models import ShortLink

    start = timezone.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return ShortLink.objects.filter(workspace__organization=org, created_at__gte=start).count()


def check_can_create_short_link(org) -> None:
    ent = get_entitlements(org)
    if ent.max_short_links_per_month is None:
        return
    used = short_links_this_month(org)
    if used < ent.max_short_links_per_month:
        return
    raise PlanLimitError(
        f"Your {ent.plan.name} plan includes {ent.max_short_links_per_month} short links a month and this "
        f"organization has made {used}. Upgrade your plan to make more.",
        code="short_link_limit",
    )


def account_set_count(org) -> int:
    from apps.account_sets.models import AccountSet

    return AccountSet.objects.filter(workspace__organization=org).count()


def check_can_add_account_set(org) -> None:
    ent = get_entitlements(org)
    if ent.max_account_sets is None:
        return
    used = account_set_count(org)
    if used < ent.max_account_sets:
        return
    raise PlanLimitError(
        f"Your {ent.plan.name} plan includes {ent.max_account_sets} saved account sets and this organization "
        f"has {used}. {_remove_hint(used, ent.max_account_sets, 'set', 'Delete')} or upgrade your plan to add more.",
        code="account_set_limit",
    )


def check_can_add_bio_page(org) -> None:
    ent = get_entitlements(org)
    if ent.max_bio_pages is None:
        return
    used = bio_page_count(org)
    if used < ent.max_bio_pages:
        return
    raise PlanLimitError(
        f"Your {ent.plan.name} plan includes {ent.max_bio_pages} link-in-bio "
        f"page{'s' if ent.max_bio_pages != 1 else ''} and this organization has {used}. "
        f"{_remove_hint(used, ent.max_bio_pages, 'page', 'Delete')} or upgrade your plan to add more.",
        code="bio_page_limit",
    )


def competitor_count(workspace, platform: str) -> int:
    from apps.competitors.models import Competitor

    return Competitor.objects.filter(workspace=workspace, platform=platform).count()


def check_can_add_competitor(workspace, platform: str) -> None:
    """Refuse when the workspace already tracks its plan's limit of competitors on ``platform``."""
    ent = get_entitlements(workspace.organization)
    if ent.max_competitors_per_network is None:
        return
    limit = ent.max_competitors_per_network
    plan_name = ent.plan.name if ent.plan else "current"
    if limit == 0:
        raise PlanLimitError(
            f"Competitor benchmarking isn't included in your {plan_name} plan. "
            f"Upgrade to {PLANS[PAID_PLANS[0]].name} or higher to track competitors.",
            code="competitor_limit",
        )
    used = competitor_count(workspace, platform)
    if used < limit:
        return
    raise PlanLimitError(
        f"Your {plan_name} plan includes {limit} competitor{'s' if limit != 1 else ''} per network and this "
        f"workspace tracks {used} here. {_remove_hint(used, limit, 'competitor', 'Remove')} or upgrade your plan to add more.",
        code="competitor_limit",
    )


def check_can_add_member(org, *, pending_invitation=None) -> None:
    """Refuse another person in the organization once the plan's member limit is reached.

    Inviting counts the invitations still waiting as well as the members, so
    a plan can't be overrun by sending many at once. Accepting
    ``pending_invitation`` counts members only: its seat was taken when it was
    sent, unless the organization has since downgraded below it.
    """
    ent = get_entitlements(org)
    if ent.max_members is None:
        return
    used = member_count(org)
    if pending_invitation is None:
        used += pending_invitation_count(org)
    if used < ent.max_members:
        return
    others = ent.max_members - 1
    counted = "members and pending invitations" if pending_invitation is None else "members"
    raise PlanLimitError(
        f"Your {ent.plan.name} plan includes the owner and {others} other member{'s' if others != 1 else ''} "
        f"(client approvers count), and this organization already has {used} {counted}. "
        f"Upgrade to {PLANS[_first_plan_without_member_limit()].name} or higher for unlimited team members.",
        code="member_limit",
    )


def _first_plan_without_member_limit() -> str:
    return next(slug for slug in PAID_PLANS if PLANS[slug].max_members is None)


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


def _remove_hint(used: int, limit: int, noun: str, verb: str) -> str:
    to_remove = used - limit + 1
    return f"{verb} {to_remove} {noun}{'s' if to_remove != 1 else ''}"
