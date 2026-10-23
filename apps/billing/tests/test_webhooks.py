"""The Stripe webhook: signature, idempotency, and each lifecycle event.

Signatures are computed with the real scheme and verified by the real
``stripe.Webhook``; the only Stripe API call (retrieving a subscription after
checkout) is mocked.
"""

from __future__ import annotations

import json
import time
from datetime import timedelta
from unittest import mock

import pytest
from django.core import mail
from django.test import Client
from django.utils import timezone

from apps.billing.models import ProcessedStripeEvent, Subscription
from apps.notifications.models import Notification

from .support import post_event, set_plan, stripe_signature

NOW = int(time.time())


def _subscription(status="active", price="price_team_m", sub_id="sub_1", **extra) -> dict:
    return {
        "id": sub_id,
        "object": "subscription",
        "customer": "cus_1",
        "status": status,
        "cancel_at_period_end": False,
        "trial_end": None,
        "metadata": {},
        "items": {"data": [{"id": "si_1", "price": {"id": price}, "current_period_end": NOW + 30 * 86400}]},
        **extra,
    }


def _event(event_type: str, obj: dict, event_id: str = "evt_1", created: int = NOW) -> dict:
    return {"id": event_id, "type": event_type, "created": created, "data": {"object": obj}}


@pytest.fixture
def stripe_sub(billing, organization):
    """An organization already linked to Stripe subscription ``sub_1``."""
    return set_plan(organization, "team", stripe_customer_id="cus_1", stripe_subscription_id="sub_1")


@pytest.mark.django_db
class TestVerification:
    def test_bad_signature_is_rejected_and_nothing_applied(self, stripe_sub):
        event = _event("customer.subscription.deleted", _subscription(status="canceled"))
        response = post_event(Client(), event, signature="t=1,v1=deadbeef")
        assert response.status_code == 400
        assert not ProcessedStripeEvent.objects.exists()
        stripe_sub.refresh_from_db()
        assert stripe_sub.status == Subscription.Status.ACTIVE

    def test_signature_with_the_wrong_secret_is_rejected(self, stripe_sub):
        event = _event("customer.subscription.deleted", _subscription(status="canceled"))
        payload_sig = stripe_signature(b"{}", secret="whsec_other")
        assert post_event(Client(), event, signature=payload_sig).status_code == 400

    def test_stale_timestamp_is_rejected(self, stripe_sub):
        event = _event("customer.subscription.deleted", _subscription(status="canceled"))
        payload = json.dumps(event).encode()
        old = stripe_signature(payload, timestamp=int(time.time()) - 3600)
        assert post_event(Client(), event, signature=old).status_code == 400

    def test_missing_secret_refuses(self, stripe_sub, settings):
        settings.STRIPE_WEBHOOK_SECRET = ""
        event = _event("customer.subscription.updated", _subscription())
        assert post_event(Client(), event).status_code == 403

    def test_not_routed_when_billing_disabled(self, db, settings):
        settings.BILLING_ENABLED = False
        assert post_event(Client(), _event("customer.subscription.updated", _subscription())).status_code == 404


@pytest.mark.django_db
class TestIdempotency:
    def test_redelivered_event_is_applied_once(self, stripe_sub, org_owner, django_capture_on_commit_callbacks):
        event = _event("invoice.payment_failed", {"id": "in_1", "customer": "cus_1", "subscription": "sub_1"})
        client = Client()
        with django_capture_on_commit_callbacks(execute=True):
            assert post_event(client, event).status_code == 200
            assert post_event(client, event).status_code == 200
        assert ProcessedStripeEvent.objects.filter(event_id="evt_1").count() == 1
        # One notification for the one owner, not two.
        assert Notification.objects.filter(event_type="billing_payment_failed").count() == 1

    def test_failed_handling_is_retried(self, billing, organization):
        set_plan(organization, "team", stripe_customer_id="cus_1")
        event = _event(
            "checkout.session.completed",
            {
                "id": "cs_1",
                "mode": "subscription",
                "customer": "cus_1",
                "subscription": "sub_1",
                "client_reference_id": str(organization.id),
            },
        )
        with mock.patch("apps.billing.services.stripe") as stripe_mock:
            stripe_mock.Subscription.retrieve.side_effect = RuntimeError("stripe down")
            with pytest.raises(RuntimeError):
                post_event(Client(raise_request_exception=True), event)
        # Rolled back with the failure, so Stripe's retry gets processed.
        assert not ProcessedStripeEvent.objects.exists()

    def test_out_of_order_snapshot_is_ignored(self, stripe_sub):
        client = Client()
        post_event(client, _event("customer.subscription.updated", _subscription(price="price_agency_m"), "evt_new"))
        stale = _event("customer.subscription.updated", _subscription(price="price_creator_m"), "evt_old", NOW - 60)
        post_event(client, stale)
        stripe_sub.refresh_from_db()
        assert stripe_sub.plan == "agency"


