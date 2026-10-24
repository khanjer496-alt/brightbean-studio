"""X provider (OAuth 2.0 + PKCE on the customer's own app, posts, threads, media, polls), HTTP mocked.

The HTTP layer is a real ``httpx.Client`` on a mock transport, so the provider's request building and its
mapping of X's error responses are exercised together.
"""

import base64
import hashlib
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from providers import PROVIDER_REGISTRY, caption_wire_length, get_provider, media_rules_for, post_settings_error_for
from providers.exceptions import (
    APIError,
    OAuthError,
    PublishError,
    QuotaExceededError,
    RateLimitError,
    TokenExpiredError,
)
from providers.types import AuthType, PostType, PublishContent, PublishResult, ThreadMode
from providers.x import (
    BUDGET_MESSAGE,
    CHUNK_BYTES,
    DUPLICATE_MESSAGE,
    XProvider,
    parse_poll_options,
    weighted_length,
)

CREDS = {"client_id": "app-id", "client_secret": "app-secret"}
API = "https://api.x.com"
REAL_CLIENT = httpx.Client


def provider() -> XProvider:
    return XProvider(dict(CREDS))


def json_response(body, status=200, headers=None):
    return httpx.Response(status, json=body, headers=headers)


class FakeX:
    """Routes ``(METHOD, path)`` to queued responses (the last one repeats) and records every request."""

    def __init__(self):
        self.routes: dict[tuple[str, str], list] = {}
        self.requests: list[httpx.Request] = []

    def on(self, method, path, *responses):
        self.routes[(method, path)] = list(responses)
        return self

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        queue = self.routes.get((request.method, request.url.path))
        if queue is None:
            raise AssertionError(f"unexpected request {request.method} {request.url}")
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item

    def calls(self, method, path):
        return [r for r in self.requests if r.method == method and r.url.path == path]


@pytest.fixture
def x():
    fake = FakeX()

    def client(**kwargs):
        return REAL_CLIENT(transport=httpx.MockTransport(fake.handle), timeout=kwargs.get("timeout"))

    with patch("providers.base.httpx.Client", side_effect=client), patch("providers.x.time.sleep"):
        yield fake


def tweet_created(tweet_id="1001"):
    return json_response({"data": {"id": tweet_id, "text": "t"}}, 201)


def body_of(request: httpx.Request) -> dict:
    import json

    return json.loads(request.content)


def write_file(tmp_path, name, size=10):
    path = tmp_path / name
    path.write_bytes(b"x" * size)
    return str(path)


class TestDeclarations:
    def test_oauth_with_pkce_and_user_supplied_credentials(self):
        p = provider()
        assert p.auth_type is AuthType.OAUTH2
        assert XProvider.uses_pkce is True
        assert XProvider.user_supplied_credentials is True
        assert XProvider.connect_url_name == "connect_x"
        assert XProvider.supports_connection_link is False
        assert p.required_scopes == ["tweet.read", "tweet.write", "users.read", "media.write", "offline.access"]

    def test_a_reply_chain_of_up_to_25_posts_with_media(self):
        assert XProvider.thread.mode is ThreadMode.REPLY_CHAIN
        assert XProvider.thread.max_parts == 25
        assert XProvider.thread.media_per_part is True

    def test_text_limit_and_mentions(self):
        assert provider().max_caption_length == 280
        assert XProvider.mention_lookup is True

    def test_registered_and_there_are_no_platform_wide_credentials(self, settings):
        assert isinstance(get_provider("x", CREDS), XProvider)
        assert "x" not in settings.PLATFORM_CREDENTIALS_FROM_ENV
        assert PROVIDER_REGISTRY["x"] is XProvider

    def test_media_rules(self):
        images = media_rules_for("x", PostType.IMAGE)
        assert images.max_items == 4
        assert images.max_image_bytes == 5 * 1024 * 1024
        video = media_rules_for("x", PostType.VIDEO)
        assert video.max_items == 1
        assert {"max_duration", "max_video_bytes"} <= video.advisory

    def test_post_settings(self):
        assert [s.key for s in XProvider.post_settings] == [
            "reply_settings",
            "poll_options",
            "poll_duration",
            "community_id",
            "made_with_ai",
            "paid_partnership",
        ]

    def test_premium_raises_the_limit_through_the_account(self):
        account = MagicMock(platform_data={"premium": True})
        assert XProvider.char_limit_for_account(account) == 25000
        assert XProvider.char_limit_for_account(MagicMock(platform_data={})) is None


