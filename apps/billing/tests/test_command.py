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

        assert stripe_mock.Product.create.call_count == 3
        amounts = {kw["lookup_key"]: kw["unit_amount"] for _, kw in stripe_mock.Price.create.call_args_list}
        assert amounts == {
            "postdelegate_creator_month": 1200,
            "postdelegate_creator_year": 12000,
            "postdelegate_team_month": 2900,
            "postdelegate_team_year": 29000,
            "postdelegate_agency_month": 7900,
            "postdelegate_agency_year": 79000,
        }
        assert "STRIPE_PRICE_TEAM_ANNUAL=price_postdelegate_team_year" in output
        assert "STRIPE_PRICE_AGENCY_MONTHLY=price_postdelegate_agency_month" in output
        assert "/webhooks/stripe/" in output and "invoice.payment_failed" in output

    def test_rerun_reuses_existing_prices(self, settings):
        settings.STRIPE_SECRET_KEY = "sk_test_fake"
        with mock.patch(COMMAND_STRIPE) as stripe_mock:
            stripe_mock.Price.list.return_value = {"data": [{"id": "price_existing"}]}
            output = _run()
        stripe_mock.Price.create.assert_not_called()
        stripe_mock.Product.create.assert_not_called()
        assert "STRIPE_PRICE_CREATOR_MONTHLY=price_existing" in output

    def test_refuses_a_live_key_without_the_flag(self, settings):
        settings.STRIPE_SECRET_KEY = "sk_live_real"
        with mock.patch(COMMAND_STRIPE) as stripe_mock, pytest.raises(CommandError, match="live-mode"):
            _run()
        stripe_mock.Price.list.assert_not_called()

    def test_needs_a_key(self, settings):
        settings.STRIPE_SECRET_KEY = ""
        with pytest.raises(CommandError, match="STRIPE_SECRET_KEY"):
            _run()
