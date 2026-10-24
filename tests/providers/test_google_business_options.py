"""Google Business local post options: call-to-action button, events and offers, with HTTP mocked."""

from unittest.mock import MagicMock

import pytest

from providers import post_settings_error_for, post_settings_for
from providers.exceptions import PublishError
from providers.google_business import GoogleBusinessProvider
from providers.types import PostType, PublishContent

EVENT = {
    "topic_type": "EVENT",
    "event_title": "Open day",
    "event_start_date": "2026-11-01",
    "event_start_time": "09:30",
    "event_end_date": "2026-11-01",
    "event_end_time": "17:00",
}


def _provider():
    provider = GoogleBusinessProvider({"client_id": "id", "client_secret": "secret"})
    provider._get_account_id = MagicMock(return_value="accounts/1")
    provider._get_location_id = MagicMock(return_value="accounts/1/locations/2")
    provider._request = MagicMock(return_value=MagicMock(json=MagicMock(return_value={"name": "p1"})))
    return provider


def _body(provider):
    return provider._request.call_args.kwargs["json"]


def test_declares_button_event_and_offer_fields():
    keys = [s.key for s in post_settings_for("google_business")]
    assert keys[:3] == ["topic_type", "cta_type", "cta_url"]
    assert {"event_title", "offer_coupon_code", "offer_redeem_url", "offer_terms"} <= set(keys)


def test_standard_post_with_button():
    provider = _provider()
    provider.publish_post(
        "t", PublishContent(text="Hi", extra={"cta_type": "BOOK", "cta_url": " https://example.com/book "})
    )
    body = _body(provider)
    assert body["topicType"] == "STANDARD"
    assert body["callToAction"] == {"actionType": "BOOK", "url": "https://example.com/book"}


def test_call_button_sends_no_url():
    provider = _provider()
    provider.publish_post("t", PublishContent(text="Hi", extra={"cta_type": "CALL"}))
    assert _body(provider)["callToAction"] == {"actionType": "CALL"}


def test_event_carries_the_full_schedule_and_button():
    provider = _provider()
    provider.publish_post(
        "t", PublishContent(text="Come", extra={**EVENT, "cta_type": "SIGN_UP", "cta_url": "https://e.com/s"})
    )
    body = _body(provider)
    assert body["event"] == {
        "title": "Open day",
        "schedule": {
            "startDate": {"year": 2026, "month": 11, "day": 1},
            "startTime": {"hours": 9, "minutes": 30, "seconds": 0, "nanos": 0},
            "endDate": {"year": 2026, "month": 11, "day": 1},
            "endTime": {"hours": 17, "minutes": 0, "seconds": 0, "nanos": 0},
        },
    }
    assert body["callToAction"]["actionType"] == "SIGN_UP"


def test_offer_sends_coupon_details_and_no_button():
    provider = _provider()
    provider.publish_post(
        "t",
        PublishContent(
            text="Deal",
            post_type=PostType.TEXT,
            extra={
                **EVENT,
                "topic_type": "OFFER",
                "offer_coupon_code": "SAVE10",
                "offer_redeem_url": "https://e.com/redeem",
                "offer_terms": "One per customer",
            },
        ),
    )
    body = _body(provider)
    assert body["topicType"] == "OFFER"
    assert body["offer"] == {
        "couponCode": "SAVE10",
        "redeemOnlineUrl": "https://e.com/redeem",
        "termsConditions": "One per customer",
    }
    assert "callToAction" not in body


@pytest.mark.parametrize(
    "settings, fragment",
    [
        ({"cta_type": "BOOK"}, "needs a link"),
        ({"cta_url": "https://e.com"}, "Choose a button"),
        ({"cta_type": "CALL", "cta_url": "https://e.com"}, "leave its link empty"),
        ({"cta_type": "BOOK", "cta_url": "ftp://e.com"}, "http"),
        ({**EVENT, "cta_type": "BOOK", "cta_url": "https://e.com", "topic_type": "OFFER"}, "ignores the button"),
        ({"topic_type": "EVENT"}, "needs a title"),
        ({"topic_type": "EVENT", "event_title": "x"}, "start and end"),
        ({**EVENT, "event_end_time": "08:00"}, "before the start"),
        ({**EVENT, "event_start_date": "tomorrow"}, "start and end"),
        ({**EVENT, "topic_type": "OFFER", "offer_redeem_url": "nope"}, "redeem link"),
    ],
)
def test_validation(settings, fragment):
    assert fragment in post_settings_error_for("google_business", settings)


def test_publish_refuses_invalid_settings_without_calling_google():
    provider = _provider()
    with pytest.raises(PublishError) as exc:
        provider.publish_post("t", PublishContent(text="x", extra={"topic_type": "EVENT"}))
    assert exc.value.retryable is False
    provider._request.assert_not_called()


def test_valid_event_passes():
    assert post_settings_error_for("google_business", EVENT) == ""