class TestAuthorizeUrl:
    def test_pkce_s256_challenge_and_scopes(self):
        verifier = "v" * 64
        url = provider().get_auth_url("https://app.example/cb/x/", "st", code_verifier=verifier)
        parsed = urlparse(url)
        assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == "https://x.com/i/oauth2/authorize"
        query = parse_qs(parsed.query)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        assert query["code_challenge"] == [challenge]
        assert query["code_challenge_method"] == ["S256"]
        assert query["client_id"] == ["app-id"]
        assert query["redirect_uri"] == ["https://app.example/cb/x/"]
        assert query["response_type"] == ["code"]
        assert query["scope"] == ["tweet.read tweet.write users.read media.write offline.access"]
        assert "%20" in parsed.query
        assert "app-secret" not in url

    def test_a_verifier_is_required(self):
        with pytest.raises(OAuthError):
            provider().get_auth_url("https://app/cb/", "st")


class TestTokens:
    BODY = {
        "access_token": "at",
        "refresh_token": "rt",
        "expires_in": 7200,
        "scope": "tweet.read tweet.write users.read media.write offline.access",
    }

    def test_exchange_uses_basic_auth_and_the_verifier(self, x):
        x.on("POST", "/2/oauth2/token", json_response(self.BODY))
        tokens = provider().exchange_code("code", "https://app/cb/", code_verifier="ver")
        request = x.requests[0]
        assert request.headers["Authorization"] == "Basic " + base64.b64encode(b"app-id:app-secret").decode()
        form = parse_qs(request.content.decode())
        assert form == {
            "grant_type": ["authorization_code"],
            "code": ["code"],
            "redirect_uri": ["https://app/cb/"],
            "code_verifier": ["ver"],
        }
        assert (tokens.access_token, tokens.refresh_token, tokens.expires_in) == ("at", "rt", 7200)

    def test_no_refresh_token_is_refused(self, x):
        x.on("POST", "/2/oauth2/token", json_response({"access_token": "at", "scope": "tweet.write"}))
        with pytest.raises(OAuthError, match="refresh token"):
            provider().exchange_code("c", "https://app/cb/", code_verifier="v")

    def test_a_missing_scope_is_named(self, x):
        body = {**self.BODY, "scope": "tweet.read users.read offline.access"}
        x.on("POST", "/2/oauth2/token", json_response(body))
        with pytest.raises(OAuthError, match="tweet.write, media.write"):
            provider().exchange_code("c", "https://app/cb/", code_verifier="v")

    def test_a_rejected_code_is_an_oauth_error(self, x):
        x.on("POST", "/2/oauth2/token", json_response({"error": "invalid_request"}, 400))
        with pytest.raises(OAuthError):
            provider().exchange_code("c", "https://app/cb/", code_verifier="v")

    def test_refresh_returns_the_rotated_refresh_token(self, x):
        x.on("POST", "/2/oauth2/token", json_response({**self.BODY, "access_token": "new", "refresh_token": "rt2"}))
        tokens = provider().refresh_token("rt")
        assert parse_qs(x.requests[0].content.decode()) == {"grant_type": ["refresh_token"], "refresh_token": ["rt"]}
        assert (tokens.access_token, tokens.refresh_token) == ("new", "rt2")

    def test_refresh_keeps_the_old_token_when_x_sends_no_new_one(self, x):
        x.on("POST", "/2/oauth2/token", json_response({"access_token": "new", "expires_in": 7200}))
        assert provider().refresh_token("rt").refresh_token == "rt"

    def test_a_revoked_refresh_token_needs_a_reconnect(self, x):
        x.on("POST", "/2/oauth2/token", json_response({"error": "invalid_grant"}, 400))
        with pytest.raises(TokenExpiredError) as caught:
            provider().refresh_token("rt")
        assert caught.value.retryable is False
        assert caught.value.raw_response == {"error": "invalid_grant"}

    def test_revoke(self, x):
        x.on("POST", "/2/oauth2/revoke", json_response({"revoked": True}))
        assert provider().revoke_token("tok") is True
        assert parse_qs(x.requests[0].content.decode()) == {"token": ["tok"]}

    def test_a_failed_revoke_is_reported_not_raised(self, x):
        x.on("POST", "/2/oauth2/revoke", json_response({}, 500))
        assert provider().revoke_token("tok") is False


