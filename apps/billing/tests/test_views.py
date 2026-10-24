"""The Plan & billing page, Stripe redirects, and limits enforced in the web UI."""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

import pytest
import stripe
from django.contrib.messages import get_messages
from django.urls import reverse
from django.utils import timezone

from apps.api_keys.models import ApiKey
from apps.billing import entitlements
from apps.billing.models import Subscription
from apps.composer.models import PlatformPost
from apps.composer.services import create_post
from apps.social_accounts.models import SocialAccount
from apps.social_accounts.views import _create_or_update_account
from apps.workspaces.models import Workspace
from providers.types import AccountProfile

from .support import expire_trial, make_channels, set_plan


def _messages(response) -> list[str]:
    return [str(m) for m in get_messages(response.wsgi_request)]


# ---------------------------------------------------------------------------
# Plan & billing page
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestOverview:
    def test_shows_trial_plans_and_usage(self, billing, owner_client, workspace):
        make_channels(workspace, 2)
        response = owner_client.get(reverse("billing:overview"))
        assert response.status_code == 200
        body = response.content.decode()
        assert "14 days left in your Team trial" in body
        for name in ("Free", "Creator", "Team", "Agency"):
            assert name in body
        assert "Unlimited team members" in body
        assert 'max="25"' in body and 'value="2"' in body  # channel meter
        assert "Plan &amp; billing" in body  # settings sidebar link

    def test_over_limit_explains_what_to_remove(self, billing, owner_client, organization, workspace):
        expire_trial(organization)
        make_channels(workspace, 5)
        body = owner_client.get(reverse("billing:overview")).content.decode()
        assert "over its Free plan" in body
        assert "Over by 2. Disconnect channels to add more." in body

    def test_grace_period_banner(self, billing, owner_client, organization):
        set_plan(
            organization,
            "team",
            status=Subscription.Status.PAST_DUE,
            stripe_subscription_id="sub_1",
            grace_until=timezone.now() + timedelta(days=3),
        )
        assert "Your last payment failed." in owner_client.get(reverse("billing:overview")).content.decode()

    def test_members_cannot_see_billing(self, billing, member_client):
        assert member_client.get(reverse("billing:overview")).status_code == 403

    def test_hidden_when_billing_disabled(self, owner_client):
        assert owner_client.get(reverse("billing:overview")).status_code == 404
        body = owner_client.get(reverse("organizations:settings")).content.decode()
        assert "Plan &amp; billing" not in body


