"""Slack provider (OAuth v2 install, bot-token posting), with every HTTP call mocked."""

import json
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from providers import get_provider
from providers.exceptions import APIError, OAuthError, PublishError, RateLimitError, TokenExpiredError
from providers.slack import SlackProvider, to_mrkdwn
from providers.types import AuthType, OAuthTokens, PostType, PublishContent, PublishResult, ThreadMode

CREDS = {"client_id": "app-1", "client_secret": "shh", "channel_id": "C1"}


def provider(**extra) -> SlackProvider:
    return SlackProvider({**CREDS, **extra})


def ok(body, status=200):
    return MagicMock(status_code=status, json=MagicMock(return_value={"ok": True, **body}))


def failed(code, status=200):
    return MagicMock(status_code=status, json=MagicMock(return_value={"ok": False, "error": code}))


def slack_api(routes):
    """A ``_request`` stand-in answering by Web API method name; an upload URL answers 200."""
    calls = []

    def request(method, url, **kwargs):
        name = url.rsplit("/", 1)[-1]
        calls.append((method, name, kwargs))
        if url.startswith("https://files.slack.com"):
            return MagicMock(status_code=200)
        result = routes[name]
        return result if isinstance(result, MagicMock) else result(kwargs)

    return request, calls


class TestDeclarations:
    def test_oauth_with_destination_picker(self):
        p = provider()
        assert p.auth_type is AuthType.OAUTH2
        assert p.required_scopes == ["chat:write", "files:write", "channels:read", "groups:read"]
        assert SlackProvider.has_destination_picker is True
        assert SlackProvider.supports_connection_link is False
        assert "/invite" in SlackProvider.no_destinations_hint

    def test_threads_and_media(self):
        assert SlackProvider.thread.mode is ThreadMode.REPLY_CHAIN
        assert SlackProvider.media_rules[PostType.IMAGE].media_types == frozenset({"image", "video", "gif", "document"})

    def test_registered(self):
        assert isinstance(get_provider("slack", CREDS), SlackProvider)


class TestMrkdwn:
    def test_escapes_control_characters(self):
        assert to_mrkdwn("a < b & c > d") == "a &lt; b &amp; c &gt; d"

    def test_keeps_slack_tokens(self):
        text = "Hi <@U123> see <https://x.io|docs> and <#C1> <!here>"
        assert to_mrkdwn(text) == text


class TestOAuth:
    def test_authorize_url(self):
        url = provider().get_auth_url("https://app.example/cb/", "st")
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == "https://slack.com/oauth/v2/authorize"
        assert query["scope"] == ["chat:write,files:write,channels:read,groups:read"]
        assert query["client_id"] == ["app-1"]
        assert query["state"] == ["st"]
        assert query["redirect_uri"] == ["https://app.example/cb/"]

    def test_exchange(self):
        body = {
            "access_token": "xoxb-1",
            "scope": "chat:write,files:write,channels:read,groups:read",
            "team": {"id": "T1", "name": "Acme"},
        }
        p = provider()
        with patch.object(p, "_request", return_value=ok(body)) as request:
            tokens = p.exchange_code("code", "https://app.example/cb/")
        assert request.call_args.args[:2] == ("POST", "https://slack.com/api/oauth.v2.access")
        assert tokens.access_token == "xoxb-1"
        assert tokens.raw_response["team"]["name"] == "Acme"

    def test_a_failed_exchange_is_an_oauth_error(self):
        p = provider()
        with patch.object(p, "_request", return_value=failed("invalid_code")), pytest.raises(OAuthError):
            p.exchange_code("bad", "https://x/")

    def test_token_rotation_is_refused_because_channels_share_the_token(self):
        body = {
            "access_token": "xoxe-1",
            "refresh_token": "xoxe-r",
            "expires_in": 43200,
            "scope": "chat:write,files:write,channels:read,groups:read",
        }
        p = provider()
        with patch.object(p, "_request", return_value=ok(body)), pytest.raises(OAuthError, match="rotation"):
            p.exchange_code("c", "https://x/")

    def test_a_missing_scope_is_named(self):
        p = provider()
        with (
            patch.object(p, "_request", return_value=ok({"access_token": "x", "scope": "chat:write"})),
            pytest.raises(OAuthError, match="files:write"),
        ):
            p.exchange_code("c", "https://x/")

    def test_destinations_are_the_member_channels_across_pages(self):
        pages = [
            {
                "channels": [
                    {"id": "C1", "name": "general", "is_member": True},
                    {"id": "C2", "name": "random", "is_member": False},
                ],
                "response_metadata": {"next_cursor": "NEXT"},
            },
            {
                "channels": [
                    {"id": "G1", "name": "secret", "is_member": True, "is_private": True},
                    {"id": "C3", "name": "old", "is_member": True, "is_archived": True},
                ],
                "response_metadata": {"next_cursor": ""},
            },
        ]
        p = provider()
        with patch.object(p, "_request", side_effect=[ok(pages[0]), ok(pages[1])]) as request:
            rows = p.list_destinations(
                OAuthTokens(access_token="xoxb-1", raw_response={"team": {"id": "T1", "name": "Acme"}})
            )
        assert [row["id"] for row in rows] == ["C1", "G1"]
        assert rows[0] == {
            "id": "C1",
            "name": "#general",
            "handle": "Acme",
            "category": "Acme",
            "access_token": "xoxb-1",
            "page_id": "T1",
        }
        assert rows[1]["category"] == "Private channel"
        assert request.call_args_list[1].kwargs["params"]["cursor"] == "NEXT"
        assert request.call_args_list[0].kwargs["params"]["types"] == "public_channel,private_channel"


