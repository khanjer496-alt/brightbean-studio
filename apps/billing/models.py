import uuid
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

from .plans import FREE, PLAN_CHOICES, Interval, Plan, get_plan


class Subscription(models.Model):
    """An organization's plan, mirrored from Stripe.

    Stripe is the source of truth for anything paid; this row is the local
    copy the entitlement checks read, kept current by the webhook. An
    organization without a Stripe subscription is either trialing (no card
    taken) or on Free.
    """

    class Status(models.TextChoices):
        TRIALING = "trialing", "Trialing"
        ACTIVE = "active", "Active"
        PAST_DUE = "past_due", "Past due"
        # Stripe gave up collecting (``unpaid``) or the subscription is
        # ``paused``. Both stay linked to the Stripe subscription so a new
        # Checkout can't start a second one; see ``effective_plan``.
        UNPAID = "unpaid", "Unpaid"
        PAUSED = "paused", "Paused"
        INCOMPLETE = "incomplete", "Incomplete"
        CANCELED = "canceled", "Canceled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.OneToOneField(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="billing_subscription",
    )
    plan = models.CharField(max_length=20, choices=PLAN_CHOICES, default=FREE)
    interval = models.CharField(max_length=10, choices=[(i.value, i.value) for i in Interval], blank=True, default="")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.TRIALING)
    trial_ends_at = models.DateTimeField(null=True, blank=True)
    current_period_end = models.DateTimeField(null=True, blank=True)
    cancel_at_period_end = models.BooleanField(default=False)
    # Set when a renewal payment fails: the paid plan is honoured until then
    # while Stripe retries the card.
    grace_until = models.DateTimeField(null=True, blank=True)

    stripe_customer_id = models.CharField(max_length=255, blank=True, default="", db_index=True)
    stripe_subscription_id = models.CharField(max_length=255, blank=True, default="", db_index=True)
    # ``created`` of the newest Stripe event applied to this row. Stripe does
    # not deliver events in order, so an older subscription snapshot arriving
    # late must not overwrite a newer one.
    stripe_synced_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "billing_subscription"

    def __str__(self):
        return f"{self.organization_id}: {self.plan} ({self.status})"

    @property
    def is_trialing(self) -> bool:
        return (
            self.status == self.Status.TRIALING
            and self.trial_ends_at is not None
            and self.trial_ends_at > timezone.now()
        )

    @property
    def in_grace_period(self) -> bool:
        return (
            self.status in (self.Status.PAST_DUE, self.Status.UNPAID)
            and self.grace_until is not None
            and self.grace_until > timezone.now()
        )

    @property
    def has_paid_subscription(self) -> bool:
        return bool(self.stripe_subscription_id) and self.status in (
            self.Status.ACTIVE,
            self.Status.TRIALING,
            self.Status.PAST_DUE,
        )

    @property
    def has_linked_subscription(self) -> bool:
        """A Stripe subscription that still exists and could bill, whatever it currently allows."""
        return bool(self.stripe_subscription_id) and self.status in LINKED_STATUSES

    @property
    def trial_days_left(self) -> int:
        if not self.is_trialing:
            return 0
        remaining = self.trial_ends_at - timezone.now()
        return max(1, remaining.days + (1 if remaining.seconds else 0))

    def effective_plan(self) -> Plan:
        """The plan whose limits apply right now.

        ``past_due`` and ``unpaid`` keep the paid plan until the grace period
        ends, then fall to Free; ``paused`` is Free straight away.
        """
        if self.status == self.Status.ACTIVE:
            return get_plan(self.plan)
        if self.status == self.Status.TRIALING and (self.stripe_subscription_id or self.is_trialing):
            return get_plan(self.plan)
        if self.in_grace_period:
            return get_plan(self.plan)
        return get_plan(FREE)

    def start_grace_period(self) -> None:
        if self.grace_until is None:
            self.grace_until = timezone.now() + timedelta(days=settings.BILLING_GRACE_PERIOD_DAYS)


# Statuses of a row whose Stripe subscription is still alive, so a second one
# must not be started for the organization.
LINKED_STATUSES = (
    Subscription.Status.ACTIVE,
    Subscription.Status.TRIALING,
    Subscription.Status.PAST_DUE,
    Subscription.Status.UNPAID,
    Subscription.Status.PAUSED,
)


class ProcessedStripeEvent(models.Model):
    """A Stripe webhook event already applied, so a redelivery is a no-op.

    Written in the same transaction as the event's effects: if handling
    fails, the row rolls back with them and Stripe's retry is processed.
    """

    event_id = models.CharField(max_length=255, primary_key=True)
    event_type = models.CharField(max_length=100)
    processed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "billing_processed_stripe_event"

    def __str__(self):
        return f"{self.event_type} {self.event_id}"
