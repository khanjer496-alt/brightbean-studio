"""Telegram Bot API provider, with every HTTP call mocked."""

import logging
from unittest.mock import MagicMock, patch

import httpx
import pytest

from providers import get_provider
from providers.exceptions import APIError, PublishError, RateLimitError
from providers.telegram import TelegramProvider, _RedactBotToken, to_telegram_html
from providers.types import AuthType, PostType, PublishContent, PublishResult, ThreadMode

CHANNEL = {
    "id": -1001234567890,
    "type": "channel",
    "title": "News",
    "username": "newsroom",
    "description": "Daily news. verify-0a1b2c3d4e",
}
ADMIN = {"status": "administrator", "can_post_messages": True}


def provider(**credentials) -> TelegramProvider:
    return TelegramProvider({"bot_token": "123:ABC", **credentials})


def ok(result, status=200):
    return MagicMock(status_code=status, json=MagicMock(return_value={"ok": True, "result": result}))


def fake_api(routes):
    """A ``_request`` stand-in answering by Bot API method name."""
    calls = []

    def request(method, url, **kwargs):
        name = url.rsplit("/", 1)[-1]
        calls.append((name, kwargs))
        result = routes[name]
        return ok(result(kwargs) if callable(result) else result)

    return request, calls


def error_response(status, body):
    return httpx.Response(status, json=body, request=httpx.Request("POST", "https://api.telegram.org/bot1/x"))


class TestDeclarations:
    def test_bot_token_auth_with_no_oauth(self):
        p = provider()
        assert p.auth_type is AuthType.BOT_TOKEN
        assert p.required_scopes == []
        with pytest.raises(NotImplementedError):
            p.get_auth_url("https://x/cb", "state")
        assert TelegramProvider.connect_url_name == "connect_telegram"
        assert TelegramProvider.supports_connection_link is False

    def test_limits_and_thread_mode(self):
        assert provider().max_caption_length == 4096
        assert TelegramProvider.thread.mode is ThreadMode.REPLY_CHAIN
        assert TelegramProvider.thread.media_per_part

    def test_media_rules_cap_albums_and_file_sizes(self):
        for post_type in (PostType.IMAGE, PostType.VIDEO):
            rules = TelegramProvider.media_rules[post_type]
            assert rules.max_items == 10
            assert rules.max_image_bytes == 10 * 1024 * 1024
            assert rules.max_video_bytes == 50 * 1024 * 1024
            assert "aspect_ratio" in rules.advisory

    def test_registered(self):
        assert isinstance(get_provider("telegram", {"bot_token": "x"}), TelegramProvider)


class TestChatReference:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("@newsroom", "@newsroom"),
            ("newsroom", "@newsroom"),
            ("https://t.me/newsroom", "@newsroom"),
            ("t.me/newsroom/", "@newsroom"),
            ("-1001234567890", "-1001234567890"),
            ("https://t.me/+AbCdEf123", ""),
            ("a b", ""),
            ("", ""),
        ],
    )
    def test_normalize(self, raw, expected):
        assert TelegramProvider.normalize_chat_reference(raw) == expected


class TestHtml:
    def test_escapes_what_telegram_cannot_parse(self):
        assert to_telegram_html("1 < 2 & 3 > 2") == "1 &lt; 2 &amp; 3 &gt; 2"

    def test_keeps_balanced_supported_tags(self):
        assert to_telegram_html('<b>bold</b> and <a href="https://x.io">link</a>') == (
            '<b>bold</b> and <a href="https://x.io">link</a>'
        )

    def test_unbalanced_tags_are_escaped_instead_of_failing_the_post(self):
        assert to_telegram_html("<b>oops") == "&lt;b&gt;oops"

    def test_unsupported_tags_are_escaped(self):
        assert to_telegram_html("<script>x</script>") == "&lt;script&gt;x&lt;/script&gt;"


    def test_an_ampersand_in_a_link_is_escaped(self):
        assert (
            to_telegram_html('<a href="https://x.io/?a=1&b=2">x</a>')
            == '<a href="https://x.io/?a=1&amp;b=2">x</a>'
        )


