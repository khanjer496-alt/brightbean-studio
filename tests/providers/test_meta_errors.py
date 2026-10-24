"""Meta Graph error taxonomy: what each code means for the stack above the provider."""

import json
from unittest.mock import patch

import httpx
import pytest

from apps.social_accounts.error_messages import (
    PUBLISH_RATE_LIMIT_MESSAGE,
    PUBLISH_RECONNECT_MESSAGE,
    PUBLISH_TEMPORARY_MESSAGE,
    friendly_health_check_error,
    friendly_publish_error,
    is_credential_rejection,
    is_transient_failure,
)
from providers.exceptions import APIError, AuthRejectedError, RateLimitError, TokenExpiredError
from providers.facebook import FacebookProvider
from providers.instagram import InstagramProvider
from providers.instagram_login import InstagramLoginProvider
from providers.meta_errors import container_status_error, is_read_after_write_quirk, parse_graph_error
from providers.threads import ThreadsProvider

CREDS = {"client_id": "id", "client_secret": "secret"}
PROVIDERS = [FacebookProvider, InstagramProvider, InstagramLoginProvider, ThreadsProvider]


def graph(code=None, subcode=None, *, type_="OAuthException", transient=None, message="boom"):
    error = {"message": message, "type": type_, "fbtrace_id": "trace-1"}
    if code is not None:
        error["code"] = code
    if subcode is not None:
        error["error_subcode"] = subcode
    if transient is not None:
        error["is_transient"] = transient
    return {"error": error}


def fail(provider_cls, body, *, status=400, headers=None, method="POST"):
    """The exception ``provider_cls`` raises when Graph answers ``status`` with ``body``."""
    provider = provider_cls(CREDS)

    def fake(self, method_, url, **kwargs):
        return httpx.Response(
            status,
            request=httpx.Request(method_, url),
            content=body if isinstance(body, bytes) else json.dumps(body).encode(),
            headers=headers,
        )

    with patch.object(httpx.Client, "request", fake), pytest.raises((APIError, RateLimitError, TokenExpiredError)) as c:
        provider._request(method, "https://graph.example/x")
    return c.value


@pytest.mark.parametrize("provider_cls", PROVIDERS)
@pytest.mark.parametrize(
    ("code", "subcode"),
    [(190, None), (190, 463), (190, 467), (102, None), (459, None), (460, None), (463, None), (467, None), (490, None)],
)
def test_dead_token_codes_are_reconnect_required_on_every_meta_provider(provider_cls, code, subcode):
    exc = fail(provider_cls, graph(code, subcode))

    assert isinstance(exc, AuthRejectedError)
    assert isinstance(exc, TokenExpiredError)
    assert isinstance(exc, APIError), "analytics and inbox still catch the HTTP failure as an APIError"
    assert exc.status_code == 400
    assert exc.raw_response["error"]["code"] == code
    assert is_credential_rejection(exc)
    assert friendly_publish_error(exc) == PUBLISH_RECONNECT_MESSAGE
    assert "reconnect" in friendly_health_check_error(exc).lower()


def test_a_dead_token_is_recognised_on_a_401_and_a_500_too():
    assert isinstance(fail(FacebookProvider, graph(190), status=401), AuthRejectedError)
    assert isinstance(fail(FacebookProvider, graph(190), status=500), AuthRejectedError)


@pytest.mark.parametrize("provider_cls", PROVIDERS)
@pytest.mark.parametrize("code", [4, 17, 32, 613, 368, *range(80001, 80015)])
def test_rate_limit_codes_are_rate_limits_with_a_backoff(provider_cls, code):
    exc = fail(provider_cls, graph(code))

    assert isinstance(exc, RateLimitError)
    assert exc.retry_after and exc.retry_after >= 60
    assert friendly_publish_error(exc) == PUBLISH_RATE_LIMIT_MESSAGE
    assert is_transient_failure(exc)


def test_a_posting_block_waits_longer_than_an_ordinary_throttle():
    assert fail(FacebookProvider, graph(368)).retry_after > fail(FacebookProvider, graph(4)).retry_after
    assert fail(FacebookProvider, graph(None, 1390008)).retry_after >= 3600


def test_a_retry_after_header_is_respected():
    assert fail(FacebookProvider, graph(4), headers={"Retry-After": "1200"}).retry_after == 1200


def test_the_business_use_case_usage_header_gives_the_wait():
    usage = {"page-1": [{"type": "pages", "call_count": 100, "estimated_time_to_regain_access": 30}]}
    exc = fail(FacebookProvider, graph(32), headers={"x-business-use-case-usage": json.dumps(usage)})
    assert exc.retry_after == 30 * 60