class TestProfile:
    def user(self, **fields):
        return json_response(
            {
                "data": {
                    "id": "42",
                    "name": "Alice",
                    "username": "alice",
                    "profile_image_url": "https://img/a.png",
                    "public_metrics": {"followers_count": 77},
                    **fields,
                }
            }
        )

    def test_me(self, x):
        x.on("GET", "/2/users/me", self.user(subscription_type="None", verified_type="none"))
        profile = provider().get_profile("at")
        assert x.requests[0].headers["Authorization"] == "Bearer at"
        assert "subscription_type" in x.requests[0].url.params["user.fields"]
        assert (profile.platform_id, profile.name, profile.handle, profile.follower_count) == (
            "42",
            "Alice",
            "alice",
            77,
        )
        assert profile.extra == {"account_data": {"premium": False}}

    @pytest.mark.parametrize(
        "fields", [{"subscription_type": "Premium"}, {"subscription_type": "PremiumPlus"}, {"verified_type": "blue"}]
    )
    def test_premium_accounts_are_detected(self, x, fields):
        x.on("GET", "/2/users/me", self.user(**fields))
        assert provider().get_profile("at").extra["account_data"]["premium"] is True


class TestSettings:
    def test_valid(self):
        assert post_settings_error_for("x", {"reply_settings": "following", "poll_options": "Yes | No"}) == ""
        assert post_settings_error_for("x", {}) == ""

    def test_unsupported_reply_setting(self):
        assert "unsupported" in post_settings_error_for("x", {"reply_settings": "nobody"})

    @pytest.mark.parametrize("options", ["Only one", "a|b|c|d|e", f"ok | {'x' * 26}"])
    def test_bad_poll_options(self, options):
        assert post_settings_error_for("x", {"poll_options": options})

    def test_parse_poll_options(self):
        assert parse_poll_options(" Yes |No||  ") == ["Yes", "No"]
        assert parse_poll_options(None) == []


