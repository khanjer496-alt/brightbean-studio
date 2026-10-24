"""Phase 4 — org-level API key UI views.

Covers:
  * List page renders for an org admin and 403s for a member.
  * The HTMX cascade returns the right accounts + grantable permissions
    for the picked workspace.
  * Issuance happy path renders the one-time reveal modal and creates an
    ``ApiKey`` row.
  * Issuance enforces server-side that workspace + accounts belong to
    the caller's org (tamper resistance).
  * Revoke flips ``revoked_at`` and surfaces 404 for foreign-org keys.
"""

from __future__ import annotations

import re
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.api_keys.models import ApiKey
from apps.members.models import (
    PERMISSION_KEYS,
    OrgMembership,
    WorkspaceMembership,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def admin_user(db):
    from apps.accounts.models import User

    return User.objects.create_user(
        email="ui-admin@example.com",
        password="testpass123",
        name="UI Admin",
        tos_accepted_at=timezone.now(),
    )


@pytest.fixture
def member_user(db):
    from apps.accounts.models import User

    return User.objects.create_user(
        email="ui-member@example.com",
        password="testpass123",
        name="UI Member",
        tos_accepted_at=timezone.now(),
    )


@pytest.fixture
def organization(admin_user):
    """Use the OrgMembership auto-created by ``apps.accounts.signals`` so
    that RBACMiddleware picks the same one our tests do. Creating a
    second ``Organization`` here would diverge from the real signup
    flow — the middleware uses ``.first()`` and would resolve the
    auto-created "My Organization" instead, breaking every workspace
    lookup downstream.
    """
    return admin_user.org_memberships.first().organization


@pytest.fixture
def workspace(db, organization):
    from apps.workspaces.models import Workspace

    return Workspace.objects.create(name="UI Workspace", organization=organization)


@pytest.fixture
def admin_memberships(db, admin_user, organization, workspace):
    # OrgMembership is already created by the signup signal — no need
    # to add a second one. We only need the WorkspaceMembership.
    return WorkspaceMembership.objects.create(
        user=admin_user,
        workspace=workspace,
        workspace_role=WorkspaceMembership.WorkspaceRole.OWNER,
    )


@pytest.fixture
def member_membership(db, member_user):
    """Downgrade ``member_user``'s auto-created OrgMembership to MEMBER.

    They live in their own ``My Organization`` (the signup signal made
    one). For the "forbidden" check we don't need them in admin's org —
    just somewhere where they lack ``manage_api_keys``.
    """
    membership = member_user.org_memberships.first()
    membership.org_role = OrgMembership.OrgRole.MEMBER
    membership.save(update_fields=["org_role"])
    return membership


@pytest.fixture
def social_account(db, workspace):
    from apps.social_accounts.models import SocialAccount

    return SocialAccount.objects.create(
        workspace=workspace,
        platform="linkedin_personal",
        account_platform_id="li-ui",
        account_name="LinkedIn UI",
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )


@pytest.fixture
def disconnected_account(db, workspace):
    from apps.social_accounts.models import SocialAccount

    return SocialAccount.objects.create(
        workspace=workspace,
        platform="linkedin_personal",
        account_platform_id="li-broken",
        account_name="Broken LinkedIn",
        connection_status=SocialAccount.ConnectionStatus.DISCONNECTED,
    )


@pytest.fixture
def admin_client(client, admin_user, admin_memberships):
    client.force_login(admin_user)
    return client


@pytest.fixture
def member_client(client, member_user, member_membership):
    client.force_login(member_user)
    return client


# ---------------------------------------------------------------------------
# List page
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestListPage:
    def test_admin_can_view_list(self, admin_client):
        r = admin_client.get(reverse("api_keys:list"))
        assert r.status_code == 200
        assert b"API Keys" in r.content
        # Empty state — no rows yet.
        assert b"No API keys issued yet." in r.content

    def test_active_key_row_uses_modal_not_native_confirm(self, admin_client, admin_user, workspace, social_account):
        """An active key renders the Alpine revoke-confirmation modal rather
        than the old native ``confirm()`` dialog.

        ``_key_row.html`` is the only place an active row is rendered, and no
        other test exercised it — this guards both the CSP-safe modal markup
        (Alpine directives, no inline ``on*`` handlers) and the removal of the
        ``confirm()`` call.
        """
        from apps.api_keys import services

        services.issue_api_key(
            workspace=workspace,
            social_accounts=[social_account],
            issued_by=admin_user,
            name="render-me",
            permissions=[],
        )
        r = admin_client.get(reverse("api_keys:list"))
        assert r.status_code == 200
        html = r.content.decode()
        # The active row and its teleported confirmation modal rendered.
        assert "render-me" in html
        assert "showRevokeModal" in html
        assert 'x-teleport="body"' in html
        assert "Revoke key" in html  # modal's submit button
        # The native confirm() dialog wiring is gone.
        assert "if (confirm(" not in html
        # The explanatory comment must NOT leak into the page. A multi-line
        # ``{# #}`` comment renders as literal text (Django only treats the
        # hash form as a comment on a single line); here that stray text also
        # became a flex child that squeezed the chip column.
        assert "Alpine-driven confirmation modal" not in html

    def test_member_without_manage_api_keys_is_forbidden(self, member_user, member_membership):
        from django.test import Client

        from apps.members.models import has_org_permission

        # Sanity-check: the membership is MEMBER and lacks manage_api_keys.
        assert member_membership.org_role == OrgMembership.OrgRole.MEMBER
        assert not has_org_permission(member_membership, "manage_api_keys")

        c = Client()
        c.force_login(member_user)
        r = c.get(reverse("api_keys:list"))
        assert r.status_code == 403, (
            f"expected 403 but got {r.status_code} — has_org_permission "
            f"is False for this membership, so the decorator should raise"
        )

    def test_anonymous_redirects_to_login(self, client):
        r = client.get(reverse("api_keys:list"))
        # @login_required redirects to the login page.
        assert r.status_code == 302
        assert "/accounts/login" in r.headers["Location"]


# ---------------------------------------------------------------------------
# HTMX cascade — workspace → accounts + permissions
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestWorkspaceOptionsPartial:
    def test_returns_connected_accounts_only(self, admin_client, workspace, social_account, disconnected_account):
        r = admin_client.get(
            reverse("api_keys:workspace_options"),
            {"workspace_id": str(workspace.id)},
        )
        assert r.status_code == 200
        body = r.content.decode()
        # Connected account appears; disconnected does NOT — so admins
        # can't mint a key against an account known to be broken.
        assert social_account.account_name in body
        assert disconnected_account.account_name not in body

    def test_blank_workspace_id_returns_empty(self, admin_client):
        r = admin_client.get(reverse("api_keys:workspace_options"))
        assert r.status_code == 200
        assert r.content == b""

    def test_foreign_workspace_returns_empty(self, admin_client, db):
        """A workspace in another org must not leak account info."""
        from apps.organizations.models import Organization
        from apps.workspaces.models import Workspace

        other_org = Organization.objects.create(name="Other")
        foreign_ws = Workspace.objects.create(name="Foreign", organization=other_org)
        r = admin_client.get(
            reverse("api_keys:workspace_options"),
            {"workspace_id": str(foreign_ws.id)},
        )
        # Empty body — same UX as "no workspace picked".
        assert r.content == b""

    def test_permissions_intersected_with_issuer(self, admin_client, workspace, social_account):
        """An owner sees every grantable permission; a non-owner doesn't."""
        from apps.api_keys.views import _HIDDEN_FROM_ISSUANCE

        r = admin_client.get(
            reverse("api_keys:workspace_options"),
            {"workspace_id": str(workspace.id)},
        )
        body = r.content.decode()
        # An owner holds every key in PERMISSION_KEYS, but the issuance picker
        # hides keys whose feature hasn't shipped yet (e.g. view_analytics).
        for k in PERMISSION_KEYS:
            if k in _HIDDEN_FROM_ISSUANCE:
                assert k not in body, f"hidden permission {k} should not appear"
            else:
                assert k in body, f"missing permission {k}"


# ---------------------------------------------------------------------------
# Issue
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestIssue:
    def test_happy_path_creates_key_and_reveals_token(self, admin_client, workspace, social_account):
        r = admin_client.post(
            reverse("api_keys:issue"),
            {
                "name": "test bot",
                "workspace_id": str(workspace.id),
                "social_account_ids": [str(social_account.id)],
                "permissions": ["create_posts"],
            },
            follow=True,
        )
        # Surface the flash messages so a real error doesn't get hidden
        # behind a generic status assertion.
        assert r.status_code == 200, r.content[:300]
        body = r.content.decode()
        # Reveal modal markers.
        assert "Save this token now." in body
        assert "bb_studio_" in body
        # Row persisted.
        keys = list(ApiKey.objects.filter(workspace=workspace))
        assert len(keys) == 1
        assert keys[0].name == "test bot"
        assert "create_posts" in (keys[0].permissions or [])

    def test_issue_redirects_and_reload_does_not_resurface_or_reissue(self, admin_client, workspace, social_account):
        """Post/Redirect/Get: issuing lands on the list (not the POST
        endpoint), reveals the token exactly once, and a reload neither
        re-shows the modal nor mints a second key.
        """
        payload = {
            "name": "reload bot",
            "workspace_id": str(workspace.id),
            "social_account_ids": [str(social_account.id)],
            "permissions": ["create_posts"],
        }
        # Without follow we can see the redirect itself (PRG).
        r0 = admin_client.post(reverse("api_keys:issue"), payload)
        assert r0.status_code == 302
        assert r0.headers["Location"] == reverse("api_keys:list")
        # And the plaintext token is NOT carried in the redirect URL.
        assert "bb_studio_" not in r0.headers["Location"]

        # Following the redirect reveals the token once.
        r1 = admin_client.get(r0.headers["Location"])
        body1 = r1.content.decode()
        assert "Save this token now." in body1
        assert "bb_studio_" in body1
        assert ApiKey.objects.filter(workspace=workspace).count() == 1

        # Reloading the list page: token was popped, so no modal — and,
        # crucially, no second key was created (the old non-PRG flow
        # re-POSTed and minted a fresh key on every refresh).
        r2 = admin_client.get(reverse("api_keys:list"))
        body2 = r2.content.decode()
        assert "Save this token now." not in body2
        assert "bb_studio_" not in body2
        assert ApiKey.objects.filter(workspace=workspace).count() == 1

    def test_missing_name_is_rejected(self, admin_client, workspace, social_account):
        r = admin_client.post(
            reverse("api_keys:issue"),
            {
                "name": "",
                "workspace_id": str(workspace.id),
                "social_account_ids": [str(social_account.id)],
            },
            follow=True,
        )
        assert r.status_code == 200
        assert ApiKey.objects.count() == 0
        assert b"Name is required" in r.content

    def test_htmx_rejection_returns_inline_field_errors_and_creates_nothing(self, admin_client, workspace):
        """The dialog posts with htmx: a refusal is a 422 carrying one message box per field (swapped out of
        band), so the open dialog keeps everything the user typed."""
        r = admin_client.post(
            reverse("api_keys:issue"),
            {"name": "", "workspace_id": str(workspace.id), "expires_at": "not-a-date"},
            HTTP_HX_REQUEST="true",
        )
        body = r.content.decode()
        assert r.status_code == 422
        assert ApiKey.objects.count() == 0
        assert re.search(r'id="issue-error-name"[^>]*>\s*<p>Name is required\.</p>', body)
        assert re.search(r'id="issue-error-social_account_ids"[^>]*>\s*<p>Select at least one connected account\.</p>', body)
        assert re.search(r'id="issue-error-workspace_id"[^>]*>\s*</div>', body)  # fine fields are cleared

    def test_htmx_success_redirects_to_the_list(self, admin_client, workspace, social_account):
        r = admin_client.post(
            reverse("api_keys:issue"),
            {"name": "bot", "workspace_id": str(workspace.id), "social_account_ids": [str(social_account.id)]},
            HTTP_HX_REQUEST="true",
        )
        assert r.status_code == 204
        assert r["HX-Redirect"] == reverse("api_keys:list")
        assert ApiKey.objects.filter(workspace=workspace).count() == 1

    def test_issue_dialog_posts_with_htmx_and_has_a_message_box_per_field(self, admin_client):
        body = admin_client.get(reverse("api_keys:list")).content.decode()
        assert 'hx-post="' + reverse("api_keys:issue") + '"' in body
        for field in ("name", "workspace_id", "social_account_ids", "scope", "expires_at"):
            assert f'id="issue-error-{field}"' in body
        assert 'id="issue-errors"' in body

    def test_foreign_workspace_is_rejected(self, admin_client, social_account, db):
        """A tampered POST naming a workspace from another org must fail
        defence-in-depth, not just rely on the UI dropdown.
        """
        from apps.organizations.models import Organization
        from apps.workspaces.models import Workspace

        other_org = Organization.objects.create(name="Other")
        foreign_ws = Workspace.objects.create(name="Foreign", organization=other_org)
        r = admin_client.post(
            reverse("api_keys:issue"),
            {
                "name": "tamper",
                "workspace_id": str(foreign_ws.id),
                "social_account_ids": [str(social_account.id)],
            },
            follow=True,
        )
        assert ApiKey.objects.count() == 0
        assert b"not in this organisation" in r.content

    def test_account_outside_workspace_rejected(self, admin_client, workspace, db):
        """Social account from a different workspace must not be accepted
        even if the workspace_id is legitimate.
        """
        from apps.social_accounts.models import SocialAccount
        from apps.workspaces.models import Workspace

        other_ws = Workspace.objects.create(name="Other WS", organization=workspace.organization)
        foreign_sa = SocialAccount.objects.create(
            workspace=other_ws,
            platform="linkedin_personal",
            account_platform_id="li-foreign",
            account_name="Foreign LinkedIn",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        r = admin_client.post(
            reverse("api_keys:issue"),
            {
                "name": "tamper-account",
                "workspace_id": str(workspace.id),
                "social_account_ids": [str(foreign_sa.id)],
            },
            follow=True,
        )
        assert ApiKey.objects.count() == 0
        assert b"do not belong to that workspace" in r.content

    def test_malformed_account_id_redirects_not_500(self, admin_client, workspace, social_account):
        """A tampered, non-UUID ``social_account_ids`` must yield a graceful
        validation error + redirect, not a 500 from UUIDField coercion.
        """
        r = admin_client.post(
            reverse("api_keys:issue"),
            {
                "name": "tamper-uuid",
                "workspace_id": str(workspace.id),
                "social_account_ids": ["not-a-uuid"],
            },
            follow=True,
        )
        assert r.status_code == 200  # followed the redirect — not a 500
        assert b"do not belong to that workspace" in r.content
        assert ApiKey.objects.count() == 0


# ---------------------------------------------------------------------------
# Revoke
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRevoke:
    def test_revoke_flips_revoked_at(self, admin_client, admin_user, workspace, social_account):
        from apps.api_keys import services

        issued = services.issue_api_key(
            workspace=workspace,
            social_accounts=[social_account],
            issued_by=admin_user,
            name="to-revoke",
            permissions=[],
        )
        r = admin_client.post(
            reverse("api_keys:revoke", args=[issued.api_key.id]),
        )
        assert r.status_code == 302  # redirect back to list
        issued.api_key.refresh_from_db()
        assert issued.api_key.revoked_at is not None

    def test_revoke_foreign_org_key_404s(self, admin_client, admin_user, workspace, social_account, db):
        """Trying to revoke a key in another org must 404, not silently
        succeed and not 500.
        """
        from apps.accounts.models import User
        from apps.api_keys import services
        from apps.organizations.models import Organization
        from apps.social_accounts.models import SocialAccount
        from apps.workspaces.models import Workspace

        other_org = Organization.objects.create(name="Other")
        foreign_ws = Workspace.objects.create(name="Foreign", organization=other_org)
        foreign_user = User.objects.create_user(
            email="foreign@example.com",
            password="testpass123",
            name="Foreign",
            tos_accepted_at=timezone.now(),
        )
        OrgMembership.objects.create(
            user=foreign_user,
            organization=other_org,
            org_role=OrgMembership.OrgRole.OWNER,
        )
        WorkspaceMembership.objects.create(
            user=foreign_user,
            workspace=foreign_ws,
            workspace_role=WorkspaceMembership.WorkspaceRole.OWNER,
        )
        foreign_sa = SocialAccount.objects.create(
            workspace=foreign_ws,
            platform="linkedin_personal",
            account_platform_id="li-other",
            account_name="Other LinkedIn",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        foreign_key = services.issue_api_key(
            workspace=foreign_ws,
            social_accounts=[foreign_sa],
            issued_by=foreign_user,
            name="other-org-key",
            permissions=[],
        )

        r = admin_client.post(
            reverse("api_keys:revoke", args=[foreign_key.api_key.id]),
        )
        assert r.status_code == 404
        foreign_key.api_key.refresh_from_db()
        assert foreign_key.api_key.revoked_at is None


# ---------------------------------------------------------------------------
# Edit
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestEditKey:
    def _issue(self, admin_user, workspace, social_account, permissions):
        from apps.api_keys import services

        return services.issue_api_key(
            workspace=workspace,
            social_accounts=[social_account],
            issued_by=admin_user,
            name="editable",
            permissions=permissions,
        ).api_key

    def test_get_is_not_allowed(self, admin_client, admin_user, workspace, social_account):
        key = self._issue(admin_user, workspace, social_account, [])
        r = admin_client.get(reverse("api_keys:edit", args=[key.id]))
        assert r.status_code == 405

    def test_member_without_manage_api_keys_is_forbidden(self, member_client):
        from uuid import uuid4

        # The decorator runs before the key lookup, so a random id still 403s.
        r = member_client.post(reverse("api_keys:edit", args=[uuid4()]))
        assert r.status_code == 403

    def test_success_updates_permissions_accounts_and_expiry(self, admin_client, admin_user, workspace, social_account):
        from apps.social_accounts.models import SocialAccount

        second = SocialAccount.objects.create(
            workspace=workspace,
            platform="linkedin_personal",
            account_platform_id="li-second-ui",
            account_name="Second UI",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        key = self._issue(admin_user, workspace, social_account, ["create_posts"])
        r = admin_client.post(
            reverse("api_keys:edit", args=[key.id]),
            {
                "permissions": ["approve_posts"],
                "social_account_ids": [str(second.id)],
                "expires_at": "2030-01-01",
            },
        )
        assert r.status_code == 302
        assert r.headers["Location"] == reverse("api_keys:list")
        key.refresh_from_db()
        assert key.permissions == ["approve_posts"]
        assert set(key.social_accounts.values_list("id", flat=True)) == {second.id}
        assert key.expires_at is not None
        assert key.expires_at.year == 2030

    def test_empty_accounts_is_rejected(self, admin_client, admin_user, workspace, social_account):
        key = self._issue(admin_user, workspace, social_account, ["create_posts"])
        r = admin_client.post(
            reverse("api_keys:edit", args=[key.id]),
            {"permissions": ["create_posts"]},  # no social_account_ids
            follow=True,
        )
        assert r.status_code == 200
        assert b"Select at least one connected account" in r.content
        key.refresh_from_db()
        # Unchanged.
        assert key.permissions == ["create_posts"]
        assert set(key.social_accounts.values_list("id", flat=True)) == {social_account.id}

    def test_empty_permissions_is_allowed(self, admin_client, admin_user, workspace, social_account):
        """Submitting no permission checkboxes clears the (grantable) set —
        an empty key is valid. admin_user is a workspace OWNER, so every perm
        is theirs to grant and nothing is preserved."""
        key = self._issue(admin_user, workspace, social_account, ["create_posts"])
        r = admin_client.post(
            reverse("api_keys:edit", args=[key.id]),
            {"social_account_ids": [str(social_account.id)]},  # no permissions
        )
        assert r.status_code == 302
        key.refresh_from_db()
        assert key.permissions == []

    def test_inactive_key_is_rejected(self, admin_client, admin_user, workspace, social_account):
        from apps.api_keys import services

        key = self._issue(admin_user, workspace, social_account, ["create_posts"])
        services.revoke_api_key(key)
        r = admin_client.post(
            reverse("api_keys:edit", args=[key.id]),
            {"permissions": [], "social_account_ids": [str(social_account.id)]},
            follow=True,
        )
        assert r.status_code == 200
        assert b"is not active" in r.content
        key.refresh_from_db()
        # Permissions untouched despite the POST.
        assert key.permissions == ["create_posts"]

    def test_cross_org_key_404s(self, admin_client, db):
        from apps.accounts.models import User
        from apps.api_keys import services
        from apps.organizations.models import Organization
        from apps.social_accounts.models import SocialAccount
        from apps.workspaces.models import Workspace

        other_org = Organization.objects.create(name="Other")
        foreign_ws = Workspace.objects.create(name="Foreign", organization=other_org)
        foreign_user = User.objects.create_user(
            email="foreign-edit@example.com",
            password="testpass123",
            name="Foreign Edit",
            tos_accepted_at=timezone.now(),
        )
        OrgMembership.objects.create(user=foreign_user, organization=other_org, org_role=OrgMembership.OrgRole.OWNER)
        WorkspaceMembership.objects.create(
            user=foreign_user,
            workspace=foreign_ws,
            workspace_role=WorkspaceMembership.WorkspaceRole.OWNER,
        )
        foreign_sa = SocialAccount.objects.create(
            workspace=foreign_ws,
            platform="linkedin_personal",
            account_platform_id="li-foreign-edit",
            account_name="Foreign Edit LinkedIn",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        foreign_key = services.issue_api_key(
            workspace=foreign_ws,
            social_accounts=[foreign_sa],
            issued_by=foreign_user,
            name="other-org-key",
            permissions=["create_posts"],
        ).api_key

        r = admin_client.post(
            reverse("api_keys:edit", args=[foreign_key.id]),
            {"permissions": [], "social_account_ids": [str(foreign_sa.id)]},
        )
        assert r.status_code == 404
        foreign_key.refresh_from_db()
        assert foreign_key.permissions == ["create_posts"]

    def test_list_renders_edit_modal(self, admin_client, admin_user, workspace, social_account):
        self._issue(admin_user, workspace, social_account, ["create_posts"])
        r = admin_client.get(reverse("api_keys:list"))
        html = r.content.decode()
        assert "showEditModal" in html
        assert "Edit API key" in html
        assert "Save changes" in html
        assert reverse("api_keys:edit", args=[ApiKey.objects.first().id]) in html

    def test_malformed_account_id_redirects_not_500(self, admin_client, admin_user, workspace, social_account):
        """A tampered, non-UUID ``social_account_ids`` must redirect with a
        validation error and leave the key untouched — not 500.
        """
        key = self._issue(admin_user, workspace, social_account, ["create_posts"])
        r = admin_client.post(
            reverse("api_keys:edit", args=[key.id]),
            {"permissions": ["create_posts"], "social_account_ids": ["not-a-uuid"]},
            follow=True,
        )
        assert r.status_code == 200  # followed the redirect — not a 500
        assert b"do not belong to that workspace" in r.content
        key.refresh_from_db()
        assert key.permissions == ["create_posts"]
        assert set(key.social_accounts.values_list("id", flat=True)) == {social_account.id}


# ---------------------------------------------------------------------------
# Scoped tokens — presets, capabilities, budgets, rotate, activity link
# ---------------------------------------------------------------------------


def _scoped_payload(workspace, social_account, **overrides):
    payload = {
        "name": "scoped bot",
        "workspace_id": str(workspace.id),
        "social_account_ids": [str(social_account.id)],
        "scoped": "1",
        "preset": "",
        "capabilities": ["read_calendar", "draft_posts"],
        "daily_request_budget": "500",
        "daily_ai_budget": "20",
        "daily_post_budget": "",
        "allowed_ip_ranges": "203.0.113.0/24, 198.51.100.7",
        "requires_approval": "on",
        "expires_at": "2099-01-01",
    }
    payload.update(overrides)
    return payload


@pytest.mark.django_db
class TestScopedIssue:
    def test_issues_a_token_with_its_scope_and_the_permissions_it_needs(self, admin_client, workspace, social_account):
        r = admin_client.post(reverse("api_keys:issue"), _scoped_payload(workspace, social_account), follow=True)
        assert r.status_code == 200
        key = ApiKey.objects.get(workspace=workspace)
        assert key.capabilities == ["read_calendar", "draft_posts"]
        assert (key.daily_request_budget, key.daily_ai_budget, key.daily_post_budget) == (500, 20, None)
        assert key.allowed_ip_ranges == ["203.0.113.0/24", "198.51.100.7/32"]
        assert key.requires_approval is True
        assert key.expires_at is not None
        assert key.permissions == ["create_posts"]  # what draft_posts needs, nothing more

    def test_a_preset_is_remembered_while_its_capabilities_match(self, admin_client, workspace, social_account):
        from apps.api_keys import capabilities as caps

        preset = caps.preset("read_only_analyst")
        payload = _scoped_payload(
            workspace, social_account, preset=preset.key, capabilities=list(preset.capabilities), requires_approval=""
        )
        admin_client.post(reverse("api_keys:issue"), payload)
        assert ApiKey.objects.get(workspace=workspace).preset == "read_only_analyst"

    def test_a_preset_is_forgotten_once_the_capabilities_differ(self, admin_client, workspace, social_account):
        payload = _scoped_payload(workspace, social_account, preset="read_only_analyst")
        admin_client.post(reverse("api_keys:issue"), payload)
        assert ApiKey.objects.get(workspace=workspace).preset == ""

    @pytest.mark.parametrize(
        ("override", "message"),
        [
            ({"capabilities": []}, "Choose at least one capability"),
            ({"daily_request_budget": "lots"}, "must be a whole number"),
            ({"daily_ai_budget": "-1"}, "must be a whole number"),
            ({"allowed_ip_ranges": "not-an-ip"}, "not an IP address or CIDR range"),
            ({"expires_at": "soon"}, "Could not parse expires_at"),
        ],
    )
    def test_bad_scope_input_is_refused_and_nothing_is_created(
        self, admin_client, workspace, social_account, override, message
    ):
        r = admin_client.post(
            reverse("api_keys:issue"), _scoped_payload(workspace, social_account, **override), follow=True
        )
        assert message in r.content.decode()
        assert not ApiKey.objects.filter(workspace=workspace).exists()

    def test_a_member_cannot_issue(self, member_client, workspace, social_account):
        r = member_client.post(reverse("api_keys:issue"), _scoped_payload(workspace, social_account))
        assert r.status_code == 403
        assert not ApiKey.objects.exists()

    def test_the_form_offers_the_presets_capabilities_and_limits(self, admin_client):
        body = admin_client.get(reverse("api_keys:list")).content.decode()
        for text in (
            "Read-only analyst",
            "Drafting assistant (needs approval)",
            "Inbox helper (drafts replies)",
            "Full agent (guardrails)",
            "Read posts &amp; calendar",
            "Read-only until approved",
            "Allowed IP ranges",
            "Daily limits",
        ):
            assert text in body
        assert "data-token-scope" in body and "js/token-scope.js" in body
        assert "style=" not in body.split("<main", 1)[-1].split("</main>", 1)[0]


@pytest.mark.django_db
class TestScopedList:
    def _issue(self, workspace, owner, social_account, **kwargs):
        from apps.api_keys import services

        return services.issue_api_key(
            workspace=workspace,
            social_accounts=[social_account],
            issued_by=owner,
            name=kwargs.pop("name", "listed"),
            permissions=["create_posts"],
            **kwargs,
        )

    def test_shows_capabilities_expiry_last_use_and_the_budgets_against_todays_usage(
        self, admin_client, admin_user, workspace, social_account
    ):
        from apps.api_keys.budgets import reserve_ai_call, reserve_scheduled_post

        issued = self._issue(
            workspace,
            admin_user,
            social_account,
            capabilities=["read_calendar", "use_ai_assistant"],
            daily_request_budget=100,
            daily_ai_budget=2,
            daily_post_budget=3,
            expires_at=timezone.now() + timedelta(days=30),
        )
        reserve_ai_call(issued.api_key)
        reserve_scheduled_post(issued.api_key)
        reserve_scheduled_post(issued.api_key)
        body = admin_client.get(reverse("api_keys:list")).content.decode()
        assert "Read posts &amp; calendar" in body and "AI writing assistant" in body
        assert "Expires" in body and "Never used" in body
        assert "0 of 100" in body and "1 of 2" in body and "2 of 3" in body

    def test_links_to_the_activity_page_filtered_by_this_token(
        self, admin_client, admin_user, workspace, social_account
    ):
        issued = self._issue(workspace, admin_user, social_account, capabilities=["read_calendar"])
        body = admin_client.get(reverse("api_keys:list")).content.decode()
        expected = f"{reverse('autonomy:activity', kwargs={'workspace_id': workspace.id})}?key={issued.api_key.id}"
        assert expected in body

    def test_the_activity_page_shows_only_that_tokens_actions(
        self, admin_client, admin_user, workspace, social_account
    ):
        from apps.autonomy.actors import agent
        from apps.autonomy.models import ActionType
        from apps.autonomy.service import record

        mine = self._issue(workspace, admin_user, social_account, capabilities=["read_calendar"], name="Mine")
        other = self._issue(workspace, admin_user, social_account, capabilities=["read_calendar"], name="Theirs")
        for issued in (mine, other):
            record(
                workspace=workspace,
                actor=agent(issued.api_key, admin_user),
                action_type=ActionType.DRAFT_CONTENT,
                operation="post.create.draft",
                notify=False,
            )
        url = reverse("autonomy:activity", kwargs={"workspace_id": workspace.id})
        body = admin_client.get(url, {"key": str(mine.api_key.id)}).content.decode()
        assert "Showing only what the token" in body
        assert body.count("Mine · Draft content") == 1 and "Theirs · Draft content" not in body
        both = admin_client.get(url).content.decode()
        assert "Mine · Draft content" in both and "Theirs · Draft content" in both

    def test_an_older_key_is_labelled_as_unlimited_and_its_form_shows_everything_ticked(
        self, admin_client, admin_user, workspace, social_account
    ):
        self._issue(workspace, admin_user, social_account)
        body = admin_client.get(reverse("api_keys:list")).content.decode()
        assert "made before tool-level scopes" in body


@pytest.mark.django_db
class TestScopedEditRotateRevoke:
    def _key(self, workspace, owner, social_account, **kwargs):
        from apps.api_keys import services

        return services.issue_api_key(
            workspace=workspace,
            social_accounts=[social_account],
            issued_by=owner,
            name="managed",
            permissions=["create_posts"],
            capabilities=["read_calendar"],
            **kwargs,
        )

    def test_edit_changes_the_scope_and_adds_the_permissions_it_needs(
        self, admin_client, admin_user, workspace, social_account
    ):
        issued = self._key(workspace, admin_user, social_account)
        r = admin_client.post(
            reverse("api_keys:edit", args=[issued.api_key.id]),
            _scoped_payload(
                workspace,
                social_account,
                capabilities=["read_calendar", "schedule_posts"],
                requires_approval="",
                daily_request_budget="",
                allowed_ip_ranges="",
                expires_at="",
            ),
        )
        assert r.status_code == 302
        key = ApiKey.objects.get(pk=issued.api_key.pk)
        assert key.capabilities == ["read_calendar", "schedule_posts"]
        assert key.requires_approval is False and key.daily_request_budget is None and key.allowed_ip_ranges == []
        assert {"create_posts", "publish_directly"} <= set(key.permissions)

    def test_edit_without_a_capability_is_refused(self, admin_client, admin_user, workspace, social_account):
        issued = self._key(workspace, admin_user, social_account)
        r = admin_client.post(
            reverse("api_keys:edit", args=[issued.api_key.id]),
            _scoped_payload(workspace, social_account, capabilities=[]),
            follow=True,
        )
        assert "Choose at least one capability" in r.content.decode()
        assert ApiKey.objects.get(pk=issued.api_key.pk).capabilities == ["read_calendar"]

    def test_a_member_cannot_edit_rotate_or_revoke(
        self, member_client, admin_user, admin_memberships, workspace, social_account
    ):
        issued = self._key(workspace, admin_user, social_account)
        for name in ("edit", "rotate", "revoke"):
            r = member_client.post(reverse(f"api_keys:{name}", args=[issued.api_key.id]), {})
            assert r.status_code == 403, name
        key = ApiKey.objects.get(pk=issued.api_key.pk)
        assert key.revoked_at is None and key.capabilities == ["read_calendar"]

    def test_rotate_shows_the_new_token_once_and_kills_the_old_one_at_once(
        self, admin_client, admin_user, workspace, social_account
    ):
        from apps.api.tests.world import bearer

        issued = self._key(workspace, admin_user, social_account)
        old = bearer(issued.plaintext_token)
        assert old.get("/api/v1/me/").status_code == 200

        r = admin_client.post(reverse("api_keys:rotate", args=[issued.api_key.id]), follow=True)
        body = r.content.decode()
        assert "Save this token now." in body and "managed" in body
        new_token = re.search(r"bb_studio_[\w-]+", body).group(0)
        assert new_token != issued.plaintext_token
        assert old.get("/api/v1/me/").status_code == 401
        assert bearer(new_token).get("/api/v1/me/").status_code == 200
        again = admin_client.get(reverse("api_keys:list")).content.decode()
        assert "Save this token now." not in again and new_token not in again

    def test_revoke_takes_effect_on_the_next_request(self, admin_client, admin_user, workspace, social_account):
        from apps.api.tests.world import bearer

        issued = self._key(workspace, admin_user, social_account)
        client = bearer(issued.plaintext_token)
        assert client.get("/api/v1/me/").status_code == 200
        admin_client.post(reverse("api_keys:revoke", args=[issued.api_key.id]))
        assert client.get("/api/v1/me/").status_code == 401

    def test_rotate_404s_for_a_key_in_another_org(self, admin_client, db):
        from apps.api.tests.world import build_world
        from apps.api_keys import services

        world = build_world()
        issued = services.issue_api_key(
            workspace=world.other_workspace,
            social_accounts=[world.other_account],
            issued_by=world.other_owner,
            name="foreign",
            permissions=[],
        )
        assert admin_client.post(reverse("api_keys:rotate", args=[issued.api_key.id])).status_code == 404

    def test_a_revoked_key_cannot_be_rotated(self, admin_client, admin_user, workspace, social_account):
        from apps.api_keys import services

        issued = self._key(workspace, admin_user, social_account)
        services.revoke_api_key(issued.api_key)
        r = admin_client.post(reverse("api_keys:rotate", args=[issued.api_key.id]), follow=True)
        assert "Only an active key can be rotated" in r.content.decode()


@pytest.mark.django_db
class TestConnectedApps:
    """OAuth apps show beside the tokens: admins see and limit every app in their org, members manage their own."""

    def _connect(self, user, organization, name="Claude Desktop", **fields):
        from oauth2_provider.models import get_application_model

        from apps.api_keys.models import ConnectedApp

        model = get_application_model()
        application = model.objects.create(
            name=name,
            client_type=model.CLIENT_PUBLIC,
            authorization_grant_type=model.GRANT_AUTHORIZATION_CODE,
            redirect_uris="https://claude.ai/api/mcp/auth_callback",
        )
        return ConnectedApp.objects.create(
            user=user,
            application=application,
            organization=organization,
            capabilities=["read_calendar", "draft_posts"],
            **fields,
        )

    @pytest.fixture
    def org_member(self, db, organization):
        """A plain member of the admin's organization (no manage_api_keys)."""
        from django.test import Client

        from apps.accounts.models import User

        user = User.objects.create_user(
            email="plain-member@example.com", password=None, name="Plain", tos_accepted_at=timezone.now()
        )
        OrgMembership.objects.filter(user=user).delete()
        OrgMembership.objects.create(user=user, organization=organization, org_role=OrgMembership.OrgRole.MEMBER)
        client = Client()
        client.force_login(user)
        return user, client

    def test_lists_an_app_in_the_org_with_its_limits(self, admin_client, admin_user, organization):
        from apps.api_keys.budgets import reserve_ai_call

        connection = self._connect(admin_user, organization, daily_ai_budget=4)
        reserve_ai_call(connection)
        body = admin_client.get(reverse("api_keys:list")).content.decode()
        assert "Claude Desktop" in body and "Connected apps" in body
        assert "Draft posts" in body and "1 of 4" in body
        assert reverse("api_keys:revoke_connection", args=[connection.id]) in body

    def test_an_app_connected_to_another_org_is_not_listed(self, admin_client, admin_user, member_user):
        other_org = member_user.org_memberships.first().organization
        self._connect(admin_user, other_org, name="Connected elsewhere")
        assert "Connected elsewhere" not in admin_client.get(reverse("api_keys:list")).content.decode()

    def test_another_orgs_app_is_a_404_even_for_the_same_person(self, admin_client, admin_user, member_user):
        other_org = member_user.org_memberships.first().organization
        connection = self._connect(admin_user, other_org)
        for name in ("revoke_connection", "edit_connection", "reinstate_connection"):
            assert admin_client.post(reverse(f"api_keys:{name}", args=[connection.id])).status_code == 404, name
        connection.refresh_from_db()
        assert connection.revoked_at is None

    def test_an_admin_edit_sets_a_ceiling_budgets_expiry_and_approval_mode(
        self, admin_client, admin_user, organization
    ):
        connection = self._connect(admin_user, organization)
        response = admin_client.post(
            reverse("api_keys:edit_connection", args=[connection.id]),
            {
                "scoped": "1",
                "capabilities": ["read_calendar"],
                "daily_request_budget": "50",
                "daily_ai_budget": "",
                "daily_post_budget": "0",
                "requires_approval": "on",
                "expires_at": "2031-01-31",
            },
        )
        assert response.status_code == 302
        connection.refresh_from_db()
        assert connection.admin_capabilities == ["read_calendar"]
        assert connection.capabilities == ["read_calendar", "draft_posts"]  # the person's own choice is kept
        assert connection.effective_capabilities == ["read_calendar"]
        assert (connection.daily_request_budget, connection.daily_ai_budget, connection.daily_post_budget) == (
            50,
            None,
            0,
        )
        assert connection.admin_requires_approval is True
        assert connection.expires_at.date().isoformat() == "2031-01-31"

    def test_an_admin_disconnect_revokes_the_tokens_and_sticks(self, admin_client, admin_user, organization):
        from oauth2_provider.models import get_access_token_model

        connection = self._connect(admin_user, organization)
        get_access_token_model().objects.create(
            user=admin_user,
            application=connection.application,
            token="abc",
            expires=timezone.now() + timedelta(hours=1),
            scope=f"mcp org:{organization.id}",
        )
        assert admin_client.post(reverse("api_keys:revoke_connection", args=[connection.id])).status_code == 302
        connection.refresh_from_db()
        assert connection.revoked_at is not None and connection.admin_revoked_at is not None
        assert not get_access_token_model().objects.filter(application=connection.application).exists()
        body = admin_client.get(reverse("api_keys:list")).content.decode()
        assert "Disconnected by an admin" in body and "Allow again" in body
        assert admin_client.post(reverse("api_keys:reinstate_connection", args=[connection.id])).status_code == 302
        connection.refresh_from_db()
        assert connection.admin_revoked_at is None

    def test_a_member_sees_and_disconnects_their_own_app(self, org_member, organization):
        user, client = org_member
        connection = self._connect(user, organization, name="My laptop Claude")
        page = client.get(reverse("api_keys:my_connections"))
        assert page.status_code == 200 and "My laptop Claude" in page.content.decode()
        assert client.post(reverse("api_keys:revoke_connection", args=[connection.id])).status_code == 302
        connection.refresh_from_db()
        assert connection.revoked_at is not None
        assert connection.admin_revoked_at is None  # their own disconnect: approving again undoes it

    def test_a_member_can_only_narrow_their_own_app(self, org_member, organization):
        user, client = org_member
        connection = self._connect(user, organization)
        client.post(
            reverse("api_keys:edit_connection", args=[connection.id]),
            {"scoped": "1", "capabilities": ["read_calendar", "schedule_posts"], "daily_post_budget": "999"},
        )
        connection.refresh_from_db()
        assert connection.capabilities == ["read_calendar"]
        assert connection.daily_post_budget is None and connection.admin_capabilities is None

    def test_a_member_cannot_see_or_touch_someone_elses_app(self, org_member, admin_user, organization):
        user, client = org_member
        connection = self._connect(admin_user, organization, name="The admin's app")
        assert "The admin's app" not in client.get(reverse("api_keys:my_connections")).content.decode()
        assert client.post(reverse("api_keys:revoke_connection", args=[connection.id])).status_code == 404
        assert client.post(reverse("api_keys:reinstate_connection", args=[connection.id])).status_code == 403
        connection.refresh_from_db()
        assert connection.revoked_at is None
