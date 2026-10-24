"""Scoped agent tokens: capabilities, budgets, expiry, IP ranges, approval mode, rotation and revocation."""

from __future__ import annotations

import json
import re
import uuid
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.test import override_settings
from django.utils import timezone

from apps.api.tests.world import MCP_URL, SecureClient, bearer, tool_error, tool_result
from apps.api_keys import capabilities as caps
from apps.api_keys import services
from apps.api_keys.models import ApiKey, ApiKeyDailyUsage
from apps.autonomy.models import AgentAction
from apps.autonomy.tests.conftest import post_json, set_policy
from apps.members.models import PERMISSION_KEYS

pytestmark = pytest.mark.django_db

ZERO = "11111111-1111-1111-1111-111111111111"


def make_token(world, capabilities, **extra):
    issued = services.issue_api_key(
        workspace=world.workspace,
        social_accounts=[world.account1, world.account2],
        issued_by=world.owner,
        name=extra.pop("name", "scoped"),
        permissions=list(PERMISSION_KEYS),
        capabilities=capabilities,
        **extra,
    )
    return issued, bearer(issued.plaintext_token)


def tolerant(token: str, **extra) -> SecureClient:
    return SecureClient(raise_request_exception=False, HTTP_AUTHORIZATION=f"Bearer {token}", **extra)


def url_for(route: str) -> tuple[str, str]:
    method, template = route.split(" ", 1)
    return method.lower(), caps.API_PREFIX + re.sub(r"\{[^}]+\}", ZERO, template)


def send(client, route: str):
    method, url = url_for(route)
    kwargs = {"data": "{}", "content_type": "application/json"} if method in {"post", "patch"} else {}
    return getattr(client, method)(url, **kwargs)


def missing_capability(response) -> str | None:
    if response.status_code != 403:
        return None
    detail = response.json().get("detail", "")
    return detail.removeprefix("Missing capability: ") if detail.startswith("Missing capability: ") else None


class TestRestCapabilities:
    @pytest.mark.parametrize("cap", caps.CAPABILITY_KEYS)
    def test_every_route_of_a_capability_needs_it(self, world, cap):
        issued, _ = make_token(world, [k for k in caps.CAPABILITY_KEYS if k != cap])
        without = tolerant(issued.plaintext_token)
        for route, owner in caps.ROUTE_CAPABILITY.items():
            if owner == cap:
                assert missing_capability(send(without, route)) == cap, route

    @pytest.mark.parametrize("cap", caps.CAPABILITY_KEYS)
    def test_holding_a_capability_opens_its_routes_and_no_others(self, world, cap):
        issued, _ = make_token(world, [cap])
        only = tolerant(issued.plaintext_token)
        for route, owner in caps.ROUTE_CAPABILITY.items():
            got = missing_capability(send(only, route))
            if owner == cap:
                assert got is None, f"{route} refused a token that holds {cap}"
            else:
                assert got == owner, route

    def test_legacy_keys_without_a_capability_limit_are_not_restricted(self, world):
        assert world.full.get("/api/v1/me/").status_code == 200
        assert world.full.get("/api/v1/brand-voice/").status_code != 403

    def test_the_403_names_the_capability(self, world):
        _, client = make_token(world, ["read_calendar"])
        response = client.get("/api/v1/analytics/overview")
        assert response.status_code == 403
        assert response.json() == {"error": "forbidden", "detail": "Missing capability: read_analytics"}

    def test_scheduling_through_create_needs_the_schedule_capability(self, world):
        _, client = make_token(world, ["draft_posts"])
        body = {
            "social_account_id": str(world.account1.id),
            "caption": "x",
            "action": "schedule",
            "scheduled_at": "2030-01-01T10:00:00Z",
        }
        response = post_json(client, "/api/v1/posts/", body)
        assert response.status_code == 403 and "schedule_posts" in response.json()["detail"]
        assert post_json(client, "/api/v1/posts/", {**body, "action": "draft"}).status_code == 201

    def test_a_capability_withdraws_the_permissions_only_it_would_use(self, world):
        issued, _ = make_token(world, ["draft_posts", "read_calendar"])
        from apps.api.auth import _resolve_effective_permissions

        effective = _resolve_effective_permissions(ApiKey.objects.get(pk=issued.api_key.pk))
        assert effective["create_posts"] is True
        assert effective["publish_directly"] is False