class TestProfile:
    def test_healthy_channel(self):
        request, _ = slack_api(
            {
                "conversations.info": ok({"channel": {"id": "C1", "name": "general", "is_member": True}}),
                "auth.test": ok({"team": "Acme"}),
            }
        )
        p = provider()
        with patch.object(p, "_request", side_effect=request):
            profile = p.get_profile("xoxb-1")
        assert (profile.platform_id, profile.name, profile.handle) == ("C1", "#general", "Acme")

    @pytest.mark.parametrize(
        ("channel", "fragment"),
        [
            ({"id": "C1", "name": "x", "is_archived": True}, "archived"),
            ({"id": "C1", "name": "x", "is_member": False}, "isn't in this channel"),
        ],
    )
    def test_an_archived_or_left_channel_fails_the_check(self, channel, fragment):
        request, _ = slack_api({"conversations.info": ok({"channel": channel})})
        p = provider()
        with patch.object(p, "_request", side_effect=request), pytest.raises(PublishError, match=fragment):
            p.get_profile("xoxb-1")

    def test_a_revoked_token_asks_for_a_reconnect(self):
        request, _ = slack_api({"conversations.info": failed("token_revoked")})
        p = provider()
        with patch.object(p, "_request", side_effect=request), pytest.raises(TokenExpiredError) as exc:
            p.get_profile("xoxb-1")
        assert exc.value.status_code == 401
        assert exc.value.retryable is False