@pytest.mark.django_db
class TestStripeRedirects:
    def test_checkout_starts_a_stripe_session(self, billing, owner_client, organization):
        with mock.patch("apps.billing.services.stripe") as stripe_mock:
            stripe_mock.Customer.create.return_value = {"id": "cus_new"}
            stripe_mock.checkout.Session.create.return_value = {"url": "https://checkout.stripe.com/c/pay/cs_1"}
            response = owner_client.post(reverse("billing:checkout"), {"plan": "team", "interval": "year"})
        assert response.status_code == 302
        assert response["Location"] == "https://checkout.stripe.com/c/pay/cs_1"

        params = stripe_mock.checkout.Session.create.call_args.kwargs
        assert params["mode"] == "subscription"
        assert params["customer"] == "cus_new"
        assert params["line_items"] == [{"price": "price_team_y", "quantity": 1}]
        assert params["allow_promotion_codes"] is True
        assert params["client_reference_id"] == str(organization.id)
        # Subscribing mid-trial defers the first charge to the trial's end.
        sub = Subscription.objects.get(organization=organization)
        assert params["subscription_data"]["trial_end"] == int(sub.trial_ends_at.timestamp())
        assert params["success_url"].startswith("https://studio.example.com/organizations/billing/")
        assert sub.stripe_customer_id == "cus_new"

    def test_existing_customer_is_reused_and_expired_trial_gets_no_trial(self, billing, owner_client, organization):
        expire_trial(organization)
        Subscription.objects.filter(organization=organization).update(stripe_customer_id="cus_old")
        with mock.patch("apps.billing.services.stripe") as stripe_mock:
            stripe_mock.checkout.Session.create.return_value = {"url": "https://checkout.stripe.com/x"}
            owner_client.post(reverse("billing:checkout"), {"plan": "creator", "interval": "month"})
        stripe_mock.Customer.create.assert_not_called()
        params = stripe_mock.checkout.Session.create.call_args.kwargs
        assert params["customer"] == "cus_old"
        assert "trial_end" not in params["subscription_data"]

    def test_existing_subscription_changes_plan_with_proration(self, billing, owner_client, organization):
        set_plan(organization, "creator", interval="month", stripe_customer_id="cus_1", stripe_subscription_id="sub_1")
        updated = {
            "id": "sub_1",
            "customer": "cus_1",
            "status": "active",
            "items": {"data": [{"id": "si_1", "price": {"id": "price_agency_m"}}]},
        }
        with mock.patch("apps.billing.services.stripe") as stripe_mock:
            stripe_mock.Subscription.retrieve.return_value = {"items": {"data": [{"id": "si_1"}]}}
            stripe_mock.Subscription.modify.return_value = updated
            response = owner_client.post(reverse("billing:checkout"), {"plan": "agency", "interval": "month"})
        assert response["Location"] == reverse("billing:overview")
        stripe_mock.checkout.Session.create.assert_not_called()
        kwargs = stripe_mock.Subscription.modify.call_args.kwargs
        assert kwargs["items"] == [{"id": "si_1", "price": "price_agency_m"}]
        assert kwargs["proration_behavior"] == "create_prorations"
        assert Subscription.objects.get(organization=organization).plan == "agency"

    @pytest.mark.parametrize("status", [Subscription.Status.UNPAID, Subscription.Status.PAUSED])
    def test_unpaid_or_paused_subscription_goes_to_the_portal_not_a_second_checkout(
        self, billing, owner_client, organization, status
    ):
        set_plan(organization, "creator", status=status, stripe_customer_id="cus_1", stripe_subscription_id="sub_1")
        with mock.patch("apps.billing.services.stripe") as stripe_mock:
            stripe_mock.billing_portal.Session.create.return_value = {"url": "https://billing.stripe.com/p/1"}
            response = owner_client.post(reverse("billing:checkout"), {"plan": "team", "interval": "month"})
        assert response["Location"] == "https://billing.stripe.com/p/1"
        stripe_mock.checkout.Session.create.assert_not_called()
        stripe_mock.Subscription.modify.assert_not_called()

    @pytest.mark.parametrize("data", [{"plan": "free", "interval": "month"}, {"plan": "team", "interval": "weekly"}])
    def test_invalid_choice_is_refused(self, billing, owner_client, data):
        with mock.patch("apps.billing.services.stripe") as stripe_mock:
            response = owner_client.post(reverse("billing:checkout"), data)
        assert response["Location"] == reverse("billing:overview")
        stripe_mock.checkout.Session.create.assert_not_called()

    def test_stripe_outage_is_reported_not_raised(self, billing, owner_client):
        with mock.patch("apps.billing.services.stripe") as stripe_mock:
            stripe_mock.Customer.create.side_effect = stripe.StripeError("boom")
            response = owner_client.post(reverse("billing:checkout"), {"plan": "team", "interval": "month"})
        assert "couldn't reach our payment provider" in " ".join(_messages(response))

    def test_missing_price_id_is_reported(self, billing, owner_client, settings):
        settings.STRIPE_PRICE_IDS = {}
        response = owner_client.post(reverse("billing:checkout"), {"plan": "team", "interval": "month"})
        assert "isn't set up" in " ".join(_messages(response))

    def test_portal_redirects_to_stripe(self, billing, owner_client, organization):
        set_plan(organization, "team", stripe_customer_id="cus_1")
        with mock.patch("apps.billing.services.stripe") as stripe_mock:
            stripe_mock.billing_portal.Session.create.return_value = {"url": "https://billing.stripe.com/p/1"}
            response = owner_client.post(reverse("billing:portal"))
        assert response["Location"] == "https://billing.stripe.com/p/1"
        assert stripe_mock.billing_portal.Session.create.call_args.kwargs["customer"] == "cus_1"

    def test_portal_without_customer(self, billing, owner_client):
        response = owner_client.post(reverse("billing:portal"))
        assert "no billing account" in " ".join(_messages(response))

    def test_members_cannot_start_checkout(self, billing, member_client):
        assert member_client.post(reverse("billing:checkout"), {"plan": "team"}).status_code == 403