class TestPublishText:
    def test_text_post(self, x):
        x.on("POST", "/2/tweets", tweet_created("555"))
        result = provider().publish_post("at", PublishContent(text="Hello X"))
        assert result.platform_post_id == "555"
        assert result.url == "https://x.com/i/web/status/555"
        assert body_of(x.requests[0]) == {"text": "Hello X"}
        assert x.requests[0].headers["Authorization"] == "Bearer at"

    def test_reply_settings_everyone_is_the_default_and_sends_nothing(self, x):
        x.on("POST", "/2/tweets", tweet_created())
        provider().publish_post("at", PublishContent(text="a", extra={"reply_settings": "everyone"}))
        assert "reply_settings" not in body_of(x.requests[0])

    @pytest.mark.parametrize("value", ["following", "mentionedUsers", "verified"])
    def test_restricted_replies(self, x, value):
        x.on("POST", "/2/tweets", tweet_created())
        provider().publish_post("at", PublishContent(text="a", extra={"reply_settings": value}))
        assert body_of(x.requests[0])["reply_settings"] == value

    def test_poll(self, x):
        x.on("POST", "/2/tweets", tweet_created())
        extra = {"poll_options": "Yes | No | Maybe", "poll_duration": "360"}
        provider().publish_post("at", PublishContent(text="Vote", extra=extra))
        assert body_of(x.requests[0])["poll"] == {"options": ["Yes", "No", "Maybe"], "duration_minutes": 360}

    def test_poll_duration_defaults_to_a_day_and_is_clamped(self, x):
        x.on("POST", "/2/tweets", tweet_created())
        provider().publish_post("at", PublishContent(text="Vote", extra={"poll_options": "a|b"}))
        assert body_of(x.requests[0])["poll"]["duration_minutes"] == 1440
        assert XProvider._poll_minutes({"poll_duration": "1"}) == 5
        assert XProvider._poll_minutes({"poll_duration": "999999"}) == 10080
        assert XProvider._poll_minutes({"poll_duration": "soon"}) == 1440

    def test_a_poll_with_media_is_refused_before_any_request(self, x, tmp_path):
        content = PublishContent(
            text="Vote",
            media_files=[write_file(tmp_path, "a.png")],
            media_types=["image"],
            extra={"poll_options": "a|b"},
        )
        with pytest.raises(PublishError, match="poll together with media") as caught:
            provider().publish_post("at", content)
        assert caught.value.retryable is False
        assert x.requests == []

    def test_invalid_settings_are_permanent(self, x):
        with pytest.raises(PublishError) as caught:
            provider().publish_post("at", PublishContent(text="a", extra={"poll_options": "only one"}))
        assert caught.value.retryable is False

    def test_empty_post_is_refused(self, x):
        with pytest.raises(PublishError, match="text or media"):
            provider().publish_post("at", PublishContent(text="  "))
        assert x.requests == []

    def test_no_id_in_the_answer_is_ambiguous(self, x):
        x.on("POST", "/2/tweets", json_response({"data": {}}, 201))
        with pytest.raises(APIError) as caught:
            provider().publish_post("at", PublishContent(text="a"))
        assert caught.value.ambiguous_write is True

    def test_first_comment_is_a_reply(self, x):
        x.on("POST", "/2/tweets", tweet_created("777"))
        result = provider().publish_comment("at", "555", "first!")
        assert body_of(x.requests[0]) == {"text": "first!", "reply": {"in_reply_to_tweet_id": "555"}}
        assert result.platform_comment_id == "777"


class TestImages:
    def test_images_use_the_simple_upload_then_attach(self, x, tmp_path):
        x.on("POST", "/2/media/upload", json_response({"data": {"id": "m1"}}), json_response({"data": {"id": "m2"}}))
        x.on("POST", "/2/tweets", tweet_created())
        content = PublishContent(
            text="pics",
            media_files=[write_file(tmp_path, "a.png"), write_file(tmp_path, "b.jpg")],
            media_types=["image", "image"],
        )
        provider().publish_post("at", content)
        uploads = x.calls("POST", "/2/media/upload")
        assert len(uploads) == 2
        assert b'name="media_category"' in uploads[0].content and b"tweet_image" in uploads[0].content
        assert body_of(x.calls("POST", "/2/tweets")[0])["media"] == {"media_ids": ["m1", "m2"]}

    def test_more_than_four_images_is_refused(self, x, tmp_path):
        files = [write_file(tmp_path, f"{i}.png") for i in range(5)]
        with pytest.raises(PublishError, match="at most 4"):
            provider().publish_post("at", PublishContent(text="x", media_files=files, media_types=["image"] * 5))
        assert x.requests == []

    def test_a_gif_cannot_share_a_post_with_an_image(self, x, tmp_path):
        files = [write_file(tmp_path, "a.png"), write_file(tmp_path, "b.gif")]
        with pytest.raises(PublishError, match="single GIF or video"):
            provider().publish_post("at", PublishContent(text="x", media_files=files, media_types=["image", "gif"]))

    def test_an_oversized_gif_is_refused(self, x, tmp_path):
        with patch("providers.x.MAX_GIF_BYTES", 5), pytest.raises(PublishError, match="15 MB"):
            provider().publish_post(
                "at",
                PublishContent(text="x", media_files=[write_file(tmp_path, "a.gif", 50)], media_types=["gif"]),
            )

    def test_a_timeout_while_uploading_is_not_ambiguous(self, x, tmp_path):
        """Nothing is posted yet, so the engine may retry instead of parking the post as unconfirmed."""
        x.on("POST", "/2/media/upload", httpx.ReadTimeout("slow"))
        content = PublishContent(text="x", media_files=[write_file(tmp_path, "a.png")], media_types=["image"])
        with pytest.raises(APIError) as caught:
            provider().publish_post("at", content)
        assert caught.value.ambiguous_write is False
        assert x.calls("POST", "/2/tweets") == []


