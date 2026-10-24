"""Reddit provider (OAuth2, text and link posts, comments), with every HTTP call mocked."""

import base64
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from providers import get_provider
from providers.exceptions import APIError, OAuthError, PublishError, RateLimitError
from providers.reddit import RedditProvider, default_user_agent, normalize_subreddit
from providers.types import AuthType, OAuthTokens, PublishContent, ThreadMode

CREDS = {"client_id": "app-id", "client_secret": "app-secret", "user_agent": "web:com.test:v1 (by /u/tester)"}
SCOPES = "identity submit read mysubreddits flair"


def provider(**extra) -> RedditProvider:
    return RedditProvider({**CREDS, **extra})


def ok(body, status=200, headers=None):
    return MagicMock(status_code=status, json=MagicMock(return_value=body), headers=headers or {})


def submitted(**data):
    return ok({"json": {"errors": [], "data": {"id": "abc", "name": "t3_abc", "url": "https://r/x", **data}}})


def error_response(status, body=None, headers=None):
    return httpx.Response(
        status, json=body or {}, headers=headers, request=httpx.Request("POST", "https://oauth.reddit.com/x")
    )


def post_content(**extra):
    base = {"subreddit": "python"}
    return PublishContent(text="Body text", title="A title", extra={**base, **extra})


class TestDeclarations:
    def test_oauth_with_a_comment_thread(self):
        p = provider()
        assert p.auth_type is AuthType.OAUTH2
        assert p.required_scopes == SCOPES.split()
        assert RedditProvider.thread.mode is ThreadMode.COMMENTS
        assert RedditProvider.thread.media_per_part is False
        assert RedditProvider.revokes_grant_on_disconnect is True
        assert RedditProvider.revocation_uses_refresh_token is True

    def test_it_declares_that_it_publishes_no_media(self):
        assert RedditProvider.accepts_media is False
        assert RedditProvider.media_rules == {}
        assert RedditProvider().supported_media_types == []

    def test_post_settings(self):
        keys = [s.key for s in RedditProvider.post_settings]
        assert keys == ["subreddit", "post_kind", "link_url", "flair_id", "extra_subreddits", "nsfw", "spoiler"]
        flair = next(s for s in RedditProvider.post_settings if s.key == "flair_id")
        assert flair.options_from_provider and flair.depends_on == "subreddit"

    def test_registered(self):
        assert isinstance(get_provider("reddit", CREDS), RedditProvider)

    def test_default_user_agent_follows_reddits_format(self):
        assert default_user_agent().startswith("web:com.")
        assert "(by /u/" in default_user_agent()


class TestSubredditNames:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("python", "python"),
            ("r/Python", "Python"),
            ("/r/python/", "python"),
            (" python ", "python"),
            ("a", ""),
            ("a b", ""),
            ("", ""),
        ],
    )
    def test_normalize(self, raw, expected):
        assert normalize_subreddit(raw) == expected