class TestPublish:
    def test_text_post_returns_the_timestamp_and_permalink(self):
        request, calls = slack_api(
            {
                "chat.postMessage": ok({"ts": "1700000000.000100"}),
                "chat.getPermalink": ok({"permalink": "https://acme.slack.com/archives/C1/p1700000000000100"}),
            }
        )
        p = provider()
        with patch.object(p, "_request", side_effect=request):
            result = p.publish_post("xoxb-1", PublishContent(text="Hello <b> & *team*"))
        body = calls[0][2]["json"]
        assert body == {"channel": "C1", "text": "Hello &lt;b&gt; &amp; *team*", "mrkdwn": True}
        assert calls[0][2]["access_token"] == "xoxb-1"
        assert result.platform_post_id == "1700000000.000100"
        assert result.url == "https://acme.slack.com/archives/C1/p1700000000000100"

    def test_a_missing_permalink_does_not_fail_the_post(self):
        request, _ = slack_api(
            {"chat.postMessage": ok({"ts": "1.1"}), "chat.getPermalink": failed("message_not_found")}
        )
        p = provider()
        with patch.object(p, "_request", side_effect=request):
            result = p.publish_post("t", PublishContent(text="x"))
        assert result.url is None

    def test_no_unfurl(self):
        request, calls = slack_api({"chat.postMessage": ok({"ts": "1.1"}), "chat.getPermalink": ok({})})
        p = provider()
        with patch.object(p, "_request", side_effect=request):
            p.publish_post("t", PublishContent(text="x", extra={"no_unfurl": True}))
        assert calls[0][2]["json"]["unfurl_links"] is False

    def test_files_use_the_external_upload_flow_into_the_messages_thread(self, tmp_path):
        paths = []
        for name, data in (("a.png", b"123"), ("b.mp4", b"12345")):
            path = tmp_path / name
            path.write_bytes(data)
            paths.append(str(path))
        ids = iter(["F1", "F2"])
        request, calls = slack_api(
            {
                "chat.postMessage": ok({"ts": "9.9"}),
                "files.getUploadURLExternal": lambda kw: ok(
                    {"upload_url": "https://files.slack.com/upload/v1/abc", "file_id": next(ids)}
                ),
                "files.completeUploadExternal": ok({"files": [{"id": "F1"}, {"id": "F2"}]}),
                "chat.getPermalink": ok({"permalink": "https://p"}),
            }
        )
        p = provider()
        with patch.object(p, "_request", side_effect=request):
            result = p.publish_post("t", PublishContent(text="pics", media_files=paths))
        names = [name for _, name, _ in calls]
        # The bytes go up (privately) before the message, so a refused file fails before anything is visible.
        assert names == [
            "files.getUploadURLExternal",
            "abc",
            "files.getUploadURLExternal",
            "abc",
            "chat.postMessage",
            "files.completeUploadExternal",
            "chat.getPermalink",
        ]
        first_ticket = calls[0][2]["data"]
        assert first_ticket == {"filename": "a.png", "length": "3"}
        assert calls[2][2]["data"] == {"filename": "b.mp4", "length": "5"}
        assert calls[1][2]["headers"] == {"Content-Type": "application/octet-stream"}
        complete = calls[5][2]["json"]
        assert complete == {
            "files": [{"id": "F1", "title": "a.png"}, {"id": "F2", "title": "b.mp4"}],
            "channel_id": "C1",
            "thread_ts": "9.9",
        }
        assert result.platform_post_id == "9.9"

    def test_an_upload_address_outside_slack_is_refused(self, tmp_path):
        path = tmp_path / "a.png"
        path.write_bytes(b"1")
        request, calls = slack_api(
            {
                "chat.postMessage": ok({"ts": "9.9"}),
                "files.getUploadURLExternal": ok({"upload_url": "https://evil.example/u", "file_id": "F1"}),
            }
        )
        p = provider()
        with patch.object(p, "_request", side_effect=request), pytest.raises(PublishError):
            p.publish_post("t", PublishContent(text="x", media_files=[str(path)]))
        assert all(name != "u" for _, name, _ in calls)

    def test_only_files_post_the_first_file_name_as_the_message(self, tmp_path):
        path = tmp_path / "chart.png"
        path.write_bytes(b"1")
        request, calls = slack_api(
            {
                "chat.postMessage": ok({"ts": "9.9"}),
                "files.getUploadURLExternal": ok({"upload_url": "https://files.slack.com/u", "file_id": "F1"}),
                "files.completeUploadExternal": ok({}),
                "chat.getPermalink": ok({}),
            }
        )
        p = provider()
        with patch.object(p, "_request", side_effect=request):
            p.publish_post("t", PublishContent(text="", media_files=[str(path)]))
        post = next(kw for _, name, kw in calls if name == "chat.postMessage")
        assert post["json"]["text"] == "chart.png"

    def test_a_refused_file_fails_before_the_message_is_posted(self, tmp_path):
        path = tmp_path / "a.png"
        path.write_bytes(b"1")
        request, calls = slack_api({"files.getUploadURLExternal": failed("file_upload_size_restricted")})
        p = provider()
        with patch.object(p, "_request", side_effect=request), pytest.raises(PublishError):
            p.publish_post("t", PublishContent(text="x", media_files=[str(path)]))
        assert all(name != "chat.postMessage" for _, name, _ in calls)

    def test_files_failing_after_the_message_is_live_are_never_retried(self, tmp_path):
        path = tmp_path / "a.png"
        path.write_bytes(b"1")
        request, _ = slack_api(
            {
                "chat.postMessage": ok({"ts": "9.9"}),
                "files.getUploadURLExternal": ok({"upload_url": "https://files.slack.com/u", "file_id": "F1"}),
                "files.completeUploadExternal": failed("ratelimited"),
            }
        )
        p = provider()
        with patch.object(p, "_request", side_effect=request), pytest.raises(APIError) as exc:
            p.publish_post("t", PublishContent(text="x", media_files=[str(path)]))
        assert exc.value.ambiguous_write is True

    def test_over_the_text_limit_is_refused(self):
        with pytest.raises(PublishError) as exc:
            provider().publish_post("t", PublishContent(text="x" * 4001))
        assert exc.value.retryable is False

    def test_nothing_to_post_is_refused(self):
        with pytest.raises(PublishError):
            provider().publish_post("t", PublishContent(text=""))

    def test_a_thread_part_replies_in_the_first_messages_thread(self):
        request, calls = slack_api({"chat.postMessage": ok({"ts": "2.2"}), "chat.getPermalink": ok({})})
        p = provider()
        with patch.object(p, "_request", side_effect=request):
            result = p.publish_thread_part(
                "t",
                PublishContent(text="two"),
                root=PublishResult(platform_post_id="1.1"),
                parent=PublishResult(platform_post_id="1.5"),
            )
        assert calls[0][2]["json"]["thread_ts"] == "1.1"
        assert result.platform_post_id == "2.2"

    def test_first_comment_is_a_thread_reply(self):
        request, calls = slack_api({"chat.postMessage": ok({"ts": "3.3"})})
        p = provider()
        with patch.object(p, "_request", side_effect=request):
            comment = p.publish_comment("t", "1.1", "first")
        assert comment.platform_comment_id == "3.3"
        assert calls[0][2]["json"]["thread_ts"] == "1.1"

    def test_delete(self):
        request, calls = slack_api({"chat.delete": ok({})})
        p = provider()
        with patch.object(p, "_request", side_effect=request):
            assert p.delete_post("t", "1.1") is True
        assert calls[0][2]["json"] == {"channel": "C1", "ts": "1.1"}