class TestVideo:
    def video(self, tmp_path, size=10, name="clip.mp4"):
        return PublishContent(
            text="watch",
            media_files=[write_file(tmp_path, name, size)],
            media_types=["video"],
            post_type=PostType.VIDEO,
        )

    def routes(self, x, final, *statuses):
        x.on("POST", "/2/media/upload/initialize", json_response({"data": {"id": "v1", "expires_after_secs": 86400}}))
        x.on("POST", "/2/media/upload/v1/append", httpx.Response(204))
        x.on("POST", "/2/media/upload/v1/finalize", json_response({"data": {"id": "v1", **final}}))
        if statuses:
            x.on("GET", "/2/media/upload", *statuses)
        x.on("POST", "/2/tweets", tweet_created())

    def test_chunked_upload_in_segments(self, x, tmp_path):
        self.routes(x, {})
        with patch("providers.x.CHUNK_BYTES", 4):
            provider().publish_post("at", self.video(tmp_path, size=10))
        assert body_of(x.calls("POST", "/2/media/upload/initialize")[0]) == {
            "media_type": "video/mp4",
            "total_bytes": 10,
            "media_category": "tweet_video",
        }
        appends = x.calls("POST", "/2/media/upload/v1/append")
        assert len(appends) == 3
        for index, request in enumerate(appends):
            assert f'name="segment_index"\r\n\r\n{index}\r\n'.encode() in request.content
        assert len(x.calls("POST", "/2/media/upload/v1/finalize")) == 1
        assert x.calls("GET", "/2/media/upload") == []
        assert body_of(x.calls("POST", "/2/tweets")[0])["media"] == {"media_ids": ["v1"]}

    def test_default_chunk_size_is_five_megabytes(self):
        assert CHUNK_BYTES == 5 * 1024 * 1024

    def test_waits_for_processing_then_posts(self, x, tmp_path):
        pending = {"processing_info": {"state": "pending", "check_after_secs": 1}}
        running = {"data": {"id": "v1", "processing_info": {"state": "in_progress", "check_after_secs": 2}}}
        done = {"data": {"id": "v1", "processing_info": {"state": "succeeded"}}}
        self.routes(x, pending, json_response(running), json_response(done))
        provider().publish_post("at", self.video(tmp_path))
        statuses = x.calls("GET", "/2/media/upload")
        assert len(statuses) == 2
        assert dict(statuses[0].url.params) == {"command": "STATUS", "media_id": "v1"}
        assert len(x.calls("POST", "/2/tweets")) == 1

    def test_failed_processing_is_permanent_and_nothing_is_posted(self, x, tmp_path):
        failed = {"data": {"id": "v1", "processing_info": {"state": "failed", "error": {"code": 1}}}}
        self.routes(x, {"processing_info": {"state": "pending"}}, json_response(failed))
        with pytest.raises(PublishError, match="couldn't process") as caught:
            provider().publish_post("at", self.video(tmp_path))
        assert caught.value.retryable is False
        assert x.calls("POST", "/2/tweets") == []

    def test_processing_that_outlasts_the_wait_is_retryable(self, x, tmp_path):
        running = json_response({"data": {"id": "v1", "processing_info": {"state": "in_progress"}}})
        self.routes(x, {"processing_info": {"state": "pending"}}, running)
        ticks = iter([0, 0, 1000, 1000, 1000])
        with (
            patch("providers.x.time.monotonic", lambda: next(ticks)),
            patch("providers.base.time.monotonic", lambda: next(ticks)),
            pytest.raises(PublishError, match="too long") as caught,
        ):
            provider().publish_post("at", self.video(tmp_path))
        assert caught.value.retryable is True

    def test_a_retry_reuses_the_upload_instead_of_sending_the_file_again(self, x, tmp_path):
        store: dict = {}
        cache = MagicMock(get=lambda key: store.get(key), set=lambda key, value, timeout: store.update({key: value}))
        self.routes(x, {"processing_info": {"state": "pending"}}, json_response({"data": {"id": "v1"}}))
        content = self.video(tmp_path)
        content.idempotency_key = "pp-1"
        content.upload_cache = cache
        x.on("POST", "/2/tweets", httpx.ReadTimeout("lost"), tweet_created())

        with pytest.raises(APIError):
            provider().publish_post("at", content)
        provider().publish_post("at", content)

        assert len(x.calls("POST", "/2/media/upload/initialize")) == 1
        assert body_of(x.calls("POST", "/2/tweets")[-1])["media"] == {"media_ids": ["v1"]}


