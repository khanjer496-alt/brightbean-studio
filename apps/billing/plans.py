"""The plans PostDelegate sells, as data.

Every plan is priced per organization and includes unlimited users — seats
are never a limit, so nothing here counts members. A limit of ``None`` means
unlimited.

Nothing outside ``apps.billing`` reads these directly: views, the API and MCP
ask ``apps.billing.entitlements`` what an organization may do, so a plan's
name never appears in a feature check.

Prices are the list prices shown in the app. What a customer is actually
charged comes from the Stripe Price ids in ``settings.STRIPE_PRICE_IDS``,
which ``manage.py billing_create_stripe_prices`` creates from these numbers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from django.conf import settings


class Feature(StrEnum):
    """Capabilities a plan can include, each mapped to an existing surface."""

    APPROVALS = "approvals"  # workspace approval workflows
    INBOX = "inbox"  # unified social inbox
    ANALYTICS = "analytics"  # channel and post analytics
    CLIENT_PORTAL = "client_portal"  # inviting clients to review posts
    BRANDED_REPORTS = "branded_reports"  # reserved: no reports surface exists yet


FEATURE_LABELS = {
    Feature.APPROVALS: "Approval workflows",
    Feature.INBOX: "Social inbox",
    Feature.ANALYTICS: "Analytics",
    Feature.CLIENT_PORTAL: "Client portal",
    Feature.BRANDED_REPORTS: "Branded reports",
}


class Interval(StrEnum):
    MONTH = "month"
    YEAR = "year"


# Annual billing charges ten months: two months free.
ANNUAL_MONTHS_CHARGED = 10


@dataclass(frozen=True)
class Plan:
    slug: str
    name: str
    tagline: str
    monthly_price_cents: int
    max_channels: int | None
    max_workspaces: int | None
    max_scheduled_posts_per_month: int | None
    # Agent API and MCP calls per UTC day, counted across the organization.
    # Agent access is on every plan; plans differ only in how much of it.
    max_api_calls_per_day: int | None
    # AI writing-assistant requests per UTC day, counted across the
    # organization. Requests made with the organization's own provider key
    # don't count — that usage is billed to them, not to us.
    max_ai_requests_per_day: int | None
    features: frozenset[Feature] = field(default_factory=frozenset)

    @property
    def is_free(self) -> bool:
        return self.monthly_price_cents == 0

    def price_cents(self, interval: str) -> int:
        if interval == Interval.YEAR:
            return self.monthly_price_cents * ANNUAL_MONTHS_CHARGED
        return self.monthly_price_cents

    def stripe_price_id(self, interval: str) -> str:
        return settings.STRIPE_PRICE_IDS.get(self.slug, {}).get(str(interval), "")

    def has(self, feature: Feature) -> bool:
        return feature in self.features


FREE = "free"
CREATOR = "creator"
TEAM = "team"
AGENCY = "agency"

PLANS: dict[str, Plan] = {
    FREE: Plan(
        slug=FREE,
        name="Free",
        tagline="For trying PostDelegate on a few channels.",
        monthly_price_cents=0,
        max_channels=3,
        max_workspaces=1,
        max_scheduled_posts_per_month=30,
        max_api_calls_per_day=100,
        max_ai_requests_per_day=20,
    ),
    CREATOR: Plan(
        slug=CREATOR,
        name="Creator",
        tagline="For creators publishing every day.",
        monthly_price_cents=1200,
        max_channels=10,
        max_workspaces=2,
        max_scheduled_posts_per_month=None,
        max_api_calls_per_day=1_000,
        max_ai_requests_per_day=200,
    ),
    TEAM: Plan(
        slug=TEAM,
        name="Team",
        tagline="For teams that review before they publish.",
        monthly_price_cents=2900,
        max_channels=25,
        max_workspaces=5,
        max_scheduled_posts_per_month=None,
        max_api_calls_per_day=10_000,
        max_ai_requests_per_day=1_000,
        features=frozenset({Feature.APPROVALS, Feature.INBOX, Feature.ANALYTICS}),
    ),
    AGENCY: Plan(
        slug=AGENCY,
        name="Agency",
        tagline="For agencies running many clients.",
        monthly_price_cents=7900,
        max_channels=100,
        max_workspaces=None,
        max_scheduled_posts_per_month=None,
        max_api_calls_per_day=100_000,
        max_ai_requests_per_day=5_000,
        features=frozenset(
            {
                Feature.APPROVALS,
                Feature.INBOX,
                Feature.ANALYTICS,
                Feature.CLIENT_PORTAL,
                Feature.BRANDED_REPORTS,
            }
        ),
    ),
}

# The plan a new organization trials, and the order plans are shown in.
TRIAL_PLAN = TEAM
PLAN_ORDER = (FREE, CREATOR, TEAM, AGENCY)
PAID_PLANS = tuple(slug for slug in PLAN_ORDER if not PLANS[slug].is_free)
PLAN_CHOICES = [(slug, PLANS[slug].name) for slug in PLAN_ORDER]


def get_plan(slug: str) -> Plan:
    """The plan for ``slug``; an unknown slug is treated as Free, never as unlimited."""
    return PLANS.get(slug, PLANS[FREE])


def plan_for_price_id(price_id: str) -> tuple[Plan, Interval] | None:
    """Map a Stripe Price id back to the plan and interval it was configured for."""
    if not price_id:
        return None
    for slug in PAID_PLANS:
        plan = PLANS[slug]
        for interval in Interval:
            if plan.stripe_price_id(interval) == price_id:
                return plan, interval
    return None


def lowest_plan_with(feature: Feature) -> Plan | None:
    """The cheapest plan that includes ``feature`` — what upgrade messages point at."""
    for slug in PLAN_ORDER:
        if PLANS[slug].has(feature):
            return PLANS[slug]
    return None