@pytest.mark.django_db
class TestLifecycle:
    def test_checkout_completed_links_and_syncs_the_subscription(self, billing, organization):
        event = _event(
            "checkout.session.completed",
            {
                "id": "cs_1",
                "mode": "subscription",
                "customer": "cus_9",
                "subscription": "sub_9",
                "client_reference_id": str(organization.id),
            },
        )
        with mock.patch("apps.billing.services.stripe") as stripe_mock:
            stripe_mock.Subscription.retrieve.return_value = _subscription(
                sub_id="sub_9", customer="cus_9", price="price_agency_y"
            )
            assert post_event(Client(), event).status_code == 200
        stripe_mock.Subscription.retrieve.assert_called_once_with("sub_9", api_key="sk_test_fake")
        sub = Subscription.objects.get(organization=organization)
        assert (sub.plan, sub.interval, sub.status) == ("agency", "year", Subscription.Status.ACTIVE)
        assert (sub.stripe_customer_id, sub.stripe_subscription_id) == ("cus_9", "sub_9")
        assert sub.current_period_end is not None

    def test_subscription_created_finds_org_by_metadata(self, billing, organization):
        obj = _subscription(
            sub_id="sub_new", price="price_creator_m", metadata={"organization_id": str(organization.id)}
        )
        post_event(Client(), _event("customer.subscription.created", obj))
        sub = Subscription.objects.get(organization=organization)
        assert (sub.plan, sub.stripe_subscription_id) == ("creator", "sub_new")

    def test_subscription_updated_changes_plan_and_cancellation(self, stripe_sub):
        obj = _subscription(price="price_agency_m", cancel_at_period_end=True)
        post_event(Client(), _event("customer.subscription.updated", obj))
        stripe_sub.refresh_from_db()
        assert stripe_sub.plan == "agency" and stripe_sub.cancel_at_period_end

    def test_stripe_trial_keeps_the_trial_end(self, stripe_sub):
        trial_end = NOW + 5 * 86400
        post_event(
            Client(), _event("customer.subscription.updated", _subscription(status="trialing", trial_end=trial_end))
        )
        stripe_sub.refresh_from_db()
        assert stripe_sub.status == Subscription.Status.TRIALING
        assert int(stripe_sub.trial_ends_at.timestamp()) == trial_end

    def test_unknown_price_leaves_the_plan_alone(self, stripe_sub):
        post_event(Client(), _event("customer.subscription.updated", _subscription(price="price_mystery")))
        stripe_sub.refresh_from_db()
        assert stripe_sub.plan == "team"

    def test_subscription_deleted_drops_to_free(self, stripe_sub):
        post_event(Client(), _event("customer.subscription.deleted", _subscription(status="canceled")))
        stripe_sub.refresh_from_db()
        assert stripe_sub.status == Subscription.Status.CANCELED
        assert stripe_sub.effective_plan().slug == "free"
        assert stripe_sub.stripe_subscription_id == ""
        # The customer is kept so a later checkout reuses it.
        assert stripe_sub.stripe_customer_id == "cus_1"

    def test_payment_failed_starts_grace_and_notifies_admins(
        self, stripe_sub, org_owner, django_capture_on_commit_callbacks
    ):
        invoice = {"id": "in_1", "customer": "cus_1", "parent": {"subscription_details": {"subscription": "sub_1"}}}
        with django_capture_on_commit_callbacks(execute=True):
            post_event(Client(), _event("invoice.payment_failed", invoice))
        stripe_sub.refresh_from_db()
        assert stripe_sub.status == Subscription.Status.PAST_DUE
        assert stripe_sub.grace_until > timezone.now() + timedelta(days=6)
        # Still on the paid plan during the grace period.
        assert stripe_sub.effective_plan().slug == "team"

        notification = Notification.objects.get(user=org_owner, event_type="billing_payment_failed")
        assert "Update your payment method" in notification.body
        assert notification.data["action_url"] == "https://studio.example.com/organizations/billing/"
        assert [m.to for m in mail.outbox] == [[org_owner.email]]

    def test_recovery_clears_the_grace_period(self, stripe_sub):
        stripe_sub.status = Subscription.Status.PAST_DUE
        stripe_sub.grace_until = timezone.now() + timedelta(days=3)
        stripe_sub.save()
        post_event(Client(), _event("customer.subscription.updated", _subscription(status="active")))
        stripe_sub.refresh_from_db()
        assert stripe_sub.status == Subscription.Status.ACTIVE and stripe_sub.grace_until is None

    def test_unhandled_event_is_acknowledged(self, stripe_sub):
        assert post_event(Client(), _event("charge.refunded", {"id": "ch_1"})).status_code == 200
        assert ProcessedStripeEvent.objects.filter(event_type="charge.refunded").exists()

    def test_end_of_a_superseded_subscription_keeps_the_current_one(self, stripe_sub):
        # A second subscription (say, from another Checkout tab) is cancelled.
        post_event(
            Client(), _event("customer.subscription.deleted", _subscription(status="canceled", sub_id="sub_extra"))
        )
        stripe_sub.refresh_from_db()
        assert stripe_sub.status == Subscription.Status.ACTIVE
        assert (stripe_sub.plan, stripe_sub.stripe_subscription_id) == ("team", "sub_1")

    def test_late_payment_failure_after_recovery_is_ignored(self, stripe_sub):
        client = Client()
        post_event(client, _event("customer.subscription.updated", _subscription(status="active"), "evt_recovered"))
        failed = {"id": "in_1", "customer": "cus_1", "subscription": "sub_1"}
        post_event(client, _event("invoice.payment_failed", failed, "evt_failed", NOW - 60))
        stripe_sub.refresh_from_db()
        assert stripe_sub.status == Subscription.Status.ACTIVE and stripe_sub.grace_until is None

    def test_payment_failure_on_an_incomplete_subscription_grants_no_grace(self, stripe_sub):
        stripe_sub.status = Subscription.Status.INCOMPLETE
        stripe_sub.save()
        failed = {"id": "in_1", "customer": "cus_1", "subscription": "sub_1"}
        post_event(Client(), _event("invoice.payment_failed", failed))
        stripe_sub.refresh_from_db()
        assert stripe_sub.status == Subscription.Status.INCOMPLETE
        assert stripe_sub.effective_plan().slug == "free"