class TestMcpCapabilities:
    def list_tools(self, client) -> set[str]:
        response = client.post(
            MCP_URL,
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
            content_type="application/json",
        )
        return {t["name"] for t in response.json()["result"]["tools"]}

    @pytest.mark.parametrize("cap", caps.CAPABILITY_KEYS)
    def test_tools_list_shows_only_the_tools_a_token_can_use(self, world, cap):
        _, client = make_token(world, [cap])
        expected = {name for name, owner in caps.TOOL_CAPABILITY.items() if owner == cap}
        assert self.list_tools(client) == expected

    def test_an_unlimited_key_sees_every_tool(self, world):
        assert self.list_tools(world.full) == set(caps.TOOL_CAPABILITY)

    @pytest.mark.parametrize("cap", caps.CAPABILITY_KEYS)
    def test_calling_a_tool_outside_the_capabilities_is_refused_by_name(self, world, cap):
        _, client = make_token(world, [k for k in caps.CAPABILITY_KEYS if k != cap])
        for name, owner in caps.TOOL_CAPABILITY.items():
            if owner == cap:
                assert tool_error(client, name) == f"Missing capability: {cap}", name

    def test_a_held_tool_is_not_refused_for_capability(self, world):
        _, client = make_token(world, ["read_calendar"])
        assert tool_result(client, "list_accounts")

    def test_a_bad_tool_name_is_still_unknown(self, world):
        _, client = make_token(world, ["read_calendar"])
        assert "unknown tool" in tool_error(client, "no_such_tool")


class TestDailyRequestBudget:
    def test_the_request_after_the_budget_is_a_429_that_resets_at_midnight_utc(self, world):
        _, client = make_token(world, ["read_calendar"], daily_request_budget=2)
        assert client.get("/api/v1/me/").status_code == 200
        assert client.get("/api/v1/me/").status_code == 200
        spent = client.get("/api/v1/me/")
        assert spent.status_code == 429
        body = spent.json()
        assert body["tier"] == "token_daily_requests" and body["limit"] == 2 and body["remaining"] == 0
        assert 1 <= int(spent["Retry-After"]) <= 86400

        tomorrow = timezone.now() + timedelta(days=1, minutes=1)
        with patch("django.utils.timezone.now", return_value=tomorrow):
            assert client.get("/api/v1/me/").status_code == 200

    def test_the_budget_applies_to_mcp_messages_too(self, world):
        _, client = make_token(world, ["read_calendar"], daily_request_budget=1)
        assert tool_result(client, "list_accounts")
        response = client.post(
            MCP_URL,
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
            content_type="application/json",
        )
        assert response.status_code == 429

    def test_a_token_without_a_budget_is_not_limited_by_this(self, world):
        _, client = make_token(world, ["read_calendar"])
        assert all(client.get("/api/v1/me/").status_code == 200 for _ in range(5))

    def test_one_tokens_spend_does_not_use_up_another(self, world):
        _, spent = make_token(world, ["read_calendar"], daily_request_budget=1, name="a")
        _, fresh = make_token(world, ["read_calendar"], daily_request_budget=1, name="b")
        spent.get("/api/v1/me/")
        assert spent.get("/api/v1/me/").status_code == 429
        assert fresh.get("/api/v1/me/").status_code == 200

    def test_the_budget_must_stay_within_the_plan_allowance(self, world):
        plan = MagicMock(max_api_calls_per_day=100)
        with (
            patch("apps.billing.entitlements.get_entitlements", return_value=plan),
            pytest.raises(ValueError, match="plan's allowance of 100"),
        ):
            make_token(world, ["read_calendar"], daily_request_budget=101)
        make_token(world, ["read_calendar"], daily_request_budget=100)