class TestOAuth:
    def test_permanent_grant_with_every_scope(self):
        url = provider().get_auth_url("https://app.example/cb/", "st")
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == "https://www.reddit.com/api/v1/authorize"
        assert query["duration"] == ["permanent"]
        assert query["scope"] == [SCOPES]
        assert query["response_type"] == ["code"]
        assert query["state"] == ["st"]

    def test_exchange_uses_basic_client_auth_and_a_user_agent(self):
        body = {"access_token": "at", "refresh_token": "rt", "expires_in": 3600, "scope": SCOPES}
        p = provider()
        with patch.object(p, "_request", return_value=ok(body)) as request:
            tokens = p.exchange_code("code", "https://app.example/cb/")
        headers = request.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Basic " + base64.b64encode(b"app-id:app-secret").decode()
        assert headers["User-Agent"] == CREDS["user_agent"]
        assert request.call_args.kwargs["data"] == {
            "grant_type": "authorization_code",
            "code": "code",
            "redirect_uri": "https://app.example/cb/",
        }
        assert (tokens.access_token, tokens.refresh_token, tokens.expires_in) == ("at", "rt", 3600)

    def test_a_grant_without_a_refresh_token_is_refused(self):
        p = provider()
        with (
            patch.object(p, "_request", return_value=ok({"access_token": "at", "scope": SCOPES})),
            pytest.raises(OAuthError, match="refresh token"),
        ):
            p.exchange_code("c", "https://x/")

    def test_a_token_error_body_is_an_oauth_error(self):
        p = provider()
        with patch.object(p, "_request", return_value=ok({"error": "invalid_grant"})), pytest.raises(OAuthError):
            p.exchange_code("c", "https://x/")

    def test_a_missing_scope_is_named(self):
        body = {"access_token": "at", "refresh_token": "rt", "scope": "identity submit"}
        p = provider()
        with patch.object(p, "_request", return_value=ok(body)), pytest.raises(OAuthError, match="flair"):
            p.exchange_code("c", "https://x/")

    def test_refresh_keeps_the_refresh_token(self):
        p = provider()
        with patch.object(p, "_request", return_value=ok({"access_token": "new", "expires_in": 3600})) as request:
            tokens = p.refresh_token("rt")
        assert request.call_args.kwargs["data"] == {"grant_type": "refresh_token", "refresh_token": "rt"}
        assert (tokens.access_token, tokens.refresh_token, tokens.expires_in) == ("new", "rt", 3600)

    def test_revoke(self):
        p = provider()
        with patch.object(p, "_request", return_value=ok({})) as request:
            assert p.revoke_token("rt") is True
        assert request.call_args.args[:2] == ("POST", "https://www.reddit.com/api/v1/revoke_token")
        assert request.call_args.kwargs["data"] == {"token": "rt"}

    def test_a_failed_revoke_is_reported_not_raised(self):
        p = provider()
        with patch.object(p, "_request", side_effect=APIError("down", status_code=500)):
            assert p.revoke_token("rt") is False


class TestProfile:
    def test_me(self):
        me = {"id": "u1", "name": "alice", "icon_img": "https://img/a.png?width=256", "total_karma": 120}
        p = provider()
        with patch.object(p, "_request", return_value=ok(me)) as request:
            profile = p.get_profile("at")
        assert request.call_args.args[:2] == ("GET", "https://oauth.reddit.com/api/v1/me")
        assert request.call_args.kwargs["access_token"] == "at"
        assert request.call_args.kwargs["headers"]["User-Agent"] == CREDS["user_agent"]
        assert (profile.platform_id, profile.name, profile.handle) == ("u1", "u/alice", "alice")
        assert profile.avatar_url == "https://img/a.png"
        assert profile.follower_count == 120


class TestSettingsValidation:
    def test_a_text_post_needs_only_a_subreddit(self):
        assert RedditProvider.validate_post_settings({"subreddit": "python"}) == ""

    def test_the_subreddit_is_required(self):
        assert "Subreddit is required" in RedditProvider.validate_post_settings({})

    def test_a_bad_subreddit_name(self):
        assert "Subreddit must be" in RedditProvider.validate_post_settings({"subreddit": "not valid!"})

    def test_a_link_post_needs_a_real_link(self):
        assert "valid link" in RedditProvider.validate_post_settings({"subreddit": "x_y", "post_kind": "link"})
        assert "valid link" in RedditProvider.validate_post_settings(
            {"subreddit": "x_y", "post_kind": "link", "link_url": "javascript:alert(1)"}
        )
        assert (
            RedditProvider.validate_post_settings(
                {"subreddit": "x_y", "post_kind": "link", "link_url": "https://example.com/a"}
            )
            == ""
        )

    def test_unknown_post_kind(self):
        assert "unsupported" in RedditProvider.validate_post_settings({"subreddit": "x_y", "post_kind": "poll"})