class TestConnect:
    def test_verifies_the_bot_can_post_to_a_channel(self):
        request, calls = fake_api(
            {
                "getChat": CHANNEL,
                "getMe": {"id": 99, "username": "pd_bot"},
                "getChatMember": ADMIN,
                "getChatMemberCount": 42,
            }
        )
        p = provider()
        with patch.object(p, "_request", side_effect=request):
            profile = p.get_profile("@newsroom")
        assert profile.platform_id == "-1001234567890"
        assert profile.name == "News"
        assert profile.handle == "@newsroom"
        assert profile.follower_count == 42
        # Where connect_telegram looks for the user's proof of control.
        assert profile.extra["description"] == "Daily news. verify-0a1b2c3d4e"
        member_call = next(kw for name, kw in calls if name == "getChatMember")
        assert member_call["json"] == {"chat_id": -1001234567890, "user_id": 99}

    def test_bot_username_for_the_instructions(self):
        request, _ = fake_api({"getMe": {"id": 99, "username": "pd_bot"}})
        p = provider()
        with patch.object(p, "_request", side_effect=request):
            assert p.get_bot_username() == "pd_bot"

    @pytest.mark.parametrize(
        ("chat", "member", "fragment"),
        [
            (CHANNEL, {"status": "member"}, "administrator"),
            (CHANNEL, {"status": "left"}, "administrator"),
            (CHANNEL, {"status": "administrator", "can_post_messages": False}, "Post Messages"),
            ({"id": 5, "type": "private", "first_name": "Ann"}, {"status": "member"}, "channels and groups"),
        ],
    )
    def test_refuses_a_chat_the_bot_cannot_post_to(self, chat, member, fragment):
        request, _ = fake_api({"getChat": chat, "getMe": {"id": 99}, "getChatMember": member})
        p = provider()
        with patch.object(p, "_request", side_effect=request), pytest.raises(PublishError, match=fragment) as exc:
            p.get_profile("@x_chat")
        assert exc.value.retryable is False

    def test_a_plain_member_may_post_to_a_group(self):
        group = {"id": -555, "type": "supergroup", "title": "Crew"}
        request, _ = fake_api(
            {"getChat": group, "getMe": {"id": 99}, "getChatMember": {"status": "member"}, "getChatMemberCount": 3}
        )
        p = provider()
        with patch.object(p, "_request", side_effect=request):
            assert p.get_profile("-555").handle is None

    def test_a_missing_member_count_does_not_fail_the_connect(self):
        def boom(_):
            raise APIError("nope", status_code=400)

        request, _ = fake_api(
            {"getChat": CHANNEL, "getMe": {"id": 99}, "getChatMember": ADMIN, "getChatMemberCount": boom}
        )
        p = provider()
        with patch.object(p, "_request", side_effect=request):
            assert p.get_profile("@newsroom").follower_count == 0

    def test_no_bot_token_is_a_permanent_failure(self):
        with pytest.raises(PublishError) as exc:
            TelegramProvider({}).get_profile("@x_chat")
        assert exc.value.retryable is False


