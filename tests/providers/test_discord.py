"""Discord provider (OAuth2 bot install, bot-token posting), with every HTTP call mocked."""

import json
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from providers import get_provider
from providers.discord import BOT_PERMISSIONS, DiscordProvider
from providers.exceptions import APIError, OAuthError, PublishError, RateLimitError
from providers.types import AuthType, OAuthTokens, PostType, PublishContent, PublishResult, ThreadMode

CREDS = {"client_id": "app-1", "client_secret": "shh", "bot_token": "bot-token"}


def provider(**extra) -> DiscordProvider:
    return DiscordProvider({**CREDS, **extra})


def ok(body, status=200):
    return MagicMock(status_code=status, json=MagicMock(return_value=body))


def error_response(status, body, headers=None):
    return httpx.Response(
        status, json=body, headers=headers, request=httpx.Request("POST", "https://discord.com/api/v10/x")
    )


class TestDeclarations:
    def test_oauth_with_destination_picker(self):
        p = provider()
        assert p.auth_type is AuthType.OAUTH2
        assert p.required_scopes == ["bot", "guilds"]
        assert DiscordProvider.has_destination_picker is True
        assert DiscordProvider.supports_connection_link is False

    def test_limits_and_threads(self):
        assert provider().max_caption_length == 2000
        assert DiscordProvider.thread.mode is ThreadMode.REPLY_CHAIN
        assert DiscordProvider.media_rules[PostType.IMAGE].max_items == 10
        assert DiscordProvider.media_rules[PostType.IMAGE].max_image_bytes == 25 * 1024 * 1024
        assert "max_image_bytes" in DiscordProvider.media_rules[PostType.IMAGE].advisory

    def test_registered(self):
        assert isinstance(get_provider("discord", CREDS), DiscordProvider)

    def test_permissions_cover_what_posting_needs(self):
        view, send, embed, attach, history = 1 << 10, 1 << 11, 1 << 14, 1 << 15, 1 << 16
        assert view | send | embed | attach | history == BOT_PERMISSIONS


class TestOAuth:
    def test_authorize_url_asks_for_the_bot_with_permissions(self):
        url = provider().get_auth_url("https://app.example/cb/", "state-1")
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == "https://discord.com/oauth2/authorize"
        assert query["client_id"] == ["app-1"]
        assert query["scope"] == ["bot guilds"]
        assert query["permissions"] == [str(BOT_PERMISSIONS)]
        assert query["response_type"] == ["code"]
        assert query["redirect_uri"] == ["https://app.example/cb/"]
        assert query["state"] == ["state-1"]

    def test_exchange_keeps_the_guild(self):
        body = {
            "access_token": "user-at",
            "refresh_token": "user-rt",
            "expires_in": 604800,
            "scope": "bot guilds",
            "guild": {"id": "g1", "name": "My Server", "icon": "abc"},
        }
        p = provider()
        with patch.object(p, "_request", return_value=ok(body)) as request:
            tokens = p.exchange_code("the-code", "https://app.example/cb/")
        assert request.call_args.args[:2] == ("POST", "https://discord.com/api/oauth2/token")
        assert request.call_args.kwargs["data"]["grant_type"] == "authorization_code"
        assert request.call_args.kwargs["data"]["code"] == "the-code"
        assert tokens.raw_response["guild"]["id"] == "g1"

    def test_exchange_without_a_token_fails(self):
        p = provider()
        with patch.object(p, "_request", return_value=ok({"error": "invalid_grant"})), pytest.raises(OAuthError):
            p.exchange_code("c", "https://x/")

    def test_destinations_are_the_text_channels_of_the_installed_server(self):
        guild = {"id": "g1", "name": "My Server", "icon": "abc"}
        channels = [
            {"id": "c2", "name": "voice", "type": 2, "position": 0},
            {"id": "c3", "name": "news", "type": 5, "position": 2},
            {"id": "c1", "name": "general", "type": 0, "position": 1},
            {"id": "c4", "name": "category", "type": 4, "position": 3},
        ]
        p = provider()
        calls = []

        def request(method, url, **kwargs):
            calls.append((method, url, kwargs))
            return ok(channels if method == "GET" else {})

        with patch.object(p, "_request", side_effect=request):
            rows = p.list_destinations(OAuthTokens(access_token="user-at", raw_response={"guild": guild}))

        assert [row["id"] for row in rows] == ["c1", "c3"]
        assert rows[0] == {
            "id": "c1",
            "name": "#general",
            "handle": "My Server",
            "category": "My Server",
            "picture": "https://cdn.discordapp.com/icons/g1/abc.png",
            "access_token": "c1",
            "page_id": "g1",
        }
        get_call = calls[0]
        assert get_call[1] == "https://discord.com/api/v10/guilds/g1/channels"
        assert get_call[2]["headers"]["Authorization"] == "Bot bot-token"
        assert get_call[2]["headers"]["User-Agent"].startswith("DiscordBot (")
        # The user's own grant is revoked once the channels are listed.
        assert calls[-1][1].endswith("/oauth2/token/revoke")
        assert calls[-1][2]["data"]["token"] == "user-at"

    def test_no_guild_in_the_token_response_is_an_oauth_error(self):
        with pytest.raises(OAuthError):
            provider().list_destinations(OAuthTokens(access_token="at", raw_response={}))