class TestFlairOptions:
    def test_flairs_of_the_subreddit(self):
        flairs = [{"id": "f1", "text": "Question"}, {"id": "f2", "text": ""}, {"text": "no id"}]
        p = provider()
        with patch.object(p, "_request", return_value=ok(flairs)) as request:
            options = p.get_setting_options("at", "flair_id", "r/Python")
        assert request.call_args.args[:2] == ("GET", "https://oauth.reddit.com/r/Python/api/link_flair_v2")
        assert options == [{"id": "f1", "name": "Question"}, {"id": "f2", "name": "f2"}]

    def test_no_subreddit_no_request(self):
        with patch.object(provider(), "_request", side_effect=AssertionError("no request")):
            assert provider().get_setting_options("at", "flair_id", "") == []
            assert provider().get_setting_options("at", "flair_id", "../x") == []

    def test_a_subreddit_without_flair_gives_none(self):
        p = provider()
        with patch.object(p, "_request", side_effect=APIError("403", status_code=403, retryable=False)):
            assert p.get_setting_options("at", "flair_id", "private") == []

    def test_other_keys_have_no_options(self):
        with pytest.raises(NotImplementedError):
            provider().get_setting_options("at", "subreddit", "")


class TestPublish:
    def test_text_post(self):
        p = provider()
        with patch.object(p, "_request", return_value=submitted()) as request:
            result = p.publish_post("at", post_content(flair_id="f1", nsfw=True))
        assert request.call_args.args[:2] == ("POST", "https://oauth.reddit.com/api/submit")
        form = request.call_args.kwargs["data"]
        assert form == {
            "api_type": "json",
            "kind": "self",
            "sr": "python",
            "title": "A title",
            "nsfw": "true",
            "spoiler": "false",
            "sendreplies": "true",
            "text": "Body text",
            "flair_id": "f1",
        }
        assert (result.platform_post_id, result.url) == ("t3_abc", "https://r/x")

    def test_link_post_sends_the_url_and_no_text(self):
        p = provider()
        content = PublishContent(
            text="ignored",
            title="T",
            link_url="https://example.com/a",
            extra={"subreddit": "r/Python", "post_kind": "link"},
        )
        with patch.object(p, "_request", return_value=submitted()) as request:
            p.publish_post("at", content)
        form = request.call_args.kwargs["data"]
        assert form["kind"] == "link" and form["url"] == "https://example.com/a" and form["sr"] == "Python"
        assert "text" not in form

    def test_spoiler(self):
        p = provider()
        with patch.object(p, "_request", return_value=submitted()) as request:
            p.publish_post("at", post_content(spoiler=True))
        assert request.call_args.kwargs["data"]["spoiler"] == "true"

    @pytest.mark.parametrize(
        ("content", "fragment"),
        [
            (PublishContent(text="x", title="", extra={"subreddit": "python"}), "title"),
            (PublishContent(text="x", title="t" * 301, extra={"subreddit": "python"}), "300"),
            (PublishContent(text="x", title="t", extra={}), "subreddit"),
            (PublishContent(text="x", title="t", extra={"subreddit": "python"}, media_files=["a.jpg"]), "media"),
            (
                PublishContent(text="x", title="t", extra={"subreddit": "python"}, media_urls=["https://x/a.jpg"]),
                "media",
            ),
            (PublishContent(text="x" * 40001, title="t", extra={"subreddit": "python"}), "40000"),
            (PublishContent(text="x", title="t", extra={"subreddit": "python", "post_kind": "link"}), "link"),
        ],
    )
    def test_refused_before_any_request(self, content, fragment):
        p = provider()
        with patch.object(p, "_request", side_effect=AssertionError("no request")), pytest.raises(PublishError) as exc:
            p.publish_post("at", content)
        assert exc.value.retryable is False
        assert fragment in str(exc.value)

    def test_first_comment_and_thread_parts_are_comments_on_the_post(self):
        reply = ok(
            {
                "json": {
                    "errors": [],
                    "data": {"things": [{"data": {"id": "k1", "name": "t1_k1", "permalink": "/r/x/c/k1"}}]},
                }
            }
        )
        p = provider()
        with patch.object(p, "_request", return_value=reply) as request:
            comment = p.publish_comment("at", "abc", "hello")
        assert request.call_args.args[:2] == ("POST", "https://oauth.reddit.com/api/comment")
        assert request.call_args.kwargs["data"] == {"api_type": "json", "thing_id": "t3_abc", "text": "hello"}
        assert comment.platform_comment_id == "t1_k1"
        assert comment.extra["url"] == "https://www.reddit.com/r/x/c/k1"

    def test_the_base_thread_part_goes_through_publish_comment(self):
        from providers.types import PublishResult

        reply = ok({"json": {"errors": [], "data": {"things": [{"data": {"name": "t1_k2"}}]}}})
        p = provider()
        root = PublishResult(platform_post_id="t3_abc")
        with patch.object(p, "_request", return_value=reply) as request:
            part = p.publish_thread_part("at", PublishContent(text="two"), root=root, parent=root)
        assert request.call_args.kwargs["data"]["thing_id"] == "t3_abc"
        assert part.platform_post_id == "t1_k2"

    def test_a_comment_that_returns_nothing_is_ambiguous(self):
        p = provider()
        with (
            patch.object(p, "_request", return_value=ok({"json": {"errors": [], "data": {"things": []}}})),
            pytest.raises(APIError) as exc,
        ):
            p.publish_comment("at", "t3_abc", "x")
        assert exc.value.ambiguous_write is True

    def test_delete(self):
        p = provider()
        with patch.object(p, "_request", return_value=ok({})) as request:
            assert p.delete_post("at", "t3_abc") is True
        assert request.call_args.kwargs["data"] == {"id": "t3_abc"}


