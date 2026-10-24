"""Stripe calls and webhook event handling.

Everything that talks to Stripe lives here so the views stay thin and the
tests have one module to mock. Webhook payloads are handled as the plain JSON
Stripe sent (after the signature is verified), which keeps the handlers
independent of the stripe library's object types.
"""

from __future__ import annotations

import datetime as dt
import logging

import stripe
from django.conf import settings
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from .entitlements import get_subscription
from .models import LINKED_STATUSES, ProcessedStripeEvent, Subscription
from .plans import FREE, Plan, plan_for_price_id

logger = logging.getLogger(__name__)

# Stripe refuses a trial_end less than 48 hours out.
_MIN_STRIPE_TRIAL = dt.timedelta(hours=49)

_STRIPE_STATUS = {
    "trialing": Subscription.Status.TRIALING,
    "active": Subscription.Status.ACTIVE,
    "past_due": Subscription.Status.PAST_DUE,
    "incomplete": Subscription.Status.INCOMPLETE,
    # Stripe keeps these subscriptions, so they stay linked: ``unpaid`` gets
    # the same grace period as ``past_due`` and then Free, ``paused`` is Free.
    "unpaid": Subscription.Status.UNPAID,
    "paused": Subscription.Status.PAUSED,
    # Everything else has stopped billing: the organization is on Free.
    "canceled": Subscription.Status.CANCELED,
    "incomplete_expired": Subscription.Status.CANCELED,
}


class BillingNotConfiguredError(Exception):
    """Stripe keys or the Price id for a plan are missing from the environment."""


def stripe_configured() -> bool:
    return bool(settings.STRIPE_SECRET_KEY)


def _api_key() -> str:
    if not settings.STRIPE_SECRET_KEY:
        raise BillingNotConfiguredError("STRIPE_SECRET_KEY is not set.")
    return settings.STRIPE_SECRET_KEY


def _as_dict(obj) -> dict:
    return obj if isinstance(obj, dict) else obj.to_dict()


def _from_timestamp(value) -> dt.datetime | None:
    return dt.datetime.fromtimestamp(value, tz=dt.UTC) if value else None


def billing_url(*, absolute: bool = False, query: str = "") -> str:
    url = reverse("billing:overview") + (f"?{query}" if query else "")
    return f"{settings.APP_URL.rstrip('/')}{url}" if absolute else url


# ---------------------------------------------------------------------------
# Outbound: Checkout, plan changes, Customer Portal
# ---------------------------------------------------------------------------


def ensure_customer(org, user) -> str:
    sub = get_subscription(org)
    if sub.stripe_customer_id:
        return sub.stripe_customer_id
    customer = stripe.Customer.create(
        api_key=_api_key(),
        email=org.billing_email or user.email,
        name=org.name,
        metadata={"organization_id": str(org.id)},
    )
    sub.stripe_customer_id = customer["id"]
    sub.save(update_fields=["stripe_customer_id", "updated_at"])
    return sub.stripe_customer_id


def create_checkout_session(org, user, plan: Plan, interval: str) -> str:
    """Start a Stripe Checkout for ``plan`` and return the URL to send the user to."""
    price_id = plan.stripe_price_id(interval)
    if not price_id:
        raise BillingNotConfiguredError(f"No Stripe Price configured for {plan.slug}/{interval}.")
    customer_id = ensure_customer(org, user)
    sub = get_subscription(org)

    subscription_data: dict = {"metadata": {"organization_id": str(org.id)}}
    # Subscribing during the free trial doesn't cut it short: the first charge
    # waits for the day the trial would have ended.
    if sub.is_trialing and sub.trial_ends_at - timezone.now() > _MIN_STRIPE_TRIAL:
        subscription_data["trial_end"] = int(sub.trial_ends_at.timestamp())

    session = stripe.checkout.Session.create(
        api_key=_api_key(),
        mode="subscription",
        customer=customer_id,
        client_reference_id=str(org.id),
        line_items=[{"price": price_id, "quantity": 1}],
        allow_promotion_codes=True,
        subscription_data=subscription_data,
        metadata={"organization_id": str(org.id)},
        success_url=billing_url(absolute=True, query="checkout=success"),
        cancel_url=billing_url(absolute=True, query="checkout=canceled"),
    )
    return session["url"]