# ---------------------------------------------------------------------------
# Enforcement in the web UI
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestUiEnforcement:
    def test_workspace_creation_respects_the_limit(self, billing, owner_client, organization):
        expire_trial(organization)
        response = owner_client.post(reverse("workspaces:create"), {"name": "Second"})
        assert response["Location"] == reverse("organizations:workspaces")
        assert "Archive 1 workspace" in " ".join(_messages(response))
        assert Workspace.objects.filter(organization=organization).count() == 1

        set_plan(organization, "creator")
        owner_client.post(reverse("workspaces:create"), {"name": "Second"})
        assert Workspace.objects.filter(organization=organization).count() == 2

    def test_restoring_an_archived_workspace_respects_the_limit(self, billing, owner_client, organization, owner):
        from apps.members.models import WorkspaceMembership

        archived = Workspace.objects.create(organization=organization, name="Old", is_archived=True)
        WorkspaceMembership.objects.create(
            user=owner, workspace=archived, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
        )
        expire_trial(organization)
        owner_client.post(reverse("workspaces:settings", args=[archived.id]), {"action": "unarchive_workspace"})
        archived.refresh_from_db()
        assert archived.is_archived

    def test_connect_is_refused_before_the_oauth_round_trip(self, billing, owner_client, organization, workspace):
        expire_trial(organization)
        make_channels(workspace, 3)
        with mock.patch("apps.social_accounts.views._get_configured_platforms", return_value={"linkedin_personal"}):
            response = owner_client.post(
                reverse("social_accounts:connect", args=[workspace.id]), {"platform": "linkedin_personal"}
            )
        assert response["Location"] == reverse("social_accounts:connect", args=[workspace.id])
        assert "Free plan includes 3 channels" in " ".join(_messages(response))

    def test_new_channel_refused_but_reconnect_allowed(self, billing, organization, workspace):
        expire_trial(organization)
        existing = make_channels(workspace, 3)[0]

        def connect(platform_id):
            profile = AccountProfile(platform_id=platform_id, name="X", handle="x", avatar_url="", follower_count=0)
            with mock.patch("apps.social_accounts.views.subscribe_account_webhooks_task"):
                return _create_or_update_account(
                    workspace_id=workspace.id, platform="linkedin_personal", profile=profile, access_token="t"
                )

        with pytest.raises(entitlements.PlanLimitError):
            connect("brand-new")
        assert connect(existing.account_platform_id).pk == existing.pk
        assert SocialAccount.objects.filter(workspace=workspace).count() == 3

    def test_composer_refuses_scheduling_past_the_allowance(self, billing, owner_client, organization, workspace):
        expire_trial(organization)
        account = make_channels(workspace, 1)[0]
        when = timezone.localtime(timezone.now() + timedelta(days=1))
        if when.month != timezone.localtime(timezone.now()).month:
            when = timezone.localtime(timezone.now() + timedelta(minutes=30))
        for _ in range(30):
            create_post(workspace=workspace, social_account=account, caption="x", scheduled_at=when, status="scheduled")
        response = owner_client.post(
            reverse("composer:save_post", args=[workspace.id]),
            {
                "action": "schedule",
                "caption": "One too many",
                "selected_accounts": str(account.id),
                "scheduled_date": when.strftime("%Y-%m-%d"),
                "scheduled_time": when.strftime("%H:%M"),
            },
            HTTP_HX_REQUEST="true",
        )
        assert response.status_code == 402
        assert "30 scheduled posts per month" in response.json()["errors"]["plan"]
        assert PlatformPost.objects.filter(social_account=account).count() == 30

    @pytest.mark.parametrize("url_name", ["inbox:feed", "analytics:index"])
    def test_team_sections_are_locked_below_team(self, billing, owner_client, organization, workspace, url_name):
        set_plan(organization, "creator")
        response = owner_client.get(reverse(url_name, args=[workspace.id]))
        assert response.status_code == 402
        assert "Upgrade to Team or higher" in response.content.decode()

        set_plan(organization, "team")
        assert owner_client.get(reverse(url_name, args=[workspace.id])).status_code != 402

    def test_client_portal_admin_needs_agency(self, billing, owner_client, organization, workspace):
        set_plan(organization, "team")
        url = reverse("client_portal_admin:client_list", args=[workspace.id])
        assert owner_client.get(url).status_code == 402
        set_plan(organization, "agency")
        assert owner_client.get(url).status_code == 200

    def test_locked_page_tells_members_to_ask_an_admin(self, billing, member_client, organization, workspace):
        set_plan(organization, "creator")
        body = member_client.get(reverse("analytics:index", args=[workspace.id])).content.decode()
        assert "Ask an organization admin" in body

    def test_approval_workflow_needs_team(self, billing, owner_client, organization, workspace):
        url = reverse("workspaces:approvals_settings", args=[workspace.id])
        set_plan(organization, "creator")
        owner_client.post(url, {"approval_workflow_mode": "required_internal"})
        workspace.refresh_from_db()
        assert workspace.approval_workflow_mode == "none"

        set_plan(organization, "team")
        owner_client.post(url, {"approval_workflow_mode": "required_internal_and_client"})
        workspace.refresh_from_db()
        assert workspace.approval_workflow_mode == "none"  # client sign-off needs the portal (Agency)
        owner_client.post(url, {"approval_workflow_mode": "required_internal"})
        workspace.refresh_from_db()
        assert workspace.approval_workflow_mode == "required_internal"

        # After a downgrade the existing workflow stays, and can be turned off.
        set_plan(organization, "creator")
        owner_client.post(url, {"approval_workflow_mode": "required_internal"})
        owner_client.post(url, {"approval_workflow_mode": "none"})
        workspace.refresh_from_db()
        assert workspace.approval_workflow_mode == "none"

    def test_api_keys_can_be_issued_on_free(self, billing, owner_client, organization, workspace):
        expire_trial(organization)
        account = make_channels(workspace, 1)[0]
        owner_client.post(
            reverse("api_keys:issue"),
            {"name": "agent", "workspace_id": str(workspace.id), "social_account_ids": [str(account.id)]},
        )
        assert ApiKey.objects.filter(workspace=workspace).exists()

    def test_intelligence_billing_is_hidden(self, billing, organization, rf):
        from apps.intelligence.context_processors import intelligence_flag

        billing.INTELLIGENCE_ENABLED = True
        assert intelligence_flag(rf.get("/")) == {"INTELLIGENCE_ENABLED": False}
        assert organization.has_intelligence is False


