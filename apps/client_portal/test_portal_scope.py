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

    def test_account_takeover_routes_are_closed(self):
        """A forwarded magic link must not let anyone change the password or email, add 2FA or delete the account."""
        self.enter()
        for url in (
            reverse("account_change_password"),
            reverse("account_set_password"),
            reverse("account_email"),
            reverse("mfa_index"),
            reverse("accounts:settings"),
            reverse("accounts:delete_account"),
            reverse("account_login"),
            reverse("account_signup"),
            reverse("account_reset_password"),
            "/admin/",
            f"/workspace/{self.workspace.id}/media/",
            "/reports/share/x/",
        ):
            get = self.client.get(url)
            self.assertRedirects(get, reverse("client_portal:dashboard"), fetch_redirect_response=False, msg_prefix=url)
            self.assertEqual(self.client.post(url, {}).status_code, 403, url)
        self.assertEqual(
            self.client.post(
                reverse("account_change_password"),
                {"oldpassword": "pw-12345", "password1": "N3w-pass-word!", "password2": "N3w-pass-word!"},
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(reverse("account_email"), {"action_add": "", "email": "attacker@example.com"}).status_code,
            403,
        )
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("pw-12345"))
        self.assertFalse(self.user.emailaddress_set.filter(email="attacker@example.com").exists())

    def test_htmx_partials_are_not_rendered(self):
        self.enter()
        response = self.client.get(f"/workspace/{self.workspace.id}/calendar/", HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.content, b"")

    def test_magic_link_rotates_the_session_and_is_single_use(self):
        token = MagicLinkToken.objects.create(user=self.user, workspace=self.workspace)
        url = reverse("client_portal:magic_link_entry", kwargs={"token": token.token})
        self.client.get(url)
        before = self.client.session.session_key
        self.client.post(url)
        self.assertNotEqual(self.client.session.session_key, before)
        self.assertTrue(self.client.session["is_portal_session"])
        other = self.client_class()
        self.assertRedirects(
            other.post(url), reverse("client_portal:magic_link_expired"), fetch_redirect_response=False
        )
        self.assertNotIn("_auth_user_id", other.session)

    def test_magic_link_ends_whatever_session_the_browser_had(self):
        staff = User.objects.create_user(email="owner@example.com", password="pw-12345", tos_accepted_at=timezone.now())
        self.client.force_login(staff)
        session = self.client.session
        session["marker"] = "from the earlier session"
        session.save()
        self.enter()
        self.assertEqual(self.client.session["_auth_user_id"], str(self.user.pk))
        self.assertNotIn("marker", self.client.session)

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

    def test_2fa_challenge_does_not_run_on_top_of_a_portal_session(self):
        """A user who added 2FA after getting a portal session: the next link ends that session and challenges them."""
        self.enter()
        self.assertTrue(self.client.session.get("is_portal_session"))
        secret = self.add_totp()
        self.assertRedirects(self.enter(), reverse("mfa_authenticate"), fetch_redirect_response=False)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertNotIn("is_portal_session", self.client.session)
        before = self.client.session.session_key
        self.client.post(reverse("mfa_authenticate"), {"code": self.code(secret)})
        self.assertNotEqual(self.client.session.session_key, before)
        self.assertFalse(self.client.session.get("is_portal_session"))
        self.assertEqual(self.client.get("/notifications/").status_code, 200)
