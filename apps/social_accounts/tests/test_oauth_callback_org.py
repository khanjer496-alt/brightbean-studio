"""The OAuth callback resolves app credentials from the signed state's workspace, never ``request.org``.

The callback URL names no workspace, so the middleware's ``request.org`` is just the user's first
organization membership. A user who belongs to two organizations must connect each workspace with
the app credentials of the organization (or workspace) that owns it, and nothing in the query
string, only the signed state, may choose that workspace.
"""

from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest
from django.core import signing
from django.urls import reverse

from apps.credentials.models import PlatformCredential
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.social_accounts.models import SocialAccount
from apps.social_accounts.oauth_aliases import to_url_slug
from apps.workspaces.models import Workspace
from providers import PROVIDER_REGISTRY

# The credential shape each platform's provider reads.
APP_KEYS = {
    "facebook": ("app_id", "app_secret"),
    "linkedin_personal": ("client_id", "client_secret"),
    "reddit": ("client_id", "client_secret"),
    "x": ("client_id", "client_secret"),
}
PLATFORMS = list(APP_KEYS)


def app(platform, name):
    key_id, key_secret = APP_KEYS[platform]
    return {key_id: f"{name}-id", key_secret: f"{name}-secret"}


@pytest.fixture(autouse=True)
def _no_env_credentials(settings):
    settings.PLATFORM_CREDENTIALS_FROM_ENV = {}


@pytest.fixture
def home_org(db, user):
    """The user's first organization: what the middleware sets as ``request.org`` on the callback."""
    org = Organization.objects.create(name="Home")
    OrgMembership.objects.create(user=user, organization=org, org_role="owner")
    return org


@pytest.fixture
def client_org(db, user, home_org):
    org = Organization.objects.create(name="Client")
    OrgMembership.objects.create(user=user, organization=org, org_role="member")
    return org


@pytest.fixture
def client_workspace(client_org, user):
    workspace = Workspace.objects.create(name="Client WS", organization=client_org)
    WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role="manager")
    return workspace


@pytest.fixture
def logged_in(client, user):
    client.force_login(user)
    return client


def configure(platform, home_org, client_org):
    PlatformCredential.objects.create(organization=home_org, platform=platform, credentials=app(platform, "home"))
    PlatformCredential.objects.create(organization=client_org, platform=platform, credentials=app(platform, "client"))


def start(client, workspace, platform):
    """Begin the connection from the workspace, as the connect page does; returns the signed state."""
    if PROVIDER_REGISTRY[platform].user_supplied_credentials:
        # The organization's app, kept as it is (a blank secret keeps the saved one).
        url = reverse("social_accounts:connect_x", kwargs={"workspace_id": workspace.id})
        response = client.post(url, {"client_id": app(platform, "client")[APP_KEYS[platform][0]], "client_secret": ""})
    else:
        url = reverse("social_accounts:connect", kwargs={"workspace_id": workspace.id})
        response = client.post(url, {"platform": platform})
    assert response.status_code == 302, response
    return parse_qs(urlparse(response.url).query)["state"][0]


def callback(client, platform, state):
    """Run the callback, recording the credentials the provider exchanged the code with."""
    seen = {}

    def exchange(self, code, redirect_uri, code_verifier=None):
        seen.update(self.credentials)
        raise RuntimeError("stop after the exchange")

    url = reverse("social_accounts:oauth_callback", kwargs={"platform": to_url_slug(platform)})
    with patch.object(PROVIDER_REGISTRY[platform], "exchange_code", autospec=True, side_effect=exchange):
        response = client.get(url, {"code": "auth-code", "state": state})
    return response, seen