class TestDailyAiBudget:
    def ai_body(self, world):
        return {"prompt": "A friendly hello", "social_account_ids": [str(world.account1.id)]}

    def test_ai_calls_past_the_budget_are_a_429_and_reset_at_midnight(self, world):
        issued, client = make_token(world, ["use_ai_assistant"], daily_ai_budget=1)
        first = post_json(client, "/api/v1/ai/caption", self.ai_body(world))
        assert first.status_code != 429  # whatever the unconfigured assistant answers, the call was spent
        second = post_json(client, "/api/v1/ai/caption", self.ai_body(world))
        assert second.status_code == 429 and second.json()["tier"] == "token_daily_ai"
        assert ApiKeyDailyUsage.objects.get(api_key=issued.api_key).ai_calls == 1

        with patch("django.utils.timezone.now", return_value=timezone.now() + timedelta(days=1, minutes=1)):
            assert post_json(client, "/api/v1/ai/caption", self.ai_body(world)).status_code != 429

    def test_the_mcp_ai_tools_spend_the_same_budget(self, world):
        _, client = make_token(world, ["use_ai_assistant"], daily_ai_budget=1)
        args = {"prompt": "Hello", "social_account_ids": [str(world.account1.id)]}
        from apps.api.tests.world import call_tool

        call_tool(client, "generate_caption", args)
        assert "Daily AI budget" in tool_error(client, "adapt_caption", {"caption": "x", **args})

    def test_a_zero_budget_means_no_ai_at_all(self, world):
        _, client = make_token(world, ["use_ai_assistant"], daily_ai_budget=0)
        assert post_json(client, "/api/v1/ai/caption", self.ai_body(world)).status_code == 429

    def test_other_routes_do_not_spend_the_ai_budget(self, world):
        issued, client = make_token(world, ["use_ai_assistant", "read_calendar"], daily_ai_budget=1)
        for _ in range(3):
            client.get("/api/v1/me/")
        assert not ApiKeyDailyUsage.objects.filter(api_key=issued.api_key).exists()


class TestDailyPostBudget:
    def schedule(self, client, account, hours=48):
        when = (timezone.now() + timedelta(hours=hours)).isoformat()
        return post_json(
            client,
            "/api/v1/posts/",
            {
                "social_account_id": str(account.id),
                "caption": "A calm update",
                "action": "schedule",
                "scheduled_at": when,
            },
        )

    def test_posts_past_the_budget_are_refused_and_the_budget_resets_at_midnight(self, world):
        set_policy(world.workspace, publish_level="full_auto", draft_level="full_auto")
        issued, client = make_token(world, ["draft_posts", "schedule_posts"], daily_post_budget=1)
        first = self.schedule(client, world.account1)
        assert first.status_code == 201 and first.json()["autonomy"]["decision"] == "executed"
        refused = self.schedule(client, world.account2)
        assert refused.status_code == 403
        assert "daily limit of 1 scheduled posts" in refused.json()["detail"]
        assert AgentAction.objects.filter(api_key=issued.api_key, decision="blocked").exists()

        with patch("django.utils.timezone.now", return_value=timezone.now() + timedelta(days=1, minutes=1)):
            again = self.schedule(client, world.account2, hours=96)
            assert again.status_code == 201 and again.json()["autonomy"]["decision"] == "executed"

    def test_a_held_draft_does_not_use_the_budget(self, world):
        set_policy(world.workspace, publish_level="draft_only", draft_level="full_auto")
        issued, client = make_token(world, ["draft_posts", "schedule_posts"], daily_post_budget=1)
        assert self.schedule(client, world.account1).json()["autonomy"]["decision"] == "held"
        assert self.schedule(client, world.account2).json()["autonomy"]["decision"] == "held"
        assert not ApiKeyDailyUsage.objects.filter(api_key=issued.api_key, posts_scheduled__gt=0).exists()

    def test_a_token_with_no_budget_schedules_freely(self, world):
        set_policy(world.workspace, publish_level="full_auto", draft_level="full_auto")
        _, client = make_token(world, ["draft_posts", "schedule_posts"])
        assert self.schedule(client, world.account1).status_code == 201
        assert self.schedule(client, world.account2).status_code == 201


class TestExpiry:
    def test_a_token_stops_working_at_its_expiry(self, world):
        _, client = make_token(world, ["read_calendar"], expires_at=timezone.now() + timedelta(hours=1))
        assert client.get("/api/v1/me/").status_code == 200
        with patch("django.utils.timezone.now", return_value=timezone.now() + timedelta(hours=2)):
            assert client.get("/api/v1/me/").status_code == 401

    def test_an_already_expired_token_is_refused(self, world):
        _, client = make_token(world, ["read_calendar"], expires_at=timezone.now() - timedelta(seconds=1))
        assert client.get("/api/v1/me/").status_code == 401