class TestErrors:
    def failing(self, x, status, body=None, headers=None):
        x.on("POST", "/2/tweets", json_response(body or {}, status, headers))
        with pytest.raises(Exception) as caught:  # noqa: PT011
            provider().publish_post("at", PublishContent(text="a"))
        return caught.value

    def test_429_with_the_reset_header_is_retried_after_the_window(self, x):
        reset = int(datetime(2030, 1, 1, tzinfo=UTC).timestamp())
        error = self.failing(x, 429, {"title": "Too Many Requests"}, {"x-rate-limit-reset": str(reset)})
        assert isinstance(error, QuotaExceededError)
        assert error.resets_at == datetime(2030, 1, 1, tzinfo=UTC)
        assert error.retryable is True
        assert error.status_code == 429

    def test_429_without_the_header_is_a_plain_rate_limit(self, x):
        error = self.failing(x, 429)
        assert type(error) is RateLimitError

    def test_a_bad_reset_header_is_ignored(self, x):
        assert type(self.failing(x, 429, headers={"x-rate-limit-reset": "soon"})) is RateLimitError

    @pytest.mark.parametrize(
        ("status", "body"),
        [
            (402, {"title": "Payment Required"}),
            (429, {"title": "UsageCapExceeded", "type": "https://api.twitter.com/2/problems/usage-capped"}),
            (403, {"title": "CreditsDepleted"}),
        ],
    )
    def test_a_spent_budget_is_permanent_with_a_clear_message(self, x, status, body):
        error = self.failing(x, status, body, {"x-rate-limit-reset": "4000000000"})
        assert isinstance(error, PublishError)
        assert str(error) == BUDGET_MESSAGE
        assert error.retryable is False

    def test_duplicate_content(self, x):
        error = self.failing(x, 403, {"detail": "You are not allowed to create a Tweet with duplicate content."})
        assert str(error) == DUPLICATE_MESSAGE
        assert error.retryable is False

    def test_a_rejection_shows_xs_own_sentence_and_is_permanent(self, x):
        error = self.failing(x, 400, {"detail": "Your Post text is too long."})
        assert isinstance(error, PublishError)
        assert "too long" in str(error)
        assert error.retryable is False
        assert error.status_code == 400

    def test_401_stays_retryable_so_the_token_is_refreshed(self, x):
        error = self.failing(x, 401, {"title": "Unauthorized"})
        assert isinstance(error, APIError)
        assert error.status_code == 401
        assert error.retryable is True

    def test_a_5xx_on_the_post_is_ambiguous(self, x):
        error = self.failing(x, 503)
        assert isinstance(error, APIError)
        assert error.ambiguous_write is True

    def test_a_timeout_on_the_post_is_ambiguous_and_never_retried_blindly(self, x):
        x.on("POST", "/2/tweets", httpx.ReadTimeout("slow"))
        with pytest.raises(APIError) as caught:
            provider().publish_post("at", PublishContent(text="a"))
        assert caught.value.ambiguous_write is True
        assert len(x.requests) == 1


