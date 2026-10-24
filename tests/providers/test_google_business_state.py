"""Google answers a local post create with 200 even when the post breaks its content policy.

The post then sits in ``state: REJECTED`` with nothing live. A post still PROCESSING is read again
briefly, then accepted: Google finishes it by itself and failing would make the customer post twice.
"""

from unittest.mock import MagicMock

import pytest

from providers import google_business
from providers.exceptions import PublishError
from providers.google_business import GoogleBusinessProvider
from providers.types import PostType, PublishContent

NAME = "accounts/1/locations/2/localPosts/3"


def _reply(body):
    return MagicMock(json=MagicMock(return_value=body))


def _provider(*replies):
    provider = GoogleBusinessProvider({"client_id": "id", "client_secret": "secret"})
    provider._get_account_id = MagicMock(return_value="accounts/1")
    provider._get_location_id = MagicMock(return_value="accounts/1/locations/2")
    provider._request = MagicMock(side_effect=[_reply(body) for body in replies])
    return provider


def _publish(provider):
    return provider.publish_post("token", PublishContent(text="Open day", post_type=PostType.TEXT))


def test_a_live_post_is_returned_without_another_request():
    provider = _provider({"name": NAME, "state": "LIVE", "searchUrl": "https://g.co/p"})
    result = _publish(provider)
    assert result.platform_post_id == NAME
    assert result.url == "https://g.co/p"
    assert provider._request.call_count == 1


def test_a_rejected_post_fails_permanently_with_the_reason():
    provider = _provider({"name": NAME, "state": "REJECTED"})
    with pytest.raises(PublishError) as raised:
        _publish(provider)
    assert raised.value.retryable is False
    assert "content policy" in str(raised.value)
    assert "not live" in str(raised.value)


def test_a_processing_post_is_read_again_until_it_settles():
    provider = _provider(
        {"name": NAME, "state": "PROCESSING"},
        {"name": NAME, "state": "PROCESSING"},
        {"name": NAME, "state": "LIVE", "searchUrl": "https://g.co/p"},
    )
    result = _publish(provider)
    assert result.url == "https://g.co/p"
    assert provider._request.call_args.args[:2] == ("GET", f"{google_business.POSTS_API}/{NAME}")


def test_a_post_rejected_while_processing_still_fails():
    provider = _provider({"name": NAME, "state": "PROCESSING"}, {"name": NAME, "state": "REJECTED"})
    with pytest.raises(PublishError, match="rejected"):
        _publish(provider)


def test_a_post_still_processing_after_the_wait_counts_as_published(monkeypatch):
    monkeypatch.setattr(google_business, "STATE_POLL_SECONDS", 0.05)
    monkeypatch.setattr(google_business, "STATE_POLL_INTERVAL", 0.01)
    provider = GoogleBusinessProvider({"client_id": "id", "client_secret": "secret"})
    provider._get_account_id = MagicMock(return_value="accounts/1")
    provider._get_location_id = MagicMock(return_value="accounts/1/locations/2")
    provider._request = MagicMock(return_value=_reply({"name": NAME, "state": "PROCESSING"}))
    result = _publish(provider)
    assert result.platform_post_id == NAME


def test_text_over_1500_is_refused_not_cut():
    provider = _provider({"name": NAME, "state": "LIVE"})
    with pytest.raises(PublishError, match="1500"):
        provider.publish_post("token", PublishContent(text="a" * 1501, post_type=PostType.TEXT))
    provider._request.assert_not_called()