class TestAllowedIpRanges:
    def test_only_addresses_inside_a_range_get_through(self, world):
        _, client = make_token(world, ["read_calendar"], allowed_ip_ranges=["203.0.113.0/24", "2001:db8::/32"])
        assert client.get("/api/v1/me/", REMOTE_ADDR="203.0.113.9").status_code == 200
        assert client.get("/api/v1/me/", REMOTE_ADDR="2001:db8::1").status_code == 200
        blocked = client.get("/api/v1/me/", REMOTE_ADDR="198.51.100.7")
        assert blocked.status_code == 403 and "IP address" in blocked.json()["detail"]

    def test_a_forged_forwarded_header_does_not_help(self, world):
        _, client = make_token(world, ["read_calendar"], allowed_ip_ranges=["203.0.113.0/24"])
        response = client.get("/api/v1/me/", REMOTE_ADDR="198.51.100.7", HTTP_X_FORWARDED_FOR="203.0.113.9")
        assert response.status_code == 403

    @override_settings(BB_TRUSTED_PROXIES=["10.0.0.0/8"])
    def test_a_trusted_proxy_vouches_for_the_client_address(self, world):
        _, client = make_token(world, ["read_calendar"], allowed_ip_ranges=["203.0.113.0/24"])
        ok = client.get("/api/v1/me/", REMOTE_ADDR="10.0.0.5", HTTP_X_FORWARDED_FOR="203.0.113.9")
        assert ok.status_code == 200
        spoofed = client.get("/api/v1/me/", REMOTE_ADDR="10.0.0.5", HTTP_X_FORWARDED_FOR="203.0.113.9, 198.51.100.7")
        assert spoofed.status_code == 403  # the address the proxy saw is the last hop, and it is outside

    def test_a_bare_address_counts_as_a_single_host_range(self, world):
        _, client = make_token(world, ["read_calendar"], allowed_ip_ranges=["203.0.113.9"])
        assert client.get("/api/v1/me/", REMOTE_ADDR="203.0.113.9").status_code == 200
        assert client.get("/api/v1/me/", REMOTE_ADDR="203.0.113.10").status_code == 403

    def test_a_refused_address_is_not_recorded_as_a_use(self, world):
        issued, client = make_token(world, ["read_calendar"], allowed_ip_ranges=["203.0.113.0/24"])
        client.get("/api/v1/me/", REMOTE_ADDR="198.51.100.7")
        issued.api_key.refresh_from_db()
        assert issued.api_key.last_used_at is None

    def test_malformed_ranges_are_rejected_when_the_token_is_saved(self, world):
        with pytest.raises(ValueError, match="not an IP address or CIDR range"):
            make_token(world, ["read_calendar"], allowed_ip_ranges=["not-an-ip"])

    def test_no_ranges_means_any_address(self, world):
        _, client = make_token(world, ["read_calendar"])
        assert client.get("/api/v1/me/", REMOTE_ADDR="198.51.100.7").status_code == 200