def change_plan(org, plan: Plan, interval: str) -> Subscription:
    """Move an existing subscription to another plan, prorating the difference."""
    price_id = plan.stripe_price_id(interval)
    if not price_id:
        raise BillingNotConfiguredError(f"No Stripe Price configured for {plan.slug}/{interval}.")
    sub = get_subscription(org)
    current = _as_dict(stripe.Subscription.retrieve(sub.stripe_subscription_id, api_key=_api_key()))
    item_id = current["items"]["data"][0]["id"]
    updated = stripe.Subscription.modify(
        sub.stripe_subscription_id,
        api_key=_api_key(),
        items=[{"id": item_id, "price": price_id}],
        proration_behavior="create_prorations",
        cancel_at_period_end=False,
    )
    sync_subscription(_as_dict(updated))
    sub.refresh_from_db()
    return sub


def manage_existing_subscription(org) -> str:
    """Where to send an organization that already has a Stripe subscription and wants another plan.

    An ``unpaid`` or ``paused`` subscription can't be re-priced in place, and
    a new Checkout would start a second live subscription next to it, so the
    Customer Portal (resume, pay the open invoice, change plan) is the way.
    """
    return create_portal_session(org)


def cancel_subscription_now(org) -> None:
    """End the organization's subscription today, without a refund, because the organization is being deleted.

    Stripe's own cancel at period end would keep a subscription billing for an
    organization that no longer exists, so this cancels immediately.
    """
    sub = get_subscription(org)
    if not sub.stripe_subscription_id:
        return
    canceled = stripe.Subscription.cancel(sub.stripe_subscription_id, api_key=_api_key())
    sync_subscription(_as_dict(canceled))


def create_portal_session(org) -> str:
    sub = get_subscription(org)
    if not sub.stripe_customer_id:
        raise BillingNotConfiguredError("This organization has no Stripe customer yet.")
    session = stripe.billing_portal.Session.create(
        api_key=_api_key(),
        customer=sub.stripe_customer_id,
        return_url=billing_url(absolute=True),
    )
    return session["url"]


# ---------------------------------------------------------------------------
# Inbound: webhook events
# ---------------------------------------------------------------------------


def construct_event(payload: bytes, signature: str):
    """Verify a webhook's signature; raises ``stripe.SignatureVerificationError``."""
    return stripe.Webhook.construct_event(payload, signature, settings.STRIPE_WEBHOOK_SECRET)


def process_event(event: dict) -> bool:
    """Apply one verified event. Returns False when it was already processed."""
    handler = _HANDLERS.get(event.get("type", ""))
    with transaction.atomic():
        try:
            with transaction.atomic():
                ProcessedStripeEvent.objects.create(event_id=event["id"], event_type=event.get("type", ""))
        except IntegrityError:
            logger.info("Stripe event %s already processed", event["id"])
            return False
        if handler is not None:
            handler(event["data"]["object"], _from_timestamp(event.get("created")))
    return True


def _find_subscription(*, subscription_id: str = "", customer_id: str = "", org_id: str = ""):
    if subscription_id:
        sub = Subscription.objects.filter(stripe_subscription_id=subscription_id).first()
        if sub:
            return sub
    if org_id:
        from apps.organizations.models import Organization

        org = Organization.objects.filter(pk=org_id).first()
        if org:
            return get_subscription(org)
    if customer_id:
        return Subscription.objects.filter(stripe_customer_id=customer_id).first()
    return None


def _subscription_period_end(obj: dict):
    # Newer API versions moved the billing period from the subscription onto
    # its items; read whichever this account's API version sends.
    if obj.get("current_period_end"):
        return _from_timestamp(obj["current_period_end"])
    items = (obj.get("items") or {}).get("data") or []
    return _from_timestamp(items[0].get("current_period_end")) if items else None


