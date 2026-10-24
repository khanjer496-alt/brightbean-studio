"""Lemmy provider (instance login, JWT only, community posts), with every HTTP call mocked."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest

from providers import get_provider
from providers.exceptions import APIError, OAuthError, PublishError, RateLimitError
from providers.lemmy import LemmyProvider, normalize_community, normalize_instance_url
from providers.types import AuthType, PostType, PublishContent, ThreadMode

CREDS = {"instance_url": "https://lemmy.example"}


@pytest.fixture(autouse=True)
def public_instance():
    with patch("providers.lemmy.is_safe_url", return_value=True):
        yield


def provider() -> LemmyProvider:
    return LemmyProvider(CREDS)


def ok(body, status=200):
    return MagicMock(status_code=status, json=MagicMock(return_value=body))


def error_response(status, body):
    return httpx.Response(status, json=body, request=httpx.Request("POST", "https://lemmy.example/api/v3/post"))


class TestDeclarations:
    def test_session_auth_with_comment_threads(self):
        p = provider()
        assert p.auth_type is AuthType.SESSION
        assert LemmyProvider.thread.mode is ThreadMode.COMMENTS
        assert LemmyProvider.connect_url_name == "connect_lemmy"
        assert LemmyProvider.supports_connection_link is False
        assert PostType.IMAGE in p.supported_post_types
        assert isinstance(get_provider("lemmy", CREDS), LemmyProvider)

    def test_post_settings(self):
        assert [s.key for s in LemmyProvider.post_settings] == ["community", "nsfw"]
        assert LemmyProvider.validate_post_settings({}) == "Community is required."
        assert "Community must be" in LemmyProvider.validate_post_settings({"community": "bad name!"})
        assert LemmyProvider.validate_post_settings({"community": "selfhosted@lemmy.world"}) == ""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("lemmy.world", "https://lemmy.world"),
            ("https://lemmy.ml/c/x", "https://lemmy.ml"),
            ("me@lemmy.ml", "https://lemmy.ml"),
            ("ftp://x.y", ""),
            ("", ""),
        ],
    )
    def test_instance_url(self, raw, expected):
        assert normalize_instance_url(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "expected"), [("!rust@lemmy.ml", "rust@lemmy.ml"), ("/c/rust", "rust"), ("a b", ""), ("", "")]
    )
    def test_community(self, raw, expected):
        assert normalize_community(raw) == expected

    def test_credentials_come_from_the_account_only_when_the_instance_is_public(self):
        account = SimpleNamespace(id="a", instance_url="https://lemmy.example")
        assert LemmyProvider.credentials_for_account(account) == {"instance_url": "https://lemmy.example"}
        with patch("providers.lemmy.is_safe_url", return_value=False):
            assert LemmyProvider.credentials_for_account(account) == {}

    def test_permalink(self):
        account = SimpleNamespace(instance_url="https://lemmy.example/")
        assert LemmyProvider.permalink_for("42", account) == "https://lemmy.example/post/42"


class TestSsrf:
    def test_a_private_instance_is_never_called(self):
        p = provider()
        with (
            patch("providers.lemmy.is_safe_url", return_value=False),
            patch.object(p, "_request") as request,
            pytest.raises(PublishError) as exc,
        ):
            p.publish_post("jwt", PublishContent(title="t", extra={"community": "rust"}))
        assert exc.value.retryable is False
        request.assert_not_called()


class TestConnect:
    def test_login_keeps_only_the_jwt_and_sends_the_password_once(self):
        p = provider()
        with patch.object(p, "_request", return_value=ok({"jwt": "the-jwt"})) as request:
            tokens = p.login("alice", "pw", "123456")
        assert request.call_args.args[:2] == ("POST", "https://lemmy.example/api/v3/user/login")
        assert request.call_args.kwargs["json"] == {
            "username_or_email": "alice",
            "password": "pw",
            "totp_2fa_token": "123456",
        }
        assert tokens.access_token == "the-jwt"
        assert tokens.refresh_token is None
        assert "pw" not in repr(tokens)

    def test_login_without_a_jwt_is_refused(self):
        p = provider()
        with patch.object(p, "_request", return_value=ok({"verify_email_sent": True})), pytest.raises(OAuthError):
            p.login("alice", "pw")

    def test_profile(self):
        site = {
            "my_user": {
                "local_user_view": {
                    "person": {"id": 7, "name": "alice", "display_name": "Alice", "avatar": "https://a/b.png"}
                }
            }
        }
        p = provider()
        with patch.object(p, "_request", return_value=ok(site)) as request:
            profile = p.get_profile("jwt")
        assert request.call_args.kwargs["access_token"] == "jwt"
        assert (profile.platform_id, profile.handle, profile.name) == (
            "7@lemmy.example",
            "alice@lemmy.example",
            "Alice",
        )


class TestPublishing:
    def test_a_post_resolves_the_community_then_posts(self):
        p = provider()
        replies = [
            ok({"community_view": {"community": {"id": 99}}}),
            ok({"post_view": {"post": {"id": 5, "ap_id": "https://lemmy.example/post/5"}}}),
        ]
        content = PublishContent(
            text="Body",
            title="Title",
            media_urls=["https://cdn/x.png"],
            extra={"community": "rust@lemmy.ml", "nsfw": True},
        )
        with patch.object(p, "_request", side_effect=replies) as request:
            result = p.publish_post("jwt", content)
        assert request.call_args_list[0].kwargs["params"] == {"name": "rust@lemmy.ml"}
        assert request.call_args_list[1].kwargs["json"] == {
            "community_id": 99,
            "name": "Title",
            "nsfw": True,
            "body": "Body",
            "url": "https://cdn/x.png",
        }
        assert (result.platform_post_id, result.url) == ("5", "https://lemmy.example/post/5")

    def test_a_link_beats_the_picture(self):
        p = provider()
        replies = [ok({"community_view": {"community": {"id": 1}}}), ok({"post_view": {"post": {"id": 5}}})]
        content = PublishContent(
            title="T", link_url="https://e.com", media_urls=["https://cdn/x.png"], extra={"community": "rust"}
        )
        with patch.object(p, "_request", side_effect=replies) as request:
            p.publish_post("jwt", content)
        assert request.call_args_list[1].kwargs["json"]["url"] == "https://e.com"

    @pytest.mark.parametrize(
        "content",
        [
            PublishContent(title="", extra={"community": "rust"}),
            PublishContent(title="x" * 201, extra={"community": "rust"}),
            PublishContent(title="t", extra={}),
            PublishContent(title="t", text="x" * 10001, extra={"community": "rust"}),
        ],
    )
    def test_permanent_refusals_make_no_call(self, content):
        p = provider()
        with patch.object(p, "_request") as request, pytest.raises(PublishError) as exc:
            p.publish_post("jwt", content)
        assert exc.value.retryable is False
        request.assert_not_called()

    def test_unknown_community(self):
        p = provider()
        with patch.object(p, "_request", return_value=ok({})), pytest.raises(PublishError, match="can't find"):
            p.publish_post("jwt", PublishContent(title="t", extra={"community": "nope"}))

    def test_comment_is_a_thread_part(self):
        p = provider()
        with patch.object(p, "_request", return_value=ok({"comment_view": {"comment": {"id": 8}}})) as request:
            result = p.publish_comment("jwt", "5", "reply")
        assert request.call_args.kwargs["json"] == {"post_id": 5, "content": "reply"}
        assert result.platform_comment_id == "8"

    def test_delete(self):
        p = provider()
        with patch.object(p, "_request", return_value=ok({})) as request:
            assert p.delete_post("jwt", "5") is True
        assert request.call_args.kwargs["json"] == {"post_id": 5, "deleted": True}


class TestErrors:
    def test_rate_limit_error_code(self):
        assert isinstance(
            provider()._error_for_response(error_response(400, {"error": "rate_limit_error"})), RateLimitError
        )

    @pytest.mark.parametrize("code", ["not_logged_in", "incorrect_login", "expired_token"])
    def test_a_revoked_login_reads_as_a_rejected_credential(self, code):
        from apps.social_accounts.error_messages import is_credential_rejection

        error = provider()._error_for_response(error_response(400, {"error": code}))
        assert isinstance(error, APIError) and error.status_code == 401
        assert is_credential_rejection(error)

    def test_other_errors_keep_the_base_shape(self):
        error = provider()._error_for_response(error_response(400, {"error": "language_not_allowed"}))
        assert isinstance(error, APIError) and error.status_code == 400