class TestReadOnlyUntilApproved:
    def test_a_schedule_becomes_a_draft_whatever_the_workspace_allows(self, world):
        set_policy(world.workspace, publish_level="full_auto", draft_level="full_auto")
        _, free = make_token(world, ["draft_posts", "schedule_posts"], name="free")
        _, gated = make_token(world, ["draft_posts", "schedule_posts"], requires_approval=True, name="gated")
        when = (timezone.now() + timedelta(hours=48)).isoformat()

        def schedule(client, account):
            return post_json(
                client,
                "/api/v1/posts/",
                {
                    "social_account_id": str(account.id),
                    "caption": "Hello there",
                    "action": "schedule",
                    "scheduled_at": when,
                },
            ).json()

        assert schedule(free, world.account1)["autonomy"]["decision"] == "executed"
        held = schedule(gated, world.account2)
        assert held["autonomy"]["decision"] == "held"
        assert "read-only until approved" in held["autonomy"]["reasons"][0]
        assert [pp["status"] for pp in held["platform_posts"]] == ["draft"]

    def test_it_can_only_tighten_an_off_workspace_stays_off(self, world):
        set_policy(world.workspace, publish_level="off", draft_level="full_auto")
        _, client = make_token(world, ["draft_posts", "schedule_posts"], requires_approval=True)
        when = (timezone.now() + timedelta(hours=48)).isoformat()
        response = post_json(
            client,
            "/api/v1/posts/",
            {"social_account_id": str(world.account1.id), "caption": "Hi", "action": "schedule", "scheduled_at": when},
        )
        assert response.status_code == 403 and "Off" in response.json()["detail"]

    def test_it_applies_over_mcp_too(self, world):
        set_policy(world.workspace, publish_level="full_auto", draft_level="full_auto")
        _, client = make_token(world, ["draft_posts", "schedule_posts"], requires_approval=True)
        when = (timezone.now() + timedelta(hours=48)).isoformat()
        result = tool_result(
            client,
            "schedule_post",
            {"social_account_id": str(world.account1.id), "caption": "Hello", "scheduled_at": when},
        )
        assert result["autonomy"]["decision"] == "held"

    def test_replies_are_drafts_for_a_gated_token(self, world):
        from apps.autonomy.actors import agent
        from apps.autonomy.service import evaluate

        set_policy(world.workspace, reply_level="full_auto")
        issued, _ = make_token(world, ["reply_inbox"], requires_approval=True)
        verdict = evaluate(
            workspace=world.workspace,
            actor=agent(issued.api_key, world.owner),
            action_type="reply",
            account=world.account1,
            text="Thanks!",
        )
        assert verdict.held and verdict.level == "draft_only"

    def test_it_is_off_by_default(self, world):
        issued, _ = make_token(world, ["read_calendar"])
        assert issued.api_key.requires_approval is False


class TestRotateAndRevoke:
    def test_a_revoked_token_stops_on_the_very_next_request(self, world):
        issued, client = make_token(world, ["read_calendar"])
        assert client.get("/api/v1/me/").status_code == 200
        services.revoke_api_key(issued.api_key)
        assert client.get("/api/v1/me/").status_code == 401

    def test_a_revocation_made_behind_the_apps_back_still_applies_at_once(self, world):
        """Another worker has no way to hear about a revocation through a process-local cache, so there is none."""
        issued, client = make_token(world, ["read_calendar"])
        for _ in range(3):
            assert client.get("/api/v1/me/").status_code == 200
        ApiKey.objects.filter(pk=issued.api_key.pk).update(revoked_at=timezone.now())  # no signals, no cache calls
        assert client.get("/api/v1/me/").status_code == 401

    def test_an_edit_made_behind_the_apps_back_applies_at_once(self, world):
        issued, client = make_token(world, ["read_calendar", "read_analytics"])
        assert client.get("/api/v1/analytics/overview").status_code != 403
        ApiKey.objects.filter(pk=issued.api_key.pk).update(capabilities=["read_calendar"])
        assert client.get("/api/v1/analytics/overview").status_code == 403

    def test_rotating_kills_the_old_secret_and_keeps_the_token(self, world):
        issued, old = make_token(world, ["read_calendar"], daily_request_budget=50, name="keep me")
        assert old.get("/api/v1/me/").status_code == 200
        rotated = services.rotate_api_key(issued.api_key, editor=world.owner)
        assert rotated.api_key.pk == issued.api_key.pk
        assert rotated.plaintext_token != issued.plaintext_token
        assert old.get("/api/v1/me/").status_code == 401
        new = bearer(rotated.plaintext_token)
        assert new.get("/api/v1/me/").status_code == 200
        kept = ApiKey.objects.get(pk=issued.api_key.pk)
        assert (kept.name, kept.capabilities, kept.daily_request_budget) == ("keep me", ["read_calendar"], 50)

    def test_a_revoked_token_cannot_be_rotated(self, world):
        issued, _ = make_token(world, ["read_calendar"])
        services.revoke_api_key(issued.api_key)
        with pytest.raises(ValueError, match="active"):
            services.rotate_api_key(issued.api_key, editor=world.owner)

    def test_only_someone_who_manages_keys_can_rotate(self, world):
        from apps.accounts.models import User
        from apps.members.models import OrgMembership

        issued, _ = make_token(world, ["read_calendar"])
        member = User.objects.create_user(
            email=f"m-{uuid.uuid4().hex[:6]}@example.com", password="x", name="M", tos_accepted_at=timezone.now()
        )
        OrgMembership.objects.create(
            user=member, organization=world.workspace.organization, org_role=OrgMembership.OrgRole.MEMBER
        )
        with pytest.raises(ValueError, match="manage_api_keys"):
            services.rotate_api_key(issued.api_key, editor=member)