@transaction.atomic
def sync_subscription(obj: dict, event_created: dt.datetime | None = None) -> Subscription | None:
    """Copy a Stripe Subscription's state onto the organization's row."""
    sub = _find_subscription(
        subscription_id=obj.get("id", ""),
        customer_id=obj.get("customer", ""),
        org_id=(obj.get("metadata") or {}).get("organization_id", ""),
    )
    if sub is None:
        logger.warning("Stripe subscription %s matches no organization", obj.get("id"))
        return None
    sub = Subscription.objects.select_for_update().get(pk=sub.pk)
    if event_created and sub.stripe_synced_at:
        if event_created < sub.stripe_synced_at:
            logger.info("Ignoring stale Stripe snapshot of %s", obj.get("id"))
            return sub
        if event_created == sub.stripe_synced_at and obj.get("id"):
            # Stripe stamps events to the second, so two events of one
            # subscription can't be ordered by ``created``. Ask Stripe for the
            # subscription as it is now instead of guessing which came first.
            obj = _current_snapshot(obj)

    status = _STRIPE_STATUS.get(obj.get("status", ""), Subscription.Status.CANCELED)
    if (
        status in LINKED_STATUSES
        and sub.stripe_subscription_id
        and obj.get("id")
        and obj["id"] != sub.stripe_subscription_id
        and sub.status in LINKED_STATUSES
    ):
        _refuse_second_subscription(sub, obj["id"])
        return sub
    # The end of a subscription that is no longer this organization's current
    # one — e.g. the extra one from a second Checkout tab, cancelled from the
    # portal — must not downgrade the subscription it is still paying for.
    if (
        status == Subscription.Status.CANCELED
        and sub.stripe_subscription_id
        and obj.get("id")
        and obj["id"] != sub.stripe_subscription_id
    ):
        logger.info("Ignoring end of superseded Stripe subscription %s", obj.get("id"))
        return sub

    items = (obj.get("items") or {}).get("data") or []
    price_id = (items[0].get("price") or {}).get("id", "") if items else ""
    mapped = plan_for_price_id(price_id)

    if status == Subscription.Status.CANCELED:
        sub.plan = FREE
        sub.interval = ""
        sub.stripe_subscription_id = ""
        sub.cancel_at_period_end = False
        sub.current_period_end = None
        sub.grace_until = None
    else:
        if mapped is None:
            logger.error("Stripe price %s is not one of STRIPE_PRICE_IDS; plan left unchanged", price_id)
        else:
            sub.plan, sub.interval = mapped[0].slug, str(mapped[1])
        sub.stripe_subscription_id = obj.get("id", "")
        sub.cancel_at_period_end = bool(obj.get("cancel_at_period_end"))
        sub.current_period_end = _subscription_period_end(obj)
        if status == Subscription.Status.TRIALING:
            sub.trial_ends_at = _from_timestamp(obj.get("trial_end")) or sub.trial_ends_at
        if status in (Subscription.Status.PAST_DUE, Subscription.Status.UNPAID):
            sub.start_grace_period()
        else:
            sub.grace_until = None

    sub.status = status
    if obj.get("customer"):
        sub.stripe_customer_id = obj["customer"]
    if event_created:
        sub.stripe_synced_at = event_created
    sub.save()
    return sub


def _current_snapshot(obj: dict) -> dict:
    """The subscription as Stripe has it now; the event's own copy if Stripe can't be reached."""
    try:
        return _as_dict(stripe.Subscription.retrieve(obj["id"], api_key=_api_key()))
    except (stripe.StripeError, BillingNotConfiguredError):
        logger.warning("Could not re-read Stripe subscription %s; using the event's snapshot", obj["id"])
        return obj


def _refuse_second_subscription(sub: Subscription, other_id: str) -> None:
    """Leave the linked subscription alone and tell the admins another one exists."""
    logger.error(
        "Organization %s already has live Stripe subscription %s; not replacing it with %s",
        sub.organization_id,
        sub.stripe_subscription_id,
        other_id,
    )
    transaction.on_commit(lambda: _notify_second_subscription(sub, other_id))


