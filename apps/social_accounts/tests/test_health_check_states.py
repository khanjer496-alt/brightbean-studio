"""What the health check concludes about an account from a failed probe.

An account is only condemned (ERROR) when the platform turns its credentials
down. A 5xx, a timeout or a rate limit says nothing about the grant; marking the
account ERROR for those dropped it from the sweep and from scheduling until
someone reconnected it by hand.
"""

from datetime import timedelta
from unittest.mock import MagicMock, patch

import httpx
import pytest
from django.utils import timezone

from apps.accounts.models import User
from apps.members.models import WorkspaceMembership
from apps.notifications.models import EventType, Notification
from apps.social_accounts.error_messages import (
    UNREACHABLE_MESSAGE,
    is_account_failure,
    is_transient_failure,
)
from apps.social_accounts.models import SocialAccount
from apps.social_accounts.tasks import (
    UNREACHABLE_WARNING_AFTER,
    check_social_account_health,
    schedule_all_health_checks,
)
from providers.exceptions import (
    APIError,
    AuthRejectedError,
    OAuthError,
    QuotaExceededError,
    RateLimitError,
    TokenExpiredError,
)
from providers.types import AccountProfile, OAuthTokens

Status = SocialAccount.ConnectionStatus

pytestmark = pytest.mark.django_db


@pytest.fixture
def workspace(organization):
    from apps.workspaces.models import Workspace

    return Workspace.objects.create(name="WS", organization=organization)


@pytest.fixture
def account(workspace):
    return SocialAccount.objects.create(
        workspace=workspace,
        platform="facebook",
        account_platform_id="123",
        account_name="Page",
        oauth_access_token="tok",
        oauth_refresh_token="refresh",
        connection_status=Status.CONNECTED,
    )


def check(account, *, profile=None, error=None):
    provider = MagicMock()
    if error is not None:
        provider.get_profile.side_effect = error
    else:
        provider.get_profile.return_value = profile or AccountProfile(platform_id="123", name="Page")
    with patch("providers.get_provider", return_value=provider):
        check_social_account_health.now(str(account.id))
    account.refresh_from_db()
    return provider


TRANSIENT = [
    pytest.param(APIError("503", status_code=503, platform="Facebook"), id="5xx"),
    pytest.param(APIError("no response", status_code=None, platform="Facebook"), id="no-response"),
    pytest.param(RateLimitError("429", retry_after=60), id="429"),
    pytest.param(APIError("blip", status_code=400, transient=True), id="meta-transient"),
    pytest.param(httpx.ConnectTimeout("slow"), id="raw-timeout"),
]

CONDEMNING = [
    pytest.param(TokenExpiredError("expired", status_code=401), id="token-expired"),
    pytest.param(AuthRejectedError("graph 190", status_code=400), id="meta-190"),
    pytest.param(APIError("unauthorized", status_code=401), id="401"),
    pytest.param(APIError("forbidden", status_code=403), id="403"),
    pytest.param(APIError("gone", status_code=404), id="404"),
    pytest.param(OAuthError("invalid_grant"), id="oauth-error"),
]


@pytest.mark.parametrize("error", TRANSIENT)
def test_a_transient_failure_leaves_the_status_alone(account, error):
    check(account, error=error)

    assert account.connection_status == Status.CONNECTED
    assert account.last_error == ""
    assert account.last_health_check_at is not None
    assert account.health_failing_since is not None


@pytest.mark.parametrize("error", CONDEMNING)
def test_a_rejected_credential_marks_the_account_error(account, error):
    check(account, error=error)

    assert account.connection_status == Status.ERROR
    assert account.last_error
    assert account.health_failing_since is None


def test_an_error_of_our_own_is_not_a_verdict_on_the_account(account):
    check(account, error=KeyError("id"))

    assert account.connection_status == Status.CONNECTED


def test_a_token_expiring_account_stays_token_expiring_through_an_outage(account):
    account.connection_status = Status.TOKEN_EXPIRING
    account.last_error = "Account connection expired. Please reconnect."
    account.save()

    check(account, error=APIError("503", status_code=503, platform="Facebook"))

    assert account.connection_status == Status.TOKEN_EXPIRING
    assert account.last_error == "Account connection expired. Please reconnect."


def test_the_next_good_check_clears_the_failure_run(account):
    check(account, error=APIError("503", status_code=503, platform="Facebook"))
    assert account.health_failing_since is not None

    check(account)

    assert account.health_failing_since is None
    assert account.connection_status == Status.CONNECTED


def test_a_long_outage_shows_a_warning_but_never_an_error(account):
    account.health_failing_since = timezone.now() - UNREACHABLE_WARNING_AFTER - timedelta(hours=1)
    account.save()

    check(account, error=APIError("503", status_code=503, platform="Facebook"))

    assert account.connection_status == Status.CONNECTED
    assert account.last_error == UNREACHABLE_MESSAGE


def test_a_short_outage_shows_nothing(account):
    account.health_failing_since = timezone.now() - timedelta(hours=6)
    account.save()

    check(account, error=APIError("503", status_code=503, platform="Facebook"))

    assert account.last_error == ""


