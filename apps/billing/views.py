import json
import logging

import stripe
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse, HttpResponseBadRequest, HttpResponseForbidden
from django.shortcuts import redirect, render
from django.template.defaultfilters import pluralize
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST
from django_ratelimit.decorators import ratelimit

from apps.members.decorators import require_org_role
from apps.members.models import OrgMembership

from . import services
from .entitlements import billing_enabled, get_subscription, usage_summary
from .plans import FEATURE_LABELS, PAID_PLANS, PLAN_ORDER, PLANS, Interval

logger = logging.getLogger(__name__)


def _require_billing_enabled():
    if not billing_enabled():
        raise Http404


def _limit_lines(plan) -> list[str]:
    def count(limit, noun):
        return f"Unlimited {noun}s" if limit is None else f"{limit} {noun}{pluralize(limit)}"

    return [
        count(plan.max_channels, "channel"),
        count(plan.max_workspaces, "workspace"),
        "Unlimited scheduled posts"
        if plan.max_scheduled_posts_per_month is None
        else f"{plan.max_scheduled_posts_per_month} scheduled posts / month",
        "Unlimited API & MCP calls"
        if plan.max_api_calls_per_day is None
        else f"{plan.max_api_calls_per_day:,} API & MCP calls / day",
        "Unlimited team members",
    ]


@login_required
@require_org_role(OrgMembership.OrgRole.ADMIN)
@require_GET
def overview(request):
    _require_billing_enabled()
    org = request.org
    sub = get_subscription(org)
    current = sub.effective_plan()

    checkout = request.GET.get("checkout")
    if checkout == "success":
        messages.success(request, "Thanks! Your plan updates as soon as Stripe confirms the payment.")
    elif checkout == "canceled":
        messages.info(request, "Checkout canceled. Your plan hasn't changed.")

    current_rank = PLAN_ORDER.index(current.slug)
    plans = []
    for rank, slug in enumerate(PLAN_ORDER):
        plan = PLANS[slug]
        plans.append(
            {
                "plan": plan,
                "monthly_price": plan.price_cents(Interval.MONTH) // 100,
                "annual_price": plan.price_cents(Interval.YEAR) // 100,
                "limits": _limit_lines(plan),
                "features": [label for feature, label in FEATURE_LABELS.items() if plan.has(feature)],
                "is_current": plan.slug == current.slug,
                "is_upgrade": rank > current_rank,
            }
        )

    usage = usage_summary(org)
    context = {
        "settings_active": "billing",
        "subscription": sub,
        "current_plan": current,
        "plans": plans,
        "usage": usage,
        "over_limit": any(row["over_by"] for row in usage),
        "current_interval": sub.interval or Interval.MONTH,
        "stripe_configured": services.stripe_configured(),
    }
    return render(request, "billing/overview.html", context)


@login_required
@require_org_role(OrgMembership.OrgRole.ADMIN)
@require_POST
@ratelimit(key="user", rate="10/m", method="POST", block=True)
def checkout(request):
    """Subscribe to a paid plan, or move an existing subscription to another one."""
    _require_billing_enabled()
    slug = request.POST.get("plan", "")
    interval = request.POST.get("interval", Interval.MONTH)
    if slug not in PAID_PLANS or interval not in {i.value for i in Interval}:
        messages.error(request, "Choose a plan and billing period.")
        return redirect("billing:overview")

    org = request.org
    plan = PLANS[slug]
    sub = get_subscription(org)
    try:
        if sub.has_paid_subscription:
            services.change_plan(org, plan, interval)
            messages.success(request, f"You're now on the {plan.name} plan. Any difference is prorated.")
            return redirect("billing:overview")
        return redirect(services.create_checkout_session(org, request.user, plan, interval))
    except services.BillingNotConfiguredError:
        logger.exception("Billing is enabled but Stripe isn't configured for %s/%s", slug, interval)
        messages.error(request, "Billing isn't set up on this server yet. Please contact support.")
    except stripe.StripeError:
        logger.exception("Stripe refused a plan change for organization %s", org.id)
        messages.error(request, "We couldn't reach our payment provider. Please try again in a moment.")
    return redirect("billing:overview")


@login_required
@require_org_role(OrgMembership.OrgRole.ADMIN)
@require_POST
@ratelimit(key="user", rate="10/m", method="POST", block=True)
def portal(request):
    """Send the user to Stripe's Customer Portal: card, invoices, cancellation."""
    _require_billing_enabled()
    try:
        return redirect(services.create_portal_session(request.org))
    except services.BillingNotConfiguredError:
        messages.error(request, "There's no billing account for this organization yet. Choose a plan first.")
    except stripe.StripeError:
        logger.exception("Stripe refused a portal session for organization %s", request.org.id)
        messages.error(request, "We couldn't reach our payment provider. Please try again in a moment.")
    return redirect("billing:overview")


@csrf_exempt
@require_POST
@ratelimit(key="ip", rate="120/m", block=True)
def stripe_webhook(request):
    """Apply Stripe subscription events. Every verified event is answered 200.

    Duplicate deliveries are acknowledged without being applied again; an
    error while applying one returns 500 so Stripe retries it.
    """
    if not billing_enabled():
        raise Http404
    if not settings.STRIPE_WEBHOOK_SECRET:
        logger.error("Stripe webhook called but STRIPE_WEBHOOK_SECRET is not configured")
        return HttpResponseForbidden("Webhook secret not configured.")
    try:
        services.construct_event(request.body, request.headers.get("stripe-signature", ""))
    except (ValueError, stripe.SignatureVerificationError):
        logger.warning("Stripe webhook: signature did not verify")
        return HttpResponseBadRequest("Invalid signature.")

    event = json.loads(request.body)
    services.process_event(event)
    return HttpResponse(status=200)