class TestScopeValidation:
    def test_a_token_must_hold_at_least_one_capability(self, world):
        with pytest.raises(ValueError, match="at least one capability"):
            make_token(world, [])

    def test_unknown_capabilities_are_dropped(self, world):
        issued, _ = make_token(world, ["read_calendar", "launch_missiles"])
        assert issued.api_key.capabilities == ["read_calendar"]

    def test_the_preset_is_remembered_only_when_it_is_a_real_one(self, world):
        issued, _ = make_token(world, ["read_calendar"], preset="drafting_assistant")
        bogus, _ = make_token(world, ["read_calendar"], preset="nonsense", name="b")
        assert (issued.api_key.preset, bogus.api_key.preset) == ("drafting_assistant", "")

    def test_updating_the_scope_applies_on_the_next_request(self, world):
        issued, client = make_token(world, ["read_calendar"])
        assert client.get("/api/v1/analytics/overview").status_code == 403
        services.update_api_key(
            issued.api_key,
            editor=world.owner,
            permissions=list(PERMISSION_KEYS),
            social_accounts=[world.account1],
            capabilities=["read_calendar", "read_analytics"],
            allowed_ip_ranges=["127.0.0.1"],
        )
        assert client.get("/api/v1/analytics/overview", REMOTE_ADDR="127.0.0.1").status_code != 403

    def test_updating_without_scope_arguments_leaves_the_scope_alone(self, world):
        issued, _ = make_token(world, ["read_calendar"], daily_ai_budget=7, requires_approval=True)
        services.update_api_key(issued.api_key, editor=world.owner, permissions=[], social_accounts=[world.account1])
        kept = ApiKey.objects.get(pk=issued.api_key.pk)
        assert (kept.capabilities, kept.daily_ai_budget, kept.requires_approval) == (["read_calendar"], 7, True)


# ---------------------------------------------------------------------------
# Review round: presets work as issued, budgets can't be overshot, verify_token stays cheap
# ---------------------------------------------------------------------------

#: Routes that need a permission beyond what their capability pre-ticks: the issuer grants it separately.
_NEEDS_AN_EXTRA_PERMISSION = {
    "GET /webhooks/",
    "GET /webhooks/{endpoint_id}",
    "GET /webhooks/{endpoint_id}/deliveries",
}


class TestCapabilitiesWorkWithThePermissionsTheyPreTick:
    @pytest.mark.parametrize("cap", caps.CAPABILITY_KEYS)
    def test_no_route_of_a_capability_is_refused_for_a_permission_the_form_grants(self, world, cap):
        issued = services.issue_api_key(
            workspace=world.workspace,
            social_accounts=[world.account1, world.account2],
            issued_by=world.owner,
            name="as issued",
            permissions=caps.permissions_for([cap]),
            capabilities=[cap],
        )
        client = tolerant(issued.plaintext_token)
        refused = []
        for route, owner in caps.ROUTE_CAPABILITY.items():
            if owner != cap or route in _NEEDS_AN_EXTRA_PERMISSION:
                continue
            response = send(client, route)
            if response.status_code == 403 and "Permission denied" in response.json().get("detail", ""):
                refused.append((route, response.json()["detail"]))
        assert not refused

    def test_a_read_only_token_still_cannot_schedule_approve_or_send(self, world):
        from apps.api.auth import _resolve_effective_permissions

        issued, _ = make_token(world, list(caps.preset("read_only_analyst").capabilities))
        effective = _resolve_effective_permissions(ApiKey.objects.get(pk=issued.api_key.pk))
        assert not any(effective[p] for p in caps.GUARDED_PERMISSIONS)