class TestErrorClassification:
    def submit_failing_with(self, errors, **top):
        body = {"json": {"errors": errors, **top}}
        p = provider()
        with patch.object(p, "_request", return_value=ok(body)):
            p.publish_post("at", post_content())

    @pytest.mark.parametrize(
        ("code", "fragment"),
        [
            ("SUBREDDIT_NOEXIST", "doesn't exist"),
            ("SUBREDDIT_NOTALLOWED", "aren't allowed"),
            ("NO_SELFS", "link post"),
            ("NO_LINKS", "text post"),
            ("ALREADY_SUB", "already"),
            ("SUBMIT_VALIDATION_FLAIR_REQUIRED", "flair"),
        ],
    )
    def test_known_rejections_are_permanent_sentences(self, code, fragment):
        with pytest.raises(PublishError, match=fragment) as exc:
            self.submit_failing_with([[code, "whatever", "field"]])
        assert exc.value.retryable is False

    def test_an_unknown_rejection_keeps_reddits_wording_and_is_permanent(self):
        with pytest.raises(PublishError, match="r/python: Something new") as exc:
            self.submit_failing_with([["NEW_CODE", "Something new", None]])
        assert exc.value.retryable is False

    def test_the_posting_cooldown_is_a_retryable_rate_limit_with_its_wait(self):
        with pytest.raises(RateLimitError) as exc:
            self.submit_failing_with(
                [["RATELIMIT", "you are doing that too much. try again in 9 minutes.", "ratelimit"]], ratelimit=540.0
            )
        assert exc.value.retry_after == 540
        assert exc.value.retryable is True

    def test_the_wait_is_read_from_the_message_when_no_number_is_given(self):
        with pytest.raises(RateLimitError) as exc:
            self.submit_failing_with([["RATELIMIT", "you are doing that too much. try again in 45 seconds.", "x"]])
        assert exc.value.retry_after == 45

    def test_http_429_uses_the_reset_header(self):
        err = provider()._error_for_response(error_response(429, headers={"X-Ratelimit-Reset": "120"}))
        assert isinstance(err, RateLimitError) and err.retry_after == 120

    @pytest.mark.parametrize("status", [400, 403, 404])
    def test_client_errors_are_permanent(self, status):
        err = provider()._error_for_response(error_response(status, {"message": "Forbidden"}))
        assert err.retryable is False and err.status_code == status

    def test_401_keeps_its_retries_so_a_refreshed_token_gets_a_turn(self):
        err = provider()._error_for_response(error_response(401, {"message": "Unauthorized"}))
        assert err.retryable is True and err.status_code == 401

    def test_5xx_is_retryable_and_ambiguous_for_a_write(self):
        p = provider()
        with patch("providers.base.httpx.Client") as client, pytest.raises(APIError) as exc:
            client.return_value.__enter__.return_value.request.return_value = error_response(503, {})
            p.publish_post("at", post_content())
        assert exc.value.retryable is True
        assert exc.value.ambiguous_write is True

    def test_a_timeout_on_submit_is_an_ambiguous_write(self):
        p = provider()
        with patch("providers.base.httpx.Client") as client, pytest.raises(APIError) as exc:
            client.return_value.__enter__.return_value.request.side_effect = httpx.ReadTimeout("slow")
            p.publish_post("at", post_content())
        assert exc.value.ambiguous_write is True

    def test_success_without_an_id_is_ambiguous(self):
        p = provider()
        with (
            patch.object(p, "_request", return_value=ok({"json": {"errors": [], "data": {}}})),
            pytest.raises(APIError) as exc,
        ):
            p.publish_post("at", post_content())
        assert exc.value.ambiguous_write is True