class TestPublish:
    def test_a_follow_up_failing_after_the_media_is_live_is_never_retried(self, tmp_path):
        photo = tmp_path / "a.jpg"
        photo.write_bytes(b"1")

        def request(method, url, **kwargs):
            if url.endswith("/sendPhoto"):
                return ok({"message_id": 5})
            raise RateLimitError("slow down", retry_after=3)

        p = provider()
        with patch.object(p, "_request", side_effect=request), pytest.raises(APIError) as exc:
            p.publish_post(
                "-100", PublishContent(text="x" * 1500, media_files=[str(photo)], media_types=["image"])
            )
        assert exc.value.ambiguous_write is True

    def test_text_goes_through_send_message_as_html(self):
        request, calls = fake_api({"sendMessage": {"message_id": 7}})
        p = provider(chat_username="newsroom")
        with patch.object(p, "_request", side_effect=request):
            result = p.publish_post("-1001234567890", PublishContent(text="Hi <b>there</b> & you"))
        name, kwargs = calls[0]
        assert name == "sendMessage"
        assert kwargs["json"] == {
            "chat_id": "-1001234567890",
            "text": "Hi <b>there</b> &amp; you",
            "parse_mode": "HTML",
        }
        assert result.platform_post_id == "7"
        assert result.url == "https://t.me/newsroom/7"
        assert result.extra == {"chat_id": "-1001234567890", "message_ids": [7]}

    def test_private_supergroup_links_through_t_me_c(self):
        request, _ = fake_api({"sendMessage": {"message_id": 9}})
        p = provider()
        with patch.object(p, "_request", side_effect=request):
            result = p.publish_post("-1001234567890", PublishContent(text="x"))
        assert result.url == "https://t.me/c/1234567890/9"

    def test_silent_option_disables_the_notification(self):
        request, calls = fake_api({"sendMessage": {"message_id": 1}})
        p = provider()
        with patch.object(p, "_request", side_effect=request):
            p.publish_post("1", PublishContent(text="x", extra={"silent": True}))
        assert calls[0][1]["json"]["disable_notification"] is True

    def test_a_photo_carries_the_text_as_its_caption(self, tmp_path):
        photo = tmp_path / "a.jpg"
        photo.write_bytes(b"jpg")
        request, calls = fake_api({"sendPhoto": {"message_id": 3}})
        p = provider()
        with patch.object(p, "_request", side_effect=request):
            result = p.publish_post(
                "55",
                PublishContent(text="Look", media_files=[str(photo)], media_types=["image"], post_type=PostType.IMAGE),
            )
        name, kwargs = calls[0]
        assert name == "sendPhoto"
        assert kwargs["data"] == {"chat_id": "55", "caption": "Look", "parse_mode": "HTML"}
        assert list(kwargs["files"]) == ["photo"]
        assert result.platform_post_id == "3"

    def test_a_video_is_sent_streamable(self, tmp_path):
        video = tmp_path / "a.mp4"
        video.write_bytes(b"mp4")
        request, calls = fake_api({"sendVideo": {"message_id": 4}})
        p = provider()
        with patch.object(p, "_request", side_effect=request):
            p.publish_post("55", PublishContent(text="", media_files=[str(video)], media_types=["video"]))
        name, kwargs = calls[0]
        assert name == "sendVideo"
        assert kwargs["data"]["supports_streaming"] == "true"
        assert "caption" not in kwargs["data"]

    def test_text_over_the_caption_limit_follows_the_media_as_a_reply(self, tmp_path):
        photo = tmp_path / "a.jpg"
        photo.write_bytes(b"jpg")
        request, calls = fake_api({"sendPhoto": {"message_id": 10}, "sendMessage": {"message_id": 11}})
        p = provider()
        with patch.object(p, "_request", side_effect=request):
            result = p.publish_post(
                "55", PublishContent(text="x" * 1500, media_files=[str(photo)], media_types=["image"])
            )
        assert [name for name, _ in calls] == ["sendPhoto", "sendMessage"]
        assert "caption" not in calls[0][1]["data"]
        assert calls[1][1]["json"]["reply_parameters"] == {"message_id": 10}
        assert result.platform_post_id == "10"
        assert result.extra["message_ids"] == [10, 11]

    def test_several_files_go_as_one_album_with_the_caption_on_the_first(self, tmp_path):
        paths = []
        for name in ("a.jpg", "b.mp4"):
            path = tmp_path / name
            path.write_bytes(b"x")
            paths.append(str(path))
        request, calls = fake_api({"sendMediaGroup": [{"message_id": 20}, {"message_id": 21}]})
        p = provider()
        with patch.object(p, "_request", side_effect=request):
            result = p.publish_post(
                "55", PublishContent(text="Album", media_files=paths, media_types=["image", "video"])
            )
        name, kwargs = calls[0]
        assert name == "sendMediaGroup"
        import json

        group = json.loads(kwargs["data"]["media"])
        assert [item["type"] for item in group] == ["photo", "video"]
        assert [item["media"] for item in group] == ["attach://file0", "attach://file1"]
        assert group[0]["caption"] == "Album"
        assert "caption" not in group[1]
        assert set(kwargs["files"]) == {"file0", "file1"}
        assert result.extra["message_ids"] == [20, 21]

    def test_more_than_ten_items_is_refused_without_a_request(self, tmp_path):
        p = provider()
        with patch.object(p, "_request", side_effect=AssertionError("no request")), pytest.raises(PublishError):
            p.publish_post("1", PublishContent(text="x", media_files=["f"] * 11, media_types=["image"] * 11))

    def test_a_gif_is_refused_permanently(self, tmp_path):
        p = provider()
        with pytest.raises(PublishError) as exc:
            p.publish_post("1", PublishContent(text="x", media_files=["a.gif"], media_types=["gif"]))
        assert exc.value.retryable is False

    def test_nothing_to_post_is_refused(self):
        with pytest.raises(PublishError) as exc:
            provider().publish_post("1", PublishContent(text="  "))
        assert exc.value.retryable is False