def test_the_wait_is_capped():
    exc = fail(FacebookProvider, graph(4), headers={"Retry-After": "9999999"})
    assert exc.retry_after == 24 * 3600


def test_an_unreadable_usage_header_falls_back_to_the_default():
    exc = fail(FacebookProvider, graph(4), headers={"x-business-use-case-usage": "{not json"})
    assert exc.retry_after == 15 * 60


@pytest.mark.parametrize("provider_cls", PROVIDERS)
@pytest.mark.parametrize("body", [graph(1), graph(2), graph(None, transient=True), graph(100, transient=True)])
def test_transient_errors_are_marked_transient_and_stay_retryable(provider_cls, body):
    exc = fail(provider_cls, body)

    assert type(exc) is APIError
    assert exc.transient is True
    assert exc.retryable is True
    assert friendly_publish_error(exc) == PUBLISH_TEMPORARY_MESSAGE
    assert is_transient_failure(exc)


def test_an_unrecognised_graph_error_is_the_plain_api_error():
    exc = fail(FacebookProvider, graph(100))

    assert type(exc) is APIError
    assert exc.status_code == 400
    assert exc.transient is False
    assert exc.retryable is True


def test_an_http_429_with_no_graph_body_is_still_a_rate_limit():
    exc = fail(FacebookProvider, b"slow down", status=429, headers={"Retry-After": "7"})
    assert isinstance(exc, RateLimitError)
    assert exc.retry_after == 7


def test_a_non_json_error_body_falls_back_to_the_base_error():
    exc = fail(FacebookProvider, b"<html>bad gateway</html>", status=502)
    assert type(exc) is APIError
    assert exc.status_code == 502


@pytest.mark.parametrize("body", [{"error": "invalid_grant"}, {"error": ["x"]}, ["error"], {"error": {"code": "abc"}}])
def test_error_bodies_that_are_not_graph_shaped_are_left_alone(body):
    exc = fail(FacebookProvider, body)
    assert type(exc) is APIError


def test_a_5xx_graph_error_on_a_write_still_carries_the_ambiguity_flag():
    exc = fail(FacebookProvider, graph(1), status=500)
    assert exc.transient and exc.ambiguous_write is True


# ----------------------------------------------------------------------
# Container errors
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("subcode", "needle"),
    [
        (2207005, "format"),
        (2207026, "format"),
        (36001, "format"),
        (2207009, "aspect ratio"),
        (36003, "aspect ratio"),
        (2207004, "too large"),
        (2207010, "caption"),
        (2207050, "restricted"),
        (2207003, "timed out"),
    ],
)
def test_permanent_container_errors_fail_at_once_with_a_clear_message(subcode, needle):
    exc = fail(InstagramProvider, graph(100, subcode))

    assert exc.retryable is False
    assert needle in friendly_publish_error(exc).lower()
    assert "{" not in friendly_publish_error(exc)


@pytest.mark.parametrize("subcode", [2207001, 2207006, 2207020, 2207027, 2207032, 2207052, 2207053])
def test_genuinely_transient_container_errors_are_retried(subcode):
    exc = fail(InstagramProvider, graph(100, subcode))

    assert exc.retryable is True
    assert exc.transient is True


def test_the_daily_publishing_cap_is_a_rate_limit():
    exc = fail(InstagramProvider, graph(9, 2207042))
    assert isinstance(exc, RateLimitError)
    assert exc.retry_after >= 3600


def test_a_container_status_error_names_the_code_in_meta_text():
    err = container_status_error("Error: Media upload has failed with error code 2207026", platform="Instagram")
    assert err.retryable is False
    assert "format" in str(err).lower()


def test_a_retryable_container_status_error_stays_retryable():
    err = container_status_error("ERROR 2207052 could not fetch", platform="Instagram")
    assert err.retryable is True


def test_an_unknown_container_status_error_keeps_the_default_handling():
    err = container_status_error("something odd", platform="Instagram", raw_response={"status": "something odd"})
    assert err.retryable is True
    assert "something odd" in str(err)


def test_the_read_after_write_quirk_is_recognised_only_for_100_33():
    assert is_read_after_write_quirk(APIError("x", raw_response=graph(100, 33)))
    assert not is_read_after_write_quirk(APIError("x", raw_response=graph(100, 2207026)))
    assert not is_read_after_write_quirk(APIError("x", raw_response={"error": "x"}))
    assert not is_read_after_write_quirk(RuntimeError("x"))


def test_parse_graph_error_reads_every_field():
    error = parse_graph_error(graph(190, 463, type_="OAuthException", transient=False, message="expired"))
    assert (error.code, error.subcode, error.type, error.message, error.trace_id) == (
        190,
        463,
        "OAuthException",
        "expired",
        "trace-1",
    )
    assert error.transient is False
    assert error.reconnect