@pytest.mark.django_db
class TestBillingDisabledUnchanged:
    def test_no_limits_no_rows_no_billing_ui(self, owner_client, organization, workspace):
        make_channels(workspace, 10)
        for name in ("Two", "Three"):
            owner_client.post(reverse("workspaces:create"), {"name": name})
        assert Workspace.objects.filter(organization=organization).count() == 3
        assert owner_client.get(reverse("inbox:feed", args=[workspace.id])).status_code == 200
        assert owner_client.get(reverse("client_portal_admin:client_list", args=[workspace.id])).status_code == 200
        assert not Subscription.objects.exists()


@pytest.mark.django_db
class TestCsvImportPlanLimit:
    """A CSV import that would pass the monthly allowance is refused whole, not stopped partway."""

    def _put_csv(self, client, workspace, rows):
        session = client.session
        session[f"csv_import_{workspace.id}"] = {"headers": ["caption", "date", "platform"], "rows": rows}
        session[f"csv_mapping_{workspace.id}"] = {"caption": 0, "date": 1, "platforms": 2}
        session.save()

    def test_refuses_the_whole_import_before_creating_any_post(self, billing, owner_client, organization, workspace):
        expire_trial(organization)
        make_channels(workspace, 1)
        day = (timezone.now() + timedelta(days=40)).strftime("%Y-%m-%d")
        rows = [[f"Post {i}", day, "linkedin_personal"] for i in range(31)]
        self._put_csv(owner_client, workspace, rows)
        response = owner_client.post(reverse("composer:csv_confirm_import", args=[workspace.id]))
        assert response.status_code == 200
        assert "30 scheduled posts per month" in response.content.decode()
        assert "No posts were imported" in response.content.decode()
        assert not PlatformPost.objects.filter(post__workspace=workspace).exists()
        assert f"csv_import_{workspace.id}" not in owner_client.session

    def test_import_within_the_allowance_proceeds(self, billing, owner_client, organization, workspace):
        expire_trial(organization)
        make_channels(workspace, 1)
        day = (timezone.now() + timedelta(days=40)).strftime("%Y-%m-%d")
        self._put_csv(owner_client, workspace, [[f"Post {i}", day, "linkedin_personal"] for i in range(30)])
        response = owner_client.post(reverse("composer:csv_confirm_import", args=[workspace.id]))
        assert response.context["created_count"] == 30