class TestThreadsAndComments:
    def test_a_part_replies_to_the_part_before_it(self):
        request, calls = fake_api({"sendMessage": {"message_id": 31}})
        p = provider()
        root = PublishResult(platform_post_id="30")
        parent = PublishResult(platform_post_id="30")
        with patch.object(p, "_request", side_effect=request):
            result = p.publish_thread_part("5", PublishContent(text="two"), root=root, parent=parent)
        assert calls[0][1]["json"]["reply_parameters"] == {"message_id": 30, "allow_sending_without_reply": False}
        assert result.platform_post_id == "31"

    def test_first_comment_is_a_reply(self):
        request, calls = fake_api({"sendMessage": {"message_id": 41}})
        p = provider()
        with patch.object(p, "_request", side_effect=request):
            comment = p.publish_comment("5", "40", "first!")
        assert comment.platform_comment_id == "41"
        assert calls[0][1]["json"]["reply_parameters"] == {"message_id": 40}

    def test_delete(self):
        request, calls = fake_api({"deleteMessage": True})
        p = provider()
        with patch.object(p, "_request", side_effect=request):
            assert p.delete_post("5", "40") is True
        assert calls[0][1]["json"] == {"chat_id": "5", "message_id": 40}


class TestErrorClassification:
    def test_flood_control_is_a_rate_limit_with_telegrams_wait(self):
        err = provider()._error_for_response(
            error_response(
                429,
                {"ok": False, "error_code": 429, "description": "Too Many Requests", "parameters": {"retry_after": 17}},
            )
        )
        assert isinstance(err, RateLimitError)
        assert err.retry_after == 17

    @pytest.mark.parametrize("status", [400, 401, 403, 404])
    def test_a_client_error_is_permanent(self, status):
        err = provider()._error_for_response(error_response(status, {"ok": False, "description": "Bad Request: x"}))
        assert isinstance(err, APIError)
        assert err.retryable is False
        assert err.status_code == status

    def test_a_server_error_is_retryable(self):
        err = provider()._error_for_response(error_response(502, {"ok": False, "description": "Bad Gateway"}))
        assert err.retryable is True

    def test_a_migrated_group_is_permanent(self):
        err = provider()._error_for_response(
            error_response(
                400,
                {"ok": False, "description": "group chat was upgraded", "parameters": {"migrate_to_chat_id": -100123}},
            )
        )
        assert err.retryable is False
        assert "migrated" in str(err)

    def test_known_refusals_become_a_sentence_for_the_user(self):
        p = provider()
        body = {"ok": False, "error_code": 403, "description": "Forbidden: bot was kicked from the channel chat"}
        with (
            patch.object(p, "_request", side_effect=p._error_for_response(error_response(403, body))),
            pytest.raises(PublishError) as exc,
        ):
            p.publish_post("5", PublishContent(text="x"))
        assert exc.value.retryable is False
        assert "removed from this chat" in str(exc.value)

    def test_a_timeout_after_a_send_is_an_ambiguous_write(self):
        p = provider()
        with (
            patch("providers.base.httpx.Client") as client,
            pytest.raises(APIError) as exc,
        ):
            client.return_value.__enter__.return_value.request.side_effect = httpx.ReadTimeout("slow")
            p.publish_post("5", PublishContent(text="x"))
        assert exc.value.ambiguous_write is True
        assert "123:ABC" not in str(exc.value)

    def test_a_5xx_after_a_send_is_ambiguous(self):
        p = provider()
        response = error_response(502, {"ok": False, "description": "Bad Gateway"})
        with patch("providers.base.httpx.Client") as client, pytest.raises(APIError) as exc:
            client.return_value.__enter__.return_value.request.return_value = response
            p.publish_post("5", PublishContent(text="x"))
        assert exc.value.ambiguous_write is True


class TestSettings:
    def test_silent_is_optional(self):
        assert TelegramProvider.validate_post_settings({}) == ""
        assert TelegramProvider.post_settings[0].kind == "checkbox"


def test_bot_token_is_redacted_from_request_logs():
    record = logging.LogRecord(
        "httpx",
        logging.INFO,
        "",
        0,
        'HTTP Request: %s %s "%s"',
        ("POST", "https://api.telegram.org/bot123:ABC/sendMessage", "200"),
        None,
    )
    assert _RedactBotToken().filter(record)
    assert "123:ABC" not in record.getMessage()
    assert "/bot<redacted>/sendMessage" in record.getMessage()