class TestThreads:
    def test_each_part_replies_to_the_part_before_it(self, x):
        x.on("POST", "/2/tweets", tweet_created("2"), tweet_created("3"))
        p = provider()
        root = PublishResult(platform_post_id="1")
        second = p.publish_thread_part("at", PublishContent(text="two"), root=root, parent=root)
        p.publish_thread_part("at", PublishContent(text="three"), root=root, parent=second)
        assert [body_of(r)["reply"] for r in x.requests] == [
            {"in_reply_to_tweet_id": "1"},
            {"in_reply_to_tweet_id": "2"},
        ]

    def test_parts_do_not_inherit_the_first_posts_poll_but_keep_reply_settings(self, x):
        x.on("POST", "/2/tweets", tweet_created("2"))
        root = PublishResult(platform_post_id="1")
        extra = {"poll_options": "a|b", "poll_duration": "60", "reply_settings": "following"}
        provider().publish_thread_part("at", PublishContent(text="two", extra=extra), root=root, parent=root)
        body = body_of(x.requests[0])
        assert "poll" not in body
        assert body["reply_settings"] == "following"

    def test_parts_and_first_comments_can_be_reconciled(self):
        from apps.publisher.engine import _can_reconcile_comments, _can_reconcile_thread_parts

        assert _can_reconcile_thread_parts(provider()) is True
        assert _can_reconcile_comments(provider()) is True


class TestReconcile:
    """An ambiguous reply is looked up on the account's own timeline before it is sent again."""

    def timeline(self, x, *posts):
        x.on("GET", "/2/users/me", json_response({"data": {"id": "42", "username": "alice"}}))
        x.on("GET", "/2/users/42/tweets", json_response({"data": list(posts)} if posts else {"meta": {}}))

    def find(self, text, parent="100"):
        return provider().find_thread_part(
            "at", root=PublishResult(platform_post_id="1"), parent=PublishResult(platform_post_id=parent), text=text
        )

    def test_finds_the_part_among_the_posts_since_its_parent(self, x):
        self.timeline(x, {"id": "103", "text": "something else"}, {"id": "102", "text": "Part two &amp; more"})
        assert self.find("Part two & more") == "102"
        request = x.calls("GET", "/2/users/42/tweets")[0]
        assert request.url.params["since_id"] == "100"
        assert request.headers["Authorization"] == "Bearer at"

    def test_links_rewritten_to_tco_still_match(self, x):
        self.timeline(x, {"id": "102", "text": "Read this https://t.co/abc123"})
        assert self.find("Read this https://example.com/a/long/article") == "102"

    def test_a_long_posts_shortened_text_matches_as_a_prefix(self, x):
        self.timeline(x, {"id": "102", "text": "word " * 50 + "\u2026"})
        assert self.find("word " * 400) == "102"

    def test_nothing_published_reads_as_missing(self, x):
        self.timeline(x)
        assert self.find("Part two") is None

    def test_a_different_text_is_not_taken_for_the_part(self, x):
        self.timeline(x, {"id": "102", "text": "Part three"})
        assert self.find("Part two") is None

    def test_a_first_comment_is_looked_up_after_its_post(self, x):
        self.timeline(x, {"id": "201", "text": "First!"})
        assert provider().find_own_comment("at", "200", "First!") == "201"
        assert x.calls("GET", "/2/users/42/tweets")[0].url.params["since_id"] == "200"

    def test_a_failed_lookup_raises_so_the_part_is_not_sent_blind(self, x):
        x.on("GET", "/2/users/me", json_response({"title": "Service Unavailable"}, status=503))
        with pytest.raises(APIError):
            self.find("Part two")


