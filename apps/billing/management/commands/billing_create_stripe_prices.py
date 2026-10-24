"""Create PostDelegate's Products and Prices in a Stripe account.

Usage:
    STRIPE_SECRET_KEY=sk_test_... python manage.py billing_create_stripe_prices

One Product per paid plan ("PostDelegate Starter", "PostDelegate Team", ...),
with a monthly and a yearly Price from the amounts in ``apps/billing/plans.py``.
Safe to run again: each Price carries a ``lookup_key`` and an existing active
one with the right amount is reused rather than duplicated; if the plan's
amount has changed, a new Price is created and the lookup key moves to it.
Prints the ``STRIPE_PRICE_*`` lines to paste into the environment.

Refuses a live-mode key unless ``--live`` is passed, so a test run can't
create real prices by accident.
"""

import stripe
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.billing.plans import PAID_PLANS, PLANS, Interval
from apps.billing.services import HANDLED_EVENT_TYPES

CURRENCY = "usd"
_ENV_SUFFIX = {Interval.MONTH: "MONTHLY", Interval.YEAR: "ANNUAL"}


class Command(BaseCommand):
    help = "Create (or reuse) the Stripe Products and Prices for each paid plan and print the env lines."

    def add_arguments(self, parser):
        parser.add_argument("--live", action="store_true", help="Allow a live-mode Stripe key.")

    def handle(self, *args, **options):
        api_key = settings.STRIPE_SECRET_KEY
        if not api_key:
            raise CommandError("Set STRIPE_SECRET_KEY first (a test-mode sk_test_ key).")
        if "_live_" in api_key and not options["live"]:
            raise CommandError("STRIPE_SECRET_KEY is a live-mode key. Re-run with --live if you really mean it.")

        env_lines = []
        for slug in PAID_PLANS:
            plan = PLANS[slug]
            product_id = None
            for interval in Interval:
                lookup_key = f"postdelegate_{slug}_{interval}"
                amount = plan.price_cents(interval)
                existing = stripe.Price.list(api_key=api_key, lookup_keys=[lookup_key], active=True, limit=1)
                current = existing["data"][0] if existing["data"] else None
                if current is not None and current.get("unit_amount", amount) == amount:
                    price = current
                    self.stdout.write(f"Reusing {lookup_key}: {price['id']}")
                else:
                    if product_id is None:
                        product_id = self._product_id(api_key, plan)
                    price = stripe.Price.create(
                        api_key=api_key,
                        product=product_id,
                        currency=CURRENCY,
                        unit_amount=amount,
                        recurring={"interval": str(interval)},
                        lookup_key=lookup_key,
                        # An old price under this key (a since-changed amount) gives it up.
                        transfer_lookup_key=current is not None,
                        metadata={"plan": slug, "plan_name": plan.name, "interval": str(interval)},
                    )
                    self.stdout.write(f"Created {lookup_key}: {price['id']}")
                env_lines.append(f"STRIPE_PRICE_{slug.upper()}_{_ENV_SUFFIX[interval]}={price['id']}")

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("Add these to the environment:"))
        for line in env_lines:
            self.stdout.write(line)
        self.stdout.write("")
        self.stdout.write(
            f"Then add a webhook endpoint at {settings.APP_URL.rstrip('/')}/webhooks/stripe/ for: "
            + ", ".join(HANDLED_EVENT_TYPES)
            + " — and set STRIPE_WEBHOOK_SECRET to its signing secret."
        )

    def _product_id(self, api_key: str, plan) -> str:
        found = stripe.Product.search(
            api_key=api_key, query=f"metadata['plan']:'{plan.slug}' AND active:'true'", limit=1
        )
        if found["data"]:
            return found["data"][0]["id"]
        product = stripe.Product.create(
            api_key=api_key,
            name=f"{settings.BRAND_NAME} {plan.name}",
            description=plan.tagline,
            metadata={"plan": plan.slug, "plan_name": plan.name},
        )
        return product["id"]
