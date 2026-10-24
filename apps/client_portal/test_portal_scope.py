"""A magic link starts a portal-scoped session; users with 2FA must pass the challenge first."""

import time

from allauth.mfa.models import Authenticator
from allauth.mfa.totp.internal.auth import format_hotp_value, generate_totp_secret, hotp_value
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.approvals.links import issue_links
from apps.client_portal.models import MagicLinkToken
from apps.composer.models import PlatformPost, Post
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace


class PortalScopeTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Org")
        self.workspace = Workspace.objects.create(
            organization=self.org, name="WS", approval_workflow_mode="required_internal_and_client"
        )
        self.user = User.objects.create_user(
            email="client@example.com", password="pw-12345", tos_accepted_at=timezone.now()
        )
        WorkspaceMembership.objects.filter(user=self.user).delete()
        OrgMembership.objects.filter(user=self.user).delete()
        # Also a member of the org, so the main app has something to show it.
        OrgMembership.objects.create(user=self.user, organization=self.org, org_role="member")
        WorkspaceMembership.objects.create(user=self.user, workspace=self.workspace, workspace_role="client")

    def enter(self, client=None, user=None):
        client = client or self.client
        token = MagicLinkToken.objects.create(user=user or self.user, workspace=self.workspace)
        return client.post(reverse("client_portal:magic_link_entry", kwargs={"token": token.token}))

    def add_totp(self):
        secret = generate_totp_secret()
        Authenticator.objects.create(user=self.user, type=Authenticator.Type.TOTP, data={"secret": secret})
        return secret

    def code(self, secret):
        return format_hotp_value(hotp_value(secret, int(time.time()) // 30))


class PortalScopedSessionTests(PortalScopeTests):
    def test_session_cannot_open_main_app_pages(self):
        self.enter()
        for url in (
            f"/workspace/{self.workspace.id}/calendar/",
            f"/workspace/{self.workspace.id}/",
            "/notifications/",
            "/settings/",
        ):
            response = self.client.get(url)
            self.assertRedirects(
                response, reverse("client_portal:dashboard"), fetch_redirect_response=False, msg_prefix=url
            )

    def test_session_cannot_write_to_main_app_or_use_the_api(self):
        self.enter()
        self.assertEqual(self.client.post(f"/workspace/{self.workspace.id}/calendar/").status_code, 403)
        self.assertEqual(self.client.get("/api/v1/mcp").status_code, 403)
        self.assertEqual(self.client.get("/oauth/authorize/").status_code, 403)

    def test_session_still_opens_the_portal(self):
        self.enter()
        self.assertEqual(self.client.get(reverse("client_portal:dashboard")).status_code, 200)

    def test_logout_clears_the_scope(self):
        self.enter()
        self.client.post("/accounts/logout/")
        self.assertNotIn("is_portal_session", self.client.session)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertRedirects(
            self.client.get(reverse("client_portal:dashboard")),
            reverse("client_portal:magic_link_expired"),
            fetch_redirect_response=False,
        )

    def test_ordinary_login_is_not_affected(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get("/notifications/").status_code, 200)

    def test_one_tap_link_works_without_leaving_the_portal_scope(self):
        from apps.approvals import decisions

        author = User.objects.create_user(
            email="author@example.com", password="pw-12345", tos_accepted_at=timezone.now()
        )
        account = SocialAccount.objects.create(
            workspace=self.workspace,
            platform="linkedin_personal",
            account_platform_id="li-1",
            account_name="Acme",
            connection_status="connected",
        )
        post = Post.objects.create(workspace=self.workspace, author=author, caption="Hello")
        PlatformPost.objects.create(post=post, social_account=account, status="pending_client")
        links = issue_links(
            post, self.user, self.workspace, decisions.current_fingerprint(post, self.user, self.workspace)
        )
        self.enter()
        url = links["approve"].split("://", 1)[1].split("/", 1)[1]
        response = self.client.post("/" + url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(post.platform_posts.values_list("status", flat=True)), {"approved"})
        self.assertTrue(self.client.session.get("is_portal_session"))


@override_settings(ACCOUNT_EMAIL_VERIFICATION="mandatory")
class PortalMfaTests(PortalScopeTests):
    def test_user_with_2fa_gets_the_challenge_not_a_session(self):
        self.add_totp()
        response = self.enter()
        self.assertRedirects(response, reverse("mfa_authenticate"), fetch_redirect_response=False)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertNotIn("is_portal_session", self.client.session)
        self.assertRedirects(
            self.client.get(reverse("client_portal:dashboard")),
            reverse("client_portal:magic_link_expired"),
            fetch_redirect_response=False,
        )

    def test_wrong_code_does_not_sign_in(self):
        self.add_totp()
        self.enter()
        self.client.post(reverse("mfa_authenticate"), {"code": "000000"})
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_correct_code_signs_in_and_lands_in_the_portal(self):
        secret = self.add_totp()
        self.enter()
        response = self.client.post(reverse("mfa_authenticate"), {"code": self.code(secret)})
        self.assertRedirects(response, reverse("client_portal:dashboard"), fetch_redirect_response=False)
        self.assertIn("_auth_user_id", self.client.session)
        self.assertEqual(self.client.get(reverse("client_portal:dashboard")).status_code, 200)
        # They passed 2FA, so this is an ordinary session.
        self.assertFalse(self.client.session.get("is_portal_session"))
        self.assertEqual(self.client.get("/notifications/").status_code, 200)

    def test_mfa_marker_does_not_open_the_portal_for_another_workspace(self):
        secret = self.add_totp()
        self.enter()
        self.client.post(reverse("mfa_authenticate"), {"code": self.code(secret)})
        other = Workspace.objects.create(organization=self.org, name="Other")
        session = self.client.session
        session["portal_mfa_workspace_id"] = str(other.id)
        session.save()
        self.assertRedirects(
            self.client.get(reverse("client_portal:dashboard")),
            reverse("client_portal:magic_link_expired"),
            fetch_redirect_response=False,
        )