class TestWeightedLength:
    """twitter-text v3: URLs 23, code points outside the light ranges 2, an emoji sequence 2."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("", 0),
            ("a" * 280, 280),
            ("日本語", 6),
            ("한국어", 6),
            ("“curly” — dash…", 16),  # U+2026 is outside the light ranges
            ("see https://example.com/a/very/long/path?query=1. ok", 4 + 23 + 4),
            ("www.example.org/x", 23),
            ("visit example.com, not me@example.com", 6 + 23 + 20),
            ("file.txt", 8),
            ("👍", 2),
            ("👍🏽", 2),
            ("👨\u200d👩\u200d👧", 2),
            ("🇯🇵", 2),
            ("❤️", 2),
            ("1️⃣", 2),
            ("e\u0301", 1),
        ],
    )
    def test_counts(self, text, expected):
        assert weighted_length(text) == expected

    def test_the_caption_counter_uses_it_for_x_only(self):
        text = "日本 https://example.com/" + "a" * 100
        assert caption_wire_length("x", text) == 2 + 2 + 1 + 23
        assert caption_wire_length("bluesky", text) == len(text)
        assert XProvider.caption_counting == "weighted"
        assert PROVIDER_REGISTRY["bluesky"].caption_counting == "characters"

    def test_a_post_of_280_cjk_characters_is_over_the_limit(self):
        assert weighted_length("中" * 140) == 280
        assert weighted_length("中" * 141) > provider().max_caption_length


class TestDelete:
    def test_delete(self, x):
        x.on("DELETE", "/2/tweets/555", json_response({"data": {"deleted": True}}))
        assert provider().delete_post("at", "555") is True

    def test_not_deleted(self, x):
        x.on("DELETE", "/2/tweets/555", json_response({"data": {"deleted": False}}))
        assert provider().delete_post("at", "555") is False

    def test_a_post_that_is_already_gone_counts_as_deleted(self, x):
        x.on("DELETE", "/2/tweets/555", json_response({"detail": "not found"}, 404))
        assert provider().delete_post("at", "555") is True

    def test_other_failures_raise(self, x):
        x.on("DELETE", "/2/tweets/555", json_response({"detail": "forbidden"}, 403))
        with pytest.raises(PublishError):
            provider().delete_post("at", "555")


class TestMentions:
    def test_search(self, x):
        users = [
            {"id": "1", "name": "Ann", "username": "ann", "profile_image_url": "https://i/a.png"},
            {"id": "2", "name": "Bob", "username": "bob"},
            {"id": "3", "name": "No handle"},
        ]
        x.on("GET", "/2/users/search", json_response({"data": users}))
        found = provider().search_mentions("at", "an", limit=5)
        assert [(m.handle, m.name, m.avatar_url) for m in found] == [
            ("ann", "Ann", "https://i/a.png"),
            ("bob", "Bob", ""),
        ]
        assert dict(x.requests[0].url.params)["query"] == "an"

    def test_no_matches(self, x):
        x.on("GET", "/2/users/search", json_response({"meta": {"result_count": 0}}))
        assert provider().search_mentions("at", "zz") == []

    def test_an_app_without_access_raises_for_the_caller_to_swallow(self, x):
        x.on("GET", "/2/users/search", json_response({"detail": "client-not-enrolled"}, 403))
        with pytest.raises(PublishError):
            provider().search_mentions("at", "an")


class TestCommunityAndLabels:
    def test_community_and_labels_are_sent_in_the_post_body(self, x):
        x.on("POST", "/2/tweets", tweet_created())
        provider().publish_post(
            "at",
            PublishContent(
                text="a", extra={"community_id": "1493446837214187523", "made_with_ai": True, "paid_partnership": True}
            ),
        )
        body = body_of(x.requests[0])
        assert body["community_id"] == "1493446837214187523"
        assert body["made_with_ai"] is True
        assert body["paid_partnership"] is True

    def test_unset_options_send_nothing(self, x):
        x.on("POST", "/2/tweets", tweet_created())
        provider().publish_post("at", PublishContent(text="a", extra={"made_with_ai": False, "community_id": ""}))
        body = body_of(x.requests[0])
        assert not {"community_id", "made_with_ai", "paid_partnership"} & set(body)

    @pytest.mark.parametrize("value", ["abc", "12 34", "1" * 20, "-5"])
    def test_community_id_must_be_a_number(self, value):
        assert "Community ID" in post_settings_error_for("x", {"community_id": value})
