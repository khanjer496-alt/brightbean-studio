"""Helpers shared by the billing tests (fixtures live in conftest.py)."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import timedelta

from django.test import Client
from django.utils import timezone

from apps.billing.models import Subscription
from apps.social_accounts.models import SocialAccount

WEBHOOK_SECRET = "whsec_test_secret"
PRICE_IDS = {
    "starter": {"month": "price_starter_m", "year": "price_starter_y"},
    "team": {"month": "price_team_m", "year": "price_team_y"},
    "pro": {"month": "price_pro_m", "year": "price_pro_y"},
    "ultimate": {"month": "price_ultimate_m", "year": "price_ultimate_y"},
}


def set_plan(org, plan: str, *, status=Subscription.Status.ACTIVE, **fields) -> Subscription:
    defaults = {"plan": plan, "status": status, "trial_ends_at": None, **fields}
    sub, _ = Subscription.objects.update_or_create(organization=org, defaults=defaults)
    return sub


def expire_trial(org) -> Subscription:
    return set_plan(org, "team", status=Subscription.Status.TRIALING, trial_ends_at=timezone.now() - timedelta(days=1))


def make_channels(workspace, count: int, prefix: str = "acct") -> list[SocialAccount]:
    return [
        SocialAccount.objects.create(
            workspace=workspace,
            platform="linkedin_personal",
            account_platform_id=f"{prefix}-{i}",
            account_name=f"{prefix} {i}",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        for i in range(count)
    ]


def stripe_signature(payload: bytes, secret: str = WEBHOOK_SECRET, timestamp: int | None = None) -> str:
    timestamp = timestamp or int(time.time())
    signed = f"{timestamp}.".encode() + payload
    digest = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def post_event(client: Client, event: dict, *, signature: str | None = None):
    payload = json.dumps(event).encode()
    return client.post(
        "/webhooks/stripe/",
        data=payload,
        content_type="application/json",
        HTTP_STRIPE_SIGNATURE=signature if signature is not None else stripe_signature(payload),
    )
