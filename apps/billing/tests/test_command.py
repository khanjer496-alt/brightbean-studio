"""``billing_create_stripe_prices`` — run against a mocked Stripe."""

from __future__ import annotations

from io import StringIO
from unittest import mock

import pytest
from django.core.management import CommandError, call_command

COMMAND_STRIPE = "apps.billing.management.commands.billing_create_stripe_prices.stripe"


def _run(**options) -> str:
    out = StringIO()
    call_command("billing_create_stripe_prices", stdout=out, **options)
    return out.getvalue()


class TestCreateStripePrices:
    def test_creates_a_product_and_two_prices_per_paid_plan(self, settings):
        settings.STRIPE_SECRET_KEY = "sk_test_fake"
        with mock.patch(COMMAND_STRIPE) as stripe_mock:
            stripe_mock.Price.list.return_value = {"data": []}
            stripe_mock.Product.search.return_value = {"data": []}
            stripe_mock.Product.create.side_effect = lambda **kw: {"id": f"prod_{kw['metadata']['plan']}"}
            stripe_mock.Price.create.side_effect = lambda **kw: {"id": f"price_{kw['lookup_key']}"}
            output = _run()

        assert stripe_mock.Product.create.call_count == 4
        names = [kw["name"] for _, kw in stripe_mock.Product.create.call_args_list]
        assert names == [f"{settings.BRAND_NAME} {n}" for n in ("Starter", "Team", "Pro", "Ultimate")]
        amounts = {kw["lookup_key"]: kw["unit_amount"] for _, kw in stripe_mock.Price.create.call_args_list}
        assert amounts == {
            "postdelegate_starter_month": 1900,
            "postdelegate_starter_year": 18000,
            "postdelegate_team_month": 2900,
            "postdelegate_team_year": 27600,
            "postdelegate_pro_month": 3900,
            "postdelegate_pro_year": 37200,
            "postdelegate_ultimate_month": 7900,
            "postdelegate_ultimate_year": 75600,
        }
        metadata = stripe_mock.Price.create.call_args_list[0].kwargs["metadata"]
        assert metadata == {"plan": "starter", "plan_name": "Starter", "interval": "month"}
        assert "STRIPE_PRICE_TEAM_ANNUAL=price_postdelegate_team_year" in output
        assert "STRIPE_PRICE_ULTIMATE_MONTHLY=price_postdelegate_ultimate_month" in output
        assert "STRIPE_PRICE_PRO_ANNUAL=price_postdelegate_pro_year" in output
        assert "/webhooks/stripe/" in output and "invoice.payment_failed" in output

    def test_rerun_reuses_existing_prices(self, settings):
        settings.STRIPE_SECRET_KEY = "sk_test_fake"
        with mock.patch(COMMAND_STRIPE) as stripe_mock:
            stripe_mock.Price.list.return_value = {"data": [{"id": "price_existing"}]}
            output = _run()
        stripe_mock.Price.create.assert_not_called()
        stripe_mock.Product.create.assert_not_called()
        assert "STRIPE_PRICE_STARTER_MONTHLY=price_existing" in output

    def test_a_changed_amount_gets_a_new_price_that_takes_the_lookup_key(self, settings):
        settings.STRIPE_SECRET_KEY = "sk_test_fake"
        with mock.patch(COMMAND_STRIPE) as stripe_mock:
            # An old Team yearly price from before the 20%-off pricing.
            stripe_mock.Price.list.side_effect = lambda **kw: {
                "data": [{"id": "price_old", "unit_amount": 29000}]
                if kw["lookup_keys"] == ["postdelegate_team_year"]
                else [{"id": "price_ok"}]
            }
            stripe_mock.Product.search.return_value = {"data": [{"id": "prod_team"}]}
            stripe_mock.Price.create.return_value = {"id": "price_new"}
            output = _run()
        created = stripe_mock.Price.create.call_args.kwargs
        assert stripe_mock.Price.create.call_count == 1
        assert (created["lookup_key"], created["unit_amount"], created["transfer_lookup_key"]) == (
            "postdelegate_team_year",
            27600,
            True,
        )
        assert "STRIPE_PRICE_TEAM_ANNUAL=price_new" in output

    def test_refuses_a_live_key_without_the_flag(self, settings):
        settings.STRIPE_SECRET_KEY = "sk_live_real"
        with mock.patch(COMMAND_STRIPE) as stripe_mock, pytest.raises(CommandError, match="live-mode"):
            _run()
        stripe_mock.Price.list.assert_not_called()

    def test_needs_a_key(self, settings):
        settings.STRIPE_SECRET_KEY = ""
        with pytest.raises(CommandError, match="STRIPE_SECRET_KEY"):
            _run()