def _on_checkout_completed(session: dict, event_created) -> None:
    if session.get("mode") != "subscription" or not session.get("subscription"):
        return
    sub = _find_subscription(
        org_id=session.get("client_reference_id") or (session.get("metadata") or {}).get("organization_id", ""),
        customer_id=session.get("customer", ""),
    )
    if sub is None:
        logger.warning("Checkout session %s matches no organization", session.get("id"))
        return
    if (
        sub.stripe_subscription_id
        and sub.stripe_subscription_id != session["subscription"]
        and sub.status in LINKED_STATUSES
    ):
        _refuse_second_subscription(sub, session["subscription"])
        return
    sub.stripe_customer_id = session.get("customer") or sub.stripe_customer_id
    sub.stripe_subscription_id = session["subscription"]
    sub.save(update_fields=["stripe_customer_id", "stripe_subscription_id", "updated_at"])
    # The session doesn't say which price was bought; the subscription does.
    stripe_sub = stripe.Subscription.retrieve(session["subscription"], api_key=_api_key())
    sync_subscription(_as_dict(stripe_sub), event_created)


def _on_subscription_changed(obj: dict, event_created) -> None:
    sync_subscription(obj, event_created)


def _on_payment_failed(invoice: dict, event_created) -> None:
    subscription_id = invoice.get("subscription") or (
        ((invoice.get("parent") or {}).get("subscription_details") or {}).get("subscription", "")
    )
    sub = _find_subscription(subscription_id=subscription_id or "", customer_id=invoice.get("customer", ""))
    if sub is None or not sub.stripe_subscription_id:
        return
    if subscription_id and subscription_id != sub.stripe_subscription_id:
        return
    # A subscription whose first payment never went through has nothing to
    # keep: a grace period would hand out the paid plan without a payment.
    if sub.status in (Subscription.Status.INCOMPLETE, Subscription.Status.CANCELED):
        return
    # A failure delivered after a newer snapshot that has moved on (the retry
    # that then succeeded, or the cancellation) must not put the row back into
    # a grace period. A newer past_due snapshot still gets the notification.
    stale = event_created and sub.stripe_synced_at and event_created < sub.stripe_synced_at
    if stale and sub.status != Subscription.Status.PAST_DUE:
        logger.info("Ignoring stale payment failure for %s", sub.stripe_subscription_id)
        return
    sub.status = Subscription.Status.PAST_DUE
    sub.start_grace_period()
    sub.save(update_fields=["status", "grace_until", "updated_at"])
    transaction.on_commit(lambda: _notify_payment_failed(sub))


def _billing_admins(org):
    from apps.members.models import OrgMembership

    return OrgMembership.objects.filter(
        organization=org,
        org_role__in=[OrgMembership.OrgRole.OWNER, OrgMembership.OrgRole.ADMIN],
    ).select_related("user")


def _notify_second_subscription(sub: Subscription, other_id: str) -> None:
    from apps.notifications.engine import notify
    from apps.notifications.models import EventType

    org = sub.organization
    for membership in _billing_admins(org):
        notify(
            user=membership.user,
            event_type=EventType.BILLING_PAYMENT_FAILED,
            title=f"Second subscription found for {org.name}",
            body=(
                f"{org.name} already has a live subscription, and Stripe now shows another one ({other_id}). "
                "We kept the first. Cancel the extra subscription from the billing portal so you aren't charged twice."
            ),
            data={"organization_id": str(org.id), "action_url": billing_url(absolute=True)},
        )


def _notify_payment_failed(sub: Subscription) -> None:
    from apps.notifications.engine import notify
    from apps.notifications.models import EventType

    org = sub.organization
    plan_name = sub.effective_plan().name
    admins = _billing_admins(org)
    grace_until = timezone.localtime(sub.grace_until)
    deadline = f"{grace_until:%B} {grace_until.day}"
    for membership in admins:
        notify(
            user=membership.user,
            event_type=EventType.BILLING_PAYMENT_FAILED,
            title=f"Payment failed for {org.name}",
            body=(
                f"We couldn't charge the card for your {plan_name} plan. Update your payment method by "
                f"{deadline} to keep it; after that {org.name} moves to the Free plan. Nothing is deleted."
            ),
            data={"organization_id": str(org.id), "action_url": billing_url(absolute=True)},
        )


_HANDLERS = {
    "checkout.session.completed": _on_checkout_completed,
    "customer.subscription.created": _on_subscription_changed,
    "customer.subscription.updated": _on_subscription_changed,
    "customer.subscription.deleted": _on_subscription_changed,
    "invoice.payment_failed": _on_payment_failed,
}
HANDLED_EVENT_TYPES = tuple(_HANDLERS)