@pytest.mark.django_db
@pytest.mark.parametrize("platform", PLATFORMS)
class TestCredentialsComeFromTheWorkspace:
    def test_the_authorize_url_and_the_exchange_use_the_workspaces_organization(
        self, logged_in, platform, home_org, client_org, client_workspace
    ):
        configure(platform, home_org, client_org)
        state = start(logged_in, client_workspace, platform)
        response, seen = callback(logged_in, platform, state)
        assert response.status_code == 302
        # Meta's provider reads ``app_id`` / ``app_secret`` under their client_* names too.
        assert (seen.get("client_id") or seen.get("app_id")) == "client-id"
        assert (seen.get("client_secret") or seen.get("app_secret")) == "client-secret"

    def test_a_state_for_a_workspace_the_user_left_is_refused(
        self, logged_in, platform, user, home_org, client_org, client_workspace
    ):
        configure(platform, home_org, client_org)
        state = start(logged_in, client_workspace, platform)
        WorkspaceMembership.objects.filter(user=user, workspace=client_workspace).delete()
        response, seen = callback(logged_in, platform, state)
        assert response.status_code == 403
        assert seen == {}

    def test_a_user_who_lost_the_connect_permission_is_refused(
        self, logged_in, platform, user, home_org, client_org, client_workspace
    ):
        configure(platform, home_org, client_org)
        state = start(logged_in, client_workspace, platform)
        WorkspaceMembership.objects.filter(user=user, workspace=client_workspace).update(workspace_role="viewer")
        response, seen = callback(logged_in, platform, state)
        assert response.status_code == 403
        assert seen == {}
        assert not SocialAccount.objects.exists()


@pytest.mark.django_db
class TestTheStateCannotBeRedirected:
    def test_a_rewritten_workspace_breaks_the_signature(self, logged_in, home_org, client_org, client_workspace):
        configure("reddit", home_org, client_org)
        state = start(logged_in, client_workspace, "reddit")
        other = Workspace.objects.create(name="Elsewhere", organization=home_org)
        payload = signing.loads(state, salt="social-oauth-state")
        forged = state.replace(
            signing.b64_encode(signing.JSONSerializer().dumps(payload)).decode(),
            signing.b64_encode(signing.JSONSerializer().dumps({**payload, "workspace_id": str(other.id)})).decode(),
        )
        response, seen = callback(logged_in, "reddit", forged)
        assert response.status_code == 302
        assert seen == {}

    def test_a_workspace_query_parameter_is_ignored(self, logged_in, home_org, client_org, client_workspace):
        configure("reddit", home_org, client_org)
        other = Workspace.objects.create(name="Elsewhere", organization=home_org)
        state = start(logged_in, client_workspace, "reddit")
        seen = {}

        def exchange(self, code, redirect_uri, code_verifier=None):
            seen.update(self.credentials)
            raise RuntimeError("stop")

        url = reverse("social_accounts:oauth_callback", kwargs={"platform": "reddit"})
        with patch.object(PROVIDER_REGISTRY["reddit"], "exchange_code", autospec=True, side_effect=exchange):
            response = logged_in.get(url, {"code": "c", "state": state, "workspace_id": str(other.id)})
        assert seen["client_id"] == "client-id"
        assert response.url == reverse("calendar:calendar", kwargs={"workspace_id": client_workspace.id})

    def test_another_users_state_is_refused(self, logged_in, client, home_org, client_org, client_workspace):
        from django.utils import timezone

        from apps.accounts.models import User

        configure("reddit", home_org, client_org)
        state = start(logged_in, client_workspace, "reddit")
        session_nonce = logged_in.session["social_oauth"]
        intruder = User.objects.create_user(
            email="intruder@example.com", password="pw-123456789", name="I", tos_accepted_at=timezone.now()
        )
        WorkspaceMembership.objects.create(user=intruder, workspace=client_workspace, workspace_role="manager")
        logged_in.force_login(intruder)
        session = logged_in.session
        session["social_oauth"] = session_nonce
        session.save()
        response, seen = callback(logged_in, "reddit", state)
        assert response.status_code == 403
        assert seen == {}