def test_an_account_flagged_error_recovers_when_the_platform_accepts_it_again(account):
    account.connection_status = Status.ERROR
    account.last_error = "Account connection expired. Please reconnect."
    account.save()

    check(account)

    assert account.connection_status == Status.CONNECTED
    assert account.last_error == ""


def test_the_sweep_includes_flagged_accounts(workspace, account):
    flagged = SocialAccount.objects.create(
        workspace=workspace,
        platform="facebook",
        account_platform_id="456",
        account_name="Flagged",
        connection_status=Status.ERROR,
    )
    gone = SocialAccount.objects.create(
        workspace=workspace,
        platform="facebook",
        account_platform_id="789",
        account_name="Gone",
        connection_status=Status.DISCONNECTED,
    )
    queued = []
    with patch("apps.social_accounts.tasks.check_social_account_health", side_effect=queued.append):
        schedule_all_health_checks.now()

    assert set(queued) == {str(account.id), str(flagged.id)}
    assert str(gone.id) not in queued


def test_a_refresh_that_failed_because_the_platform_was_down_does_not_demote_the_account(account):
    account.token_expires_at = timezone.now() + timedelta(days=1)
    account.save()
    provider = MagicMock()
    provider.get_profile.return_value = AccountProfile(platform_id="123", name="Page")
    provider.refresh_token.side_effect = APIError("503", status_code=503, platform="Facebook")

    with patch("providers.get_provider", return_value=provider):
        check_social_account_health.now(str(account.id))
    account.refresh_from_db()

    assert account.connection_status == Status.CONNECTED


def test_a_refresh_the_platform_refused_marks_the_token_expiring(account):
    account.token_expires_at = timezone.now() + timedelta(days=1)
    account.save()
    provider = MagicMock()
    provider.get_profile.return_value = AccountProfile(platform_id="123", name="Page")
    provider.refresh_token.side_effect = OAuthError("invalid_grant")

    with patch("providers.get_provider", return_value=provider):
        check_social_account_health.now(str(account.id))
    account.refresh_from_db()

    assert account.connection_status == Status.TOKEN_EXPIRING


def test_a_spent_quota_is_still_not_a_failure(account):
    check(account, error=QuotaExceededError("daily quota spent", status_code=403))

    assert account.connection_status == Status.CONNECTED


def test_the_classification_helpers_agree_with_the_table():
    for param in TRANSIENT[:4]:
        assert is_transient_failure(param.values[0]) and not is_account_failure(param.values[0])
    for param in CONDEMNING:
        assert is_account_failure(param.values[0]) and not is_transient_failure(param.values[0])


# ----------------------------------------------------------------------
# Refresh-token expiry
# ----------------------------------------------------------------------


@pytest.fixture
def manager(workspace):
    user = User.objects.create_user(email="m@example.com", password="x", name="M", tos_accepted_at=timezone.now())
    WorkspaceMembership.objects.create(
        user=user, workspace=workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
    )
    return user


def test_an_account_whose_refresh_token_ends_soon_is_warned_about_once(account, manager):
    account.refresh_token_expires_at = timezone.now() + timedelta(days=5)
    account.save()

    check(account)
    check(account)

    assert account.connection_status == Status.TOKEN_EXPIRING
    assert "Reconnect before" in account.last_error
    notifications = Notification.objects.filter(event_type=EventType.SOCIAL_ACCOUNT_DISCONNECTED)
    assert notifications.count() == 1
    assert notifications.get().user == manager


def test_a_refresh_token_with_plenty_of_time_raises_no_warning(account, manager):
    account.refresh_token_expires_at = timezone.now() + timedelta(days=200)
    account.save()

    check(account)

    assert account.connection_status == Status.CONNECTED
    assert Notification.objects.count() == 0


def test_an_account_with_no_known_refresh_expiry_is_unaffected(account):
    check(account)

    assert account.connection_status == Status.CONNECTED


def test_a_refresh_stores_the_new_refresh_token_expiry(account):
    account.token_expires_at = timezone.now() + timedelta(hours=1)
    account.save()
    provider = MagicMock()
    provider.refresh_token.return_value = OAuthTokens(
        access_token="new", refresh_token="new-refresh", expires_in=3600, refresh_expires_in=86400 * 60
    )

    account.refresh_oauth_token(provider, enqueue_backfill=False)

    account.refresh_from_db()
    assert account.refresh_token_expires_at > timezone.now() + timedelta(days=59)


def test_a_refresh_without_a_refresh_expiry_keeps_the_recorded_one(account):
    recorded = timezone.now() + timedelta(days=30)
    account.refresh_token_expires_at = recorded
    account.token_expires_at = timezone.now() + timedelta(hours=1)
    account.save()
    provider = MagicMock()
    provider.refresh_token.return_value = OAuthTokens(access_token="new", expires_in=3600)

    account.refresh_oauth_token(provider, enqueue_backfill=False)

    account.refresh_from_db()
    assert account.refresh_token_expires_at == recorded
