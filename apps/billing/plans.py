"""The plans PostDelegate sells, as data.

Every plan is priced per organization. Paid plans include every feature and
differ only in how much: channels, workspaces, team members and the usage
allowances. AI is not part of any plan: every AI feature runs on the
organization's own AI connection (``apps.ai_writer``), so there is no AI
allowance here — only the per-organization safety cap in settings. Free
keeps the advanced features (approvals, inbox, analytics, the client portal,
white-label reports, competitors, AI visibility) locked.
A limit of ``None`` means unlimited.

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
    BRANDED_REPORTS = "branded_reports"  # scheduled, white-label client reports (apps.reports)
    AI_VISIBILITY = "ai_visibility"  # weekly "how AI describes your brand" report (apps.ai_visibility)


FEATURE_LABELS = {
    Feature.APPROVALS: "Approval workflows",
    Feature.INBOX: "Social inbox",
    Feature.ANALYTICS: "Analytics",
    Feature.CLIENT_PORTAL: "Client portal",
    Feature.BRANDED_REPORTS: "Branded reports",
    Feature.AI_VISIBILITY: "AI visibility report",
}


class Interval(StrEnum):
    MONTH = "month"
    YEAR = "year"


# Every paid plan includes every feature: paid plans differ only in how much
# (channels, workspaces, members and the daily or monthly allowances).
ALL_FEATURES = frozenset(Feature)


@dataclass(frozen=True)
class Plan:
    slug: str
    name: str
    tagline: str
    monthly_price_cents: int
    # What one year costs when billed yearly (20% off the monthly price).
    annual_price_cents: int
    max_channels: int | None
    max_workspaces: int | None
    max_scheduled_posts_per_month: int | None
    # Agent API and MCP calls per UTC day, counted across the organization.
    # Agent access is on every plan; plans differ only in how much of it.
    max_api_calls_per_day: int | None
    # People in the organization, owner included; client-role users count.
    # Members already over the limit after a downgrade are kept; only new
    # invitations are refused.
    max_members: int | None = None
    features: frozenset[Feature] = field(default_factory=frozenset)
    # Link-in-bio pages a workspace's organization may keep (apps.linkinbio), and whether
    # its public pages carry the product's badge. Free: one badged page; paid plans: more, unbranded.
    max_bio_pages: int | None = 1
    bio_badge: bool = False
    # Public competitor profiles a workspace may track on each network that
    # supports it (apps.competitors); 0 turns benchmarking off.
    max_competitors_per_network: int | None = 0
    # Category prompts a workspace may have the AI visibility report ask about
    # (apps.ai_visibility); only meaningful on a plan with Feature.AI_VISIBILITY.
    max_visibility_prompts: int = 0
    # Built-in or external short links created per calendar month, and saved account sets
    # (apps.links, apps.account_sets). None = unlimited.
    max_short_links_per_month: int | None = None
    max_account_sets: int | None = None

    @property
    def is_free(self) -> bool:
        return self.monthly_price_cents == 0

    @property
    def annual_monthly_price_cents(self) -> int:
        """The yearly price spread over twelve months, as shown beside the monthly one."""
        return self.annual_price_cents // 12

    def price_cents(self, interval: str) -> int:
        if interval == Interval.YEAR:
            return self.annual_price_cents
        return self.monthly_price_cents

    def stripe_price_id(self, interval: str) -> str:
        return settings.STRIPE_PRICE_IDS.get(self.slug, {}).get(str(interval), "")

    def has(self, feature: Feature) -> bool:
        return feature in self.features


FREE = "free"
STARTER = "starter"
TEAM = "team"
PRO = "pro"
ULTIMATE = "ultimate"

# Allowances scale with the plan: Starter is the base, Team 2x, Pro 5x and
# Ultimate 10x. Free is a small taste of each.
PLANS: dict[str, Plan] = {
    FREE: Plan(
        slug=FREE,
        name="Free",
        tagline="For trying PostDelegate on a few channels.",
        monthly_price_cents=0,
        annual_price_cents=0,
        max_channels=3,
        max_workspaces=1,
        max_scheduled_posts_per_month=30,
        max_api_calls_per_day=100,
        max_members=2,
        max_bio_pages=1,
        bio_badge=True,
        max_competitors_per_network=0,
        max_short_links_per_month=100,
        max_account_sets=2,
    ),
    STARTER: Plan(
        slug=STARTER,
        name="Starter",
        tagline="For one brand and a client to approve.",
        monthly_price_cents=1900,
        annual_price_cents=18000,
        max_channels=5,
        max_workspaces=2,
        max_scheduled_posts_per_month=None,
        max_api_calls_per_day=2_000,
        max_members=2,
        features=ALL_FEATURES,
        max_bio_pages=3,
        max_competitors_per_network=3,
        max_visibility_prompts=3,
        max_short_links_per_month=2_000,
        max_account_sets=10,
    ),
    TEAM: Plan(
        slug=TEAM,
        name="Team",
        tagline="For teams that review before they publish.",
        monthly_price_cents=2900,
        annual_price_cents=27600,
        max_channels=10,
        max_workspaces=5,
        max_scheduled_posts_per_month=None,
        max_api_calls_per_day=4_000,
        features=ALL_FEATURES,
        max_bio_pages=6,
        max_competitors_per_network=6,
        max_visibility_prompts=5,
        max_short_links_per_month=4_000,
        max_account_sets=20,
    ),
    PRO: Plan(
        slug=PRO,
        name="Pro",
        tagline="For growing teams and small agencies.",
        monthly_price_cents=3900,
        annual_price_cents=37200,
        max_channels=30,
        max_workspaces=15,
        max_scheduled_posts_per_month=None,
        max_api_calls_per_day=10_000,
        features=ALL_FEATURES,
        max_bio_pages=15,
        max_competitors_per_network=15,
        max_visibility_prompts=10,
        max_short_links_per_month=10_000,
        max_account_sets=50,
    ),
    ULTIMATE: Plan(
        slug=ULTIMATE,
        name="Ultimate",
        tagline="For agencies running many clients.",
        monthly_price_cents=7900,
        annual_price_cents=75600,
        max_channels=100,
        max_workspaces=None,
        max_scheduled_posts_per_month=None,
        max_api_calls_per_day=20_000,
        features=ALL_FEATURES,
        max_bio_pages=30,
        max_competitors_per_network=30,
        max_visibility_prompts=15,
        max_short_links_per_month=20_000,
        max_account_sets=100,
    ),
}

# The plan a new organization trials (no card), and the order plans are shown in.
TRIAL_PLAN = ULTIMATE
PLAN_ORDER = (FREE, STARTER, TEAM, PRO, ULTIMATE)
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
