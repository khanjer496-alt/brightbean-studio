"""Plan enforcement on the Agent API and the MCP endpoint."""

from __future__ import annotations

import dataclasses
import json
from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from apps.api_keys import services as key_services
from apps.api_keys.models import ApiKeyUsageHourly
from apps.billing.models import Subscription
from apps.billing.plans import PLANS
from apps.members.models import PERMISSION_KEYS

from .support import expire_trial, make_channels, set_plan


class _SecureClient(Client):
    def generic(self, method, path, *args, **kwargs):
        kwargs["secure"] = True
        return super().generic(method, path, *args, **kwargs)


@pytest.fixture(autouse=True)
def _clear_auth_throttle():
    from django.core.cache import cache

    cache.delete("agent_api:auth_fail:127.0.0.1")


@pytest.fixture
def account(workspace):
    return make_channels(workspace, 1)[0]


@pytest.fixture
def api_client(owner, workspace, account):
    key = key_services.issue_api_key(
        workspace=workspace,
        social_accounts=[account],
        issued_by=owner,
        name="agent",
        permissions=list(PERMISSION_KEYS),
    )
    return _SecureClient(HTTP_AUTHORIZATION=f"Bearer {key.plaintext_token}")


@pytest.fixture
def one_post_agency(monkeypatch, organization):
    """Agency, but with a one-post monthly allowance so the post limit is reachable."""
    monkeypatch.setitem(PLANS, "agency", dataclasses.replace(PLANS["agency"], max_scheduled_posts_per_month=1))
    set_plan(organization, "agency")


def _use_api_calls(workspace, count):
    """Record ``count`` calls made today by another caller in the organization."""
    now = timezone.now()
    ApiKeyUsageHourly.objects.create(
        actor="key:00000000-0000-0000-0000-000000000000",
        hour_start=now.replace(minute=0, second=0, microsecond=0),
        action="other",
        status_code=200,
        count=count,
        last_seen_at=now,
        workspace=workspace,
    )


def _create(client, account, action="schedule"):
    return client.post(
        "/api/v1/posts/",
        data=json.dumps(
            {
                "social_account_id": str(account.id),
                "caption": "from the API",
                "action": action,
                "scheduled_at": (timezone.now() + timedelta(minutes=30)).isoformat(),
            }
        ),
        content_type="application/json",
    )


def _mcp(client, method, params=None):
    msg = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    return client.post("/api/v1/mcp/", data=json.dumps(msg), content_type="application/json")


@pytest.mark.django_db
class TestRestApi:
    def test_api_works_on_every_plan_and_during_the_trial(self, billing, organization, api_client):
        assert api_client.get("/api/v1/me/").status_code == 200  # trialing
        for plan in ("free", "creator", "team", "agency"):
            set_plan(organization, plan)
            assert api_client.get("/api/v1/me/").status_code == 200, plan
        expire_trial(organization)
        assert api_client.get("/api/v1/me/").status_code == 200

    def test_daily_call_allowance_is_a_429_for_the_whole_organization(
        self, billing, organization, workspace, api_client
    ):
        expire_trial(organization)
        _use_api_calls(workspace, 99)
        assert api_client.get("/api/v1/me/").status_code == 200  # the 100th call
        response = api_client.get("/api/v1/me/")
        assert response.status_code == 429
        body = response.json()
        assert (body["tier"], body["limit"], body["remaining"]) == ("plan_daily", 100, 0)
        assert body["upgrade_url"] == "https://studio.example.com/organizations/billing/"
        assert 0 < int(response["Retry-After"]) <= 86400

        set_plan(organization, "creator")
        assert api_client.get("/api/v1/me/").status_code == 200

    def test_no_daily_allowance_when_billing_disabled(self, workspace, api_client):
        _use_api_calls(workspace, 5000)
        assert api_client.get("/api/v1/me/").status_code == 200

    def test_post_limit_is_a_402(self, billing, one_post_agency, api_client, account):
        assert _create(api_client, account).status_code == 201
        response = _create(api_client, account)
        assert response.status_code == 402
        assert response.json()["code"] == "post_limit"
        # Drafts are never limited.
        assert _create(api_client, account, action="draft").status_code == 201

    def test_scheduling_a_draft_hits_the_limit(self, billing, one_post_agency, api_client, account):
        assert _create(api_client, account).status_code == 201
        draft_id = _create(api_client, account, action="draft").json()["id"]
        response = api_client.post(
            f"/api/v1/posts/{draft_id}/schedule",
            data=json.dumps({"scheduled_at": (timezone.now() + timedelta(hours=1)).isoformat()}),
            content_type="application/json",
        )
        assert response.status_code == 402

    def test_unchanged_when_billing_disabled(self, organization, api_client, account):
        for _ in range(35):
            assert _create(api_client, account).status_code == 201
        assert not Subscription.objects.exists()


@pytest.mark.django_db
class TestMcp:
    def test_mcp_works_on_free(self, billing, organization, api_client):
        expire_trial(organization)
        assert _mcp(api_client, "ping").json()["result"] == {}

    def test_mcp_shares_the_daily_call_allowance(self, billing, organization, workspace, api_client):
        expire_trial(organization)
        _use_api_calls(workspace, 100)
        response = _mcp(api_client, "ping")
        assert response.status_code == 429
        assert response.json()["tier"] == "plan_daily"

    def test_post_limit_is_a_readable_tool_error(self, billing, one_post_agency, api_client, account):
        args = {
            "social_account_id": str(account.id),
            "caption": "via mcp",
            "scheduled_at": (timezone.now() + timedelta(minutes=30)).isoformat(),
        }
        first = _mcp(api_client, "tools/call", {"name": "schedule_post", "arguments": args}).json()
        assert "error" not in first, first
        second = _mcp(api_client, "tools/call", {"name": "schedule_post", "arguments": args}).json()
        assert "1 scheduled posts per month" in second["error"]["message"]
        assert "/organizations/billing/" in second["error"]["message"]

    def test_unchanged_when_billing_disabled(self, api_client):
        assert _mcp(api_client, "ping").json()["result"] == {}