class TestProfile:
    def test_a_visible_channel_is_healthy(self):
        p = provider()
        routes = {
            "/channels/c1": {"id": "c1", "name": "general", "guild_id": "g1"},
            "/guilds/g1": {"id": "g1", "name": "My Server", "icon": None},
        }
        with patch.object(p, "_request", side_effect=lambda m, url, **kw: ok(routes[url.split("/v10")[1]])):
            profile = p.get_profile("c1")
        assert (profile.platform_id, profile.name, profile.handle) == ("c1", "#general", "My Server")

    def test_a_channel_the_bot_lost_access_to_fails_the_check(self):
        p = provider()
        err = APIError("Missing Access", status_code=403, retryable=False, raw_response={"code": 50001})
        with patch.object(p, "_request", side_effect=err), pytest.raises(APIError):
            p.get_profile("c1")


class TestPublish:
    def test_text_message_with_safe_mentions(self):
        p = provider(guild_id="g1")
        with patch.object(p, "_request", return_value=ok({"id": "m1"})) as request:
            result = p.publish_post("c1", PublishContent(text="**Hello** @everyone"))
        assert request.call_args.args[:2] == ("POST", "https://discord.com/api/v10/channels/c1/messages")
        payload = request.call_args.kwargs["json"]
        assert payload["content"] == "**Hello** @everyone"
        assert payload["allowed_mentions"]["parse"] == ["users", "roles"]
        assert result.platform_post_id == "m1"
        assert result.url == "https://discord.com/channels/g1/c1/m1"

    def test_flags_for_embeds_and_notifications(self):
        p = provider()
        with patch.object(p, "_request", return_value=ok({"id": "m1"})) as request:
            p.publish_post("c1", PublishContent(text="x", extra={"suppress_embeds": True, "silent": True}))
        assert request.call_args.kwargs["json"]["flags"] == (1 << 2) | (1 << 12)

    def test_files_travel_as_multipart_with_payload_json(self, tmp_path):
        paths = []
        for name in ("a.png", "b.mp4"):
            path = tmp_path / name
            path.write_bytes(b"x")
            paths.append(str(path))
        p = provider()
        with patch.object(p, "_request", return_value=ok({"id": "m2"})) as request:
            p.publish_post("c1", PublishContent(text="pics", media_files=paths))
        kwargs = request.call_args.kwargs
        assert set(kwargs["files"]) == {"files[0]", "files[1]"}
        payload = json.loads(kwargs["data"]["payload_json"])
        assert payload["content"] == "pics"
        assert payload["attachments"] == [{"id": 0, "filename": "a.png"}, {"id": 1, "filename": "b.mp4"}]

    def test_over_2000_characters_is_refused_without_a_request(self):
        with pytest.raises(PublishError) as exc:
            provider().publish_post("c1", PublishContent(text="x" * 2001))
        assert exc.value.retryable is False

    def test_eleven_files_are_refused(self):
        with pytest.raises(PublishError):
            provider().publish_post("c1", PublishContent(text="x", media_files=["f"] * 11))

    def test_nothing_to_post_is_refused(self):
        with pytest.raises(PublishError):
            provider().publish_post("c1", PublishContent(text=""))

    def test_missing_bot_token_is_permanent(self):
        with pytest.raises(PublishError) as exc:
            DiscordProvider({}).publish_post("c1", PublishContent(text="x"))
        assert exc.value.retryable is False

    def test_a_thread_part_replies_to_the_previous_part(self):
        p = provider()
        with patch.object(p, "_request", return_value=ok({"id": "m3"})) as request:
            p.publish_thread_part(
                "c1",
                PublishContent(text="two"),
                root=PublishResult(platform_post_id="m1"),
                parent=PublishResult(platform_post_id="m2"),
            )
        assert request.call_args.kwargs["json"]["message_reference"] == {
            "message_id": "m2",
            "fail_if_not_exists": False,
        }

    def test_first_comment_is_a_reply(self):
        p = provider()
        with patch.object(p, "_request", return_value=ok({"id": "m4"})) as request:
            comment = p.publish_comment("c1", "m1", "first")
        assert comment.platform_comment_id == "m4"
        assert request.call_args.kwargs["json"]["message_reference"]["message_id"] == "m1"

    def test_delete(self):
        p = provider()
        with patch.object(p, "_request", return_value=MagicMock(status_code=204)) as request:
            assert p.delete_post("c1", "m1") is True
        assert request.call_args.args[:2] == ("DELETE", "https://discord.com/api/v10/channels/c1/messages/m1")