class TestErrorClassification:
    def publish_failing_with(self, code):
        request, _ = slack_api({"chat.postMessage": failed(code)})
        p = provider()
        with patch.object(p, "_request", side_effect=request):
            p.publish_post("t", PublishContent(text="x"))

    @pytest.mark.parametrize("code", ["invalid_auth", "token_revoked", "account_inactive"])
    def test_credential_failures_are_a_permanent_reconnect(self, code):
        with pytest.raises(TokenExpiredError) as exc:
            self.publish_failing_with(code)
        assert exc.value.retryable is False and exc.value.status_code == 401

    @pytest.mark.parametrize(
        ("code", "fragment"),
        [
            ("channel_not_found", "can't find"),
            ("not_in_channel", "/invite"),
            ("is_archived", "archived"),
            ("missing_scope", "Reconnect"),
        ],
    )
    def test_known_refusals_are_permanent_sentences(self, code, fragment):
        with pytest.raises(PublishError, match=fragment) as exc:
            self.publish_failing_with(code)
        assert exc.value.retryable is False

    def test_an_unknown_refusal_is_a_permanent_api_error(self):
        with pytest.raises(APIError) as exc:
            self.publish_failing_with("invalid_blocks")
        assert exc.value.retryable is False and exc.value.ambiguous_write is False

    def test_a_server_failure_on_a_write_is_ambiguous(self):
        with pytest.raises(APIError) as exc:
            self.publish_failing_with("internal_error")
        assert exc.value.ambiguous_write is True

    def test_http_429_is_a_rate_limit_with_the_wait(self):
        response = httpx.Response(
            429, headers={"Retry-After": "30"}, request=httpx.Request("POST", "https://slack.com/api/x")
        )
        err = provider()._error_for_response(response)
        assert isinstance(err, RateLimitError) and err.retry_after == 30

    def test_http_4xx_is_permanent(self):
        response = httpx.Response(403, request=httpx.Request("POST", "https://slack.com/api/x"))
        assert provider()._error_for_response(response).retryable is False

    def test_a_timeout_on_send_is_an_ambiguous_write(self):
        p = provider()
        with patch("providers.base.httpx.Client") as client, pytest.raises(APIError) as exc:
            client.return_value.__enter__.return_value.request.side_effect = httpx.ReadTimeout("slow")
            p.publish_post("t", PublishContent(text="x"))
        assert exc.value.ambiguous_write is True


def test_post_settings_validate():
    assert SlackProvider.validate_post_settings({"no_unfurl": True}) == ""
    assert json.dumps(SlackProvider.post_settings[0].as_dict())