def test_oauth_tokens_type_is_used():
    assert isinstance(OAuthTokens(access_token="x"), OAuthTokens)


class TestMultipleSubreddits:
    def _provider(self, flairs=None):
        p = provider()
        p._pause_between_submissions = MagicMock()
        calls = []

        def api(method, path, token, **kwargs):
            calls.append((method, path, kwargs.get("data")))
            if path.endswith("/api/submit"):
                sr = kwargs["data"]["sr"]
                return ok({"json": {"errors": [], "data": {"id": sr, "name": f"t3_{sr}", "url": f"https://r/{sr}"}}})
            if "link_flair_v2" in path:
                return ok(flairs or [{"id": "f-q", "text": "Question"}, {"id": "f-d", "text": "Discussion"}])
            raise AssertionError(path)

        p._api_request = api
        return p, calls

    def test_fans_out_with_per_subreddit_flair_and_spacing(self):
        p, calls = self._provider()
        result = p.publish_post(
            "at",
            post_content(flair_id="primary-flair", extra_subreddits="r/learnpython | question, flask"),
        )
        submits = [c[2] for c in calls if c[1] == "/api/submit"]
        assert [s["sr"] for s in submits] == ["python", "learnpython", "flask"]
        assert [s.get("flair_id") for s in submits] == ["primary-flair", "f-q", None]
        assert p._pause_between_submissions.call_count == 2
        assert result.platform_post_id == "t3_python"
        assert [a["subreddit"] for a in result.extra["also_posted"]] == ["learnpython", "flask"]

    def test_unknown_flair_name_fails_before_anything_is_posted(self):
        p, calls = self._provider()
        with pytest.raises(PublishError) as exc:
            p.publish_post("at", post_content(extra_subreddits="learnpython | Nope"))
        assert "no flair called" in str(exc.value)
        assert not [c for c in calls if c[1] == "/api/submit"]

    def test_a_failure_after_a_success_is_not_retried(self):
        p, calls = self._provider()
        original = p._api_request

        def api(method, path, token, **kwargs):
            if path == "/api/submit" and kwargs["data"]["sr"] == "flask":
                return ok({"json": {"errors": [["RATELIMIT", "try again in 9 minutes"]], "ratelimit": 540}})
            return original(method, path, token, **kwargs)

        p._api_request = api
        with pytest.raises(PublishError) as exc:
            p.publish_post("at", post_content(extra_subreddits="flask"))
        assert exc.value.retryable is False
        assert "Posted to r/python" in str(exc.value)

    def test_single_subreddit_behaviour_is_unchanged(self):
        p, calls = self._provider()
        result = p.publish_post("at", post_content())
        assert len([c for c in calls if c[1] == "/api/submit"]) == 1
        assert "also_posted" not in result.extra
        p._pause_between_submissions.assert_not_called()

    @pytest.mark.parametrize(
        "extra, fragment",
        [
            ("a1, b1, c1, d1, e1", "at most 5"),
            ("python", "only once"),
            ("bad name!", "isn't a subreddit"),
            ("x", "isn't a subreddit"),
        ],
    )
    def test_validation(self, extra, fragment):
        assert fragment in RedditProvider.validate_post_settings({"subreddit": "python", "extra_subreddits": extra})

    def test_the_spacing_stops_when_the_worker_is_stopping(self):
        p = provider()
        content = PublishContent(should_stop=lambda: True)
        with pytest.raises(PublishError):
            p._pause_between_submissions(content)