class TestErrorClassification:
    def test_429_reads_the_body_wait(self):
        err = provider()._error_for_response(
            error_response(429, {"message": "You are being rate limited.", "retry_after": 1.5, "global": False})
        )
        assert isinstance(err, RateLimitError)
        assert err.retry_after == 2

    def test_429_falls_back_to_the_header(self):
        err = provider()._error_for_response(error_response(429, {"message": "slow"}, {"Retry-After": "7"}))
        assert err.retry_after == 7

    @pytest.mark.parametrize("status", [400, 401, 403, 404])
    def test_client_errors_are_permanent(self, status):
        err = provider()._error_for_response(error_response(status, {"message": "nope", "code": 0}))
        assert err.retryable is False and err.status_code == status

    def test_server_errors_are_retryable(self):
        assert provider()._error_for_response(error_response(503, {"message": "down"})).retryable is True

    @pytest.mark.parametrize(
        ("code", "fragment"),
        [(50013, "Send Messages"), (50001, "can't see"), (10003, "channel any more"), (40005, "too large")],
    )
    def test_known_codes_become_a_sentence_for_the_user(self, code, fragment):
        p = provider()
        body = {"message": "x", "code": code}
        error = p._error_for_response(error_response(403, body))
        with patch.object(p, "_request", side_effect=error), pytest.raises(PublishError) as exc:
            p.publish_post("c1", PublishContent(text="x"))
        assert exc.value.retryable is False
        assert fragment in str(exc.value)

    def test_a_timeout_on_send_is_an_ambiguous_write(self):
        p = provider()
        with patch("providers.base.httpx.Client") as client, pytest.raises(APIError) as exc:
            client.return_value.__enter__.return_value.request.side_effect = httpx.ReadTimeout("slow")
            p.publish_post("c1", PublishContent(text="x"))
        assert exc.value.ambiguous_write is True

    def test_a_5xx_on_send_is_ambiguous(self):
        p = provider()
        with patch("providers.base.httpx.Client") as client, pytest.raises(APIError) as exc:
            client.return_value.__enter__.return_value.request.return_value = error_response(502, {"message": "bad"})
            p.publish_post("c1", PublishContent(text="x"))
        assert exc.value.ambiguous_write is True
