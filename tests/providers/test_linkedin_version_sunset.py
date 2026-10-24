"""LinkedIn retires an API version a year after releasing it; every call then fails with 426.

The version is the LINKEDIN_API_VERSION setting. A 426 fails the request permanently with a clear
sentence and alerts staff, instead of looking like a generic LinkedIn outage.
"""

import httpx
import pytest

from apps.notifications.models import EventType, Notification
from apps.notifications.operators import alert_staff
from providers import linkedin
from providers.exceptions import APIError, ApiVersionSunsetError, RateLimitError
from providers.linkedin_personal import LinkedInPersonalProvider

SUNSET_BODY = {"status": 426, "code": "NONEXISTENT_VERSION", "message": "Requested version 20240101 is not active"}


def _provider():
    return LinkedInPersonalProvider({"client_id": "id", "client_secret": "secret"})


@pytest.fixture
def reported(monkeypatch):
    calls = []
    monkeypatch.setattr(linkedin, "report_version_sunset", lambda *args: calls.append(args))
    return calls


def test_a_426_is_a_sunset_not_a_generic_error(reported):
    error = _provider()._error_for_response(httpx.Response(426, json=SUNSET_BODY))
    assert isinstance(error, ApiVersionSunsetError)
    assert isinstance(error, APIError)
    assert error.status_code == 426
    assert error.retryable is False
    assert "retired" in str(error)
    assert reported == [("LinkedIn", linkedin.LINKEDIN_HEADERS["LinkedIn-Version"], "LINKEDIN_API_VERSION")]


def test_the_error_code_alone_is_enough(reported):
    error = _provider()._error_for_response(httpx.Response(400, json={"code": "NONEXISTENT_VERSION"}))
    assert isinstance(error, ApiVersionSunsetError)


def test_other_errors_are_untouched(reported):
    provider = _provider()
    assert type(provider._error_for_response(httpx.Response(500, json={"message": "boom"}))) is APIError
    assert isinstance(provider._error_for_response(httpx.Response(429, json={})), RateLimitError)
    assert reported == []


def test_the_version_is_read_from_the_setting(monkeypatch):
    monkeypatch.setenv("LINKEDIN_API_VERSION", "202701")
    assert linkedin._configured_api_version() == "202701"
    monkeypatch.delenv("LINKEDIN_API_VERSION")
    assert linkedin._configured_api_version() == linkedin.DEFAULT_LINKEDIN_API_VERSION


@pytest.mark.parametrize("bad", ["2026-04", "26", "202613", "latest"])
def test_a_malformed_version_stops_the_app_from_starting_with_it(monkeypatch, bad):
    monkeypatch.setenv("LINKEDIN_API_VERSION", bad)
    with pytest.raises(ValueError, match="LINKEDIN_API_VERSION"):
        linkedin._configured_api_version()


@pytest.mark.django_db
def test_staff_are_notified_once_per_cooldown(django_user_model):
    staff = django_user_model.objects.create_user(email="ops@example.com", password="x", is_staff=True)
    django_user_model.objects.create_user(email="customer@example.com", password="x")

    assert alert_staff(key="api-version-sunset:LinkedIn:202604", title="LinkedIn retired", body="Update it") == 1
    assert alert_staff(key="api-version-sunset:LinkedIn:202604", title="LinkedIn retired", body="Update it") == 0
    assert alert_staff(key="api-version-sunset:LinkedIn:202701", title="Another", body="Update it") == 1

    assert Notification.objects.filter(user=staff, event_type=EventType.PLATFORM_API_ALERT).count() == 2
    assert Notification.objects.count() == 2