class TestOneRequestCannotOvershootThePostBudget:
    def two_child_post(self, world):
        from apps.composer.models import PlatformPost, Post

        post = Post.objects.create(workspace=world.workspace, author=world.owner, caption="Two networks")
        for account in (world.account1, world.account2):
            PlatformPost.objects.create(post=post, social_account=account, status="draft")
        return post

    def schedule(self, client, post):
        when = (timezone.now() + timedelta(hours=48)).isoformat()
        return post_json(client, f"/api/v1/posts/{post.id}/schedule", {"scheduled_at": when})

    def test_a_request_that_would_pass_the_budget_is_refused_whole_and_spends_nothing(self, world):
        set_policy(world.workspace, publish_level="full_auto", draft_level="full_auto")
        issued, client = make_token(world, ["draft_posts", "schedule_posts"], daily_post_budget=1)
        post = self.two_child_post(world)
        response = self.schedule(client, post)
        assert response.status_code == 403
        assert "daily limit of 1 scheduled posts" in response.json()["detail"]
        assert not post.platform_posts.filter(status="scheduled").exists()
        usage = ApiKeyDailyUsage.objects.filter(api_key=issued.api_key).first()
        assert usage is None or usage.posts_scheduled == 0

    def test_each_post_of_a_request_counts(self, world):
        set_policy(world.workspace, publish_level="full_auto", draft_level="full_auto")
        issued, client = make_token(world, ["draft_posts", "schedule_posts"], daily_post_budget=2)
        post = self.two_child_post(world)
        assert self.schedule(client, post).status_code == 200
        assert ApiKeyDailyUsage.objects.get(api_key=issued.api_key).posts_scheduled == 2
        refused = TestDailyPostBudget().schedule(client, world.account1)
        assert refused.status_code == 403

    def test_the_reservation_never_passes_the_budget(self, world):
        from apps.api_keys import budgets

        issued, _ = make_token(world, ["schedule_posts"], daily_post_budget=3)
        results = [budgets.reserve_scheduled_post(issued.api_key) for _ in range(5)]
        assert results == [True, True, True, False, False]
        assert ApiKeyDailyUsage.objects.get(api_key=issued.api_key).posts_scheduled == 3


class TestRepurposingSpendsTheAiBudget:
    def test_a_run_needing_more_ai_calls_than_are_left_is_refused_before_it_starts(self, world):
        from apps.repurpose.models import RepurposeJob

        issued, client = make_token(world, ["use_ai_assistant"], daily_ai_budget=1)
        response = post_json(
            client,
            "/api/v1/repurpose/jobs",
            {
                "source_type": "text",
                "text": "A long article about calm social media. " * 40,
                "outputs": [{"kind": "post", "social_account_ids": [str(world.account1.id), str(world.account2.id)]}],
            },
        )
        assert response.status_code == 429
        assert "daily AI budget" in response.json()["detail"]
        assert not RepurposeJob.objects.exists()


class TestVerifyTokenCost:
    def test_a_request_resolves_its_token_in_three_queries(self, world, django_assert_max_num_queries):
        issued, _ = make_token(world, ["read_calendar"], allowed_ip_ranges=["0.0.0.0/0"], daily_request_budget=50)
        with django_assert_max_num_queries(3):
            api_key = services.verify_token(issued.plaintext_token)
            assert api_key is not None
            list(api_key.social_accounts.all())
            _ = api_key.workspace.name, api_key.issued_by.email

    def test_the_permission_intersection_reuses_the_membership_verify_token_read(
        self, world, django_assert_num_queries
    ):
        from apps.api.auth import _resolve_effective_permissions

        issued, _ = make_token(world, ["read_calendar"])
        api_key = services.verify_token(issued.plaintext_token)
        with django_assert_num_queries(0):
            _resolve_effective_permissions(api_key)

    def test_a_deactivated_issuer_or_an_archived_workspace_kills_the_token(self, world):
        issued, _ = make_token(world, ["read_calendar"])
        world.workspace.is_archived = True
        world.workspace.save(update_fields=["is_archived"])
        assert services.verify_token(issued.plaintext_token) is None
        world.workspace.is_archived = False
        world.workspace.save(update_fields=["is_archived"])
        world.owner.is_active = False
        world.owner.save(update_fields=["is_active"])
        assert services.verify_token(issued.plaintext_token) is None
