"""Twitch, Kick and Dribbble providers, with every HTTP call mocked."""

import base64
import hashlib
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest
from PIL import Image

from providers import get_provider, is_video_only, requires_media
from providers.dribbble import DribbbleProvider
from providers.exceptions import APIError, OAuthError, PublishError
from providers.kick import KickProvider
from providers.twitch import TwitchProvider
from providers.types import AuthType, PostType, PublishContent, PublishState

CREDS = {"client_id": "cid", "client_secret": "sec"}


def ok(body=None, status=200, headers=None):
    return MagicMock(status_code=status, json=MagicMock(return_value=body or {}), headers=headers or {})


class TestTwitch:
    def provider(self, **extra):
        return TwitchProvider({**CREDS, "user_id": "123", **extra})

    def test_declarations(self):
        p = self.provider()
        assert p.auth_type is AuthType.OAUTH2
        assert p.required_scopes == ["user:write:chat", "moderator:manage:announcements"]
        assert TwitchProvider.accepts_media is False
        assert p.max_caption_length == 500
        assert isinstance(get_provider("twitch", CREDS), TwitchProvider)
        assert TwitchProvider.credentials_for_account(SimpleNamespace(account_platform_id="9")) == {"user_id": "9"}

    def test_authorize_url(self):
        query = parse_qs(urlparse(self.provider().get_auth_url("https://app/cb/", "st")).query)
        assert query["scope"] == ["user:write:chat moderator:manage:announcements"]
        assert query["response_type"] == ["code"]

    def test_exchange_requires_every_scope(self):
        p = self.provider()
        body = {"access_token": "at", "refresh_token": "rt", "expires_in": 14000, "scope": ["user:write:chat"]}
        with patch.object(p, "_request", return_value=ok(body)), pytest.raises(OAuthError, match="announcements"):
            p.exchange_code("c", "https://app/cb/")
        body["scope"] = ["user:write:chat", "moderator:manage:announcements"]
        with patch.object(p, "_request", return_value=ok(body)) as request:
            tokens = p.exchange_code("c", "https://app/cb/")
        assert request.call_args.kwargs["data"]["grant_type"] == "authorization_code"
        assert (tokens.access_token, tokens.refresh_token, tokens.expires_in) == ("at", "rt", 14000)

    def test_refresh(self):
        p = self.provider()
        with patch.object(p, "_request", return_value=ok({"access_token": "new", "expires_in": 100})) as request:
            tokens = p.refresh_token("rt")
        assert request.call_args.kwargs["data"]["refresh_token"] == "rt"
        assert (tokens.access_token, tokens.refresh_token) == ("new", "rt")

    def test_profile_sends_the_client_id_header(self):
        p = self.provider()
        user = {"id": "123", "login": "streamer", "display_name": "Streamer", "profile_image_url": "https://i/x.png"}
        with patch.object(p, "_request", return_value=ok({"data": [user]})) as request:
            profile = p.get_profile("at")
        assert request.call_args.kwargs["headers"] == {"Client-Id": "cid"}
        assert (profile.platform_id, profile.handle, profile.name) == ("123", "streamer", "Streamer")

    def test_a_chat_message(self):
        p = self.provider()
        reply = ok({"data": [{"message_id": "m1", "is_sent": True}]})
        with patch.object(p, "_request", return_value=reply) as request:
            result = p.publish_post("at", PublishContent(text="going live"))
        assert request.call_args.args[:2] == ("POST", "https://api.twitch.tv/helix/chat/messages")
        assert request.call_args.kwargs["json"] == {
            "broadcaster_id": "123",
            "sender_id": "123",
            "message": "going live",
        }
        assert result.platform_post_id == "m1"

    def test_an_announcement_with_a_colour(self):
        p = self.provider()
        with patch.object(p, "_request", return_value=ok(status=204)) as request:
            result = p.publish_post("at", PublishContent(text="Live!", extra={"kind": "announcement", "color": "blue"}))
        assert request.call_args.args[:2] == ("POST", "https://api.twitch.tv/helix/chat/announcements")
        assert request.call_args.kwargs["params"] == {"broadcaster_id": "123", "moderator_id": "123"}
        assert request.call_args.kwargs["json"] == {"message": "Live!", "color": "blue"}
        assert result.platform_post_id == "announcement:123"

    def test_a_dropped_message_is_a_permanent_failure_with_the_reason(self):
        p = self.provider()
        reply = ok(
            {"data": [{"is_sent": False, "drop_reason": {"code": "x", "message": "Chat is in emote-only mode"}}]}
        )
        with patch.object(p, "_request", return_value=reply), pytest.raises(PublishError, match="emote-only") as exc:
            p.publish_post("at", PublishContent(text="hi"))
        assert exc.value.retryable is False

    @pytest.mark.parametrize("content", [PublishContent(), PublishContent(text="x" * 501)])
    def test_refusals_make_no_call(self, content):
        p = self.provider()
        with patch.object(p, "_request") as request, pytest.raises(PublishError):
            p.publish_post("at", content)
        request.assert_not_called()

    def test_a_colour_needs_an_announcement(self):
        assert TwitchProvider.validate_post_settings({"kind": "message", "color": "blue"}) != ""
        assert TwitchProvider.validate_post_settings({"kind": "announcement", "color": "blue"}) == ""
        assert TwitchProvider.validate_post_settings({"color": "pink", "kind": "announcement"}) != ""

    def test_an_account_without_a_channel_is_refused(self):
        with pytest.raises(PublishError):
            TwitchProvider(CREDS).publish_post("at", PublishContent(text="x"))


class TestKick:
    def provider(self, **extra):
        return KickProvider({**CREDS, "user_id": "42", **extra})

    def test_declarations(self):
        p = self.provider()
        assert p.auth_type is AuthType.OAUTH2 and p.uses_pkce is True and p.max_caption_length == 500
        assert isinstance(get_provider("kick", CREDS), KickProvider)

    def test_authorize_url_carries_an_s256_challenge(self):
        verifier = "v" * 60
        query = parse_qs(urlparse(self.provider().get_auth_url("https://app/cb/", "st", verifier)).query)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        assert query["code_challenge"] == [challenge] and query["code_challenge_method"] == ["S256"]
        assert query["scope"] == ["user:read chat:write"]
        with pytest.raises(OAuthError):
            self.provider().get_auth_url("https://app/cb/", "st")

    def test_exchange_replays_the_verifier(self):
        p = self.provider()
        body = {"access_token": "at", "refresh_token": "rt", "expires_in": 3600, "scope": "user:read chat:write"}
        with patch.object(p, "_request", return_value=ok(body)) as request:
            tokens = p.exchange_code("code", "https://app/cb/", code_verifier="ver")
        assert request.call_args.args[:2] == ("POST", "https://id.kick.com/oauth/token")
        assert request.call_args.kwargs["data"]["code_verifier"] == "ver"
        assert tokens.refresh_token == "rt"

    def test_a_grant_without_chat_write_is_refused(self):
        p = self.provider()
        with (
            patch.object(p, "_request", return_value=ok({"access_token": "at", "scope": "user:read"})),
            pytest.raises(OAuthError),
        ):
            p.exchange_code("code", "https://app/cb/", code_verifier="ver")

    def test_refresh_rotates_the_refresh_token(self):
        p = self.provider()
        with patch.object(p, "_request", return_value=ok({"access_token": "n", "refresh_token": "r2"})):
            assert p.refresh_token("r1").refresh_token == "r2"

    def test_profile_does_not_keep_the_email(self):
        p = self.provider()
        user = {"user_id": 42, "name": "streamer", "email": "a@b.c", "profile_picture": "https://i/x.png"}
        with patch.object(p, "_request", return_value=ok({"data": [user]})):
            profile = p.get_profile("at")
        assert (profile.platform_id, profile.handle) == ("42", "streamer")
        assert "a@b.c" not in repr(profile)

    def test_a_chat_message(self):
        p = self.provider()
        reply = ok({"data": {"is_sent": True, "message_id": "uuid-1"}})
        with patch.object(p, "_request", return_value=reply) as request:
            result = p.publish_post("at", PublishContent(text="live now"))
        assert request.call_args.kwargs["json"] == {"content": "live now", "type": "user", "broadcaster_user_id": 42}
        assert result.platform_post_id == "uuid-1"

    @pytest.mark.parametrize(
        "content", [PublishContent(), PublishContent(text="x" * 501), PublishContent(text="é" * 500 + "é")]
    )
    def test_refusals_make_no_call(self, content):
        p = self.provider()
        with patch.object(p, "_request") as request, pytest.raises(PublishError):
            p.publish_post("at", content)
        request.assert_not_called()

    def test_not_sent_is_permanent_and_missing_id_is_ambiguous(self):
        p = self.provider()
        with patch.object(p, "_request", return_value=ok({"data": {"is_sent": False}})), pytest.raises(PublishError):
            p.publish_post("at", PublishContent(text="x"))
        with patch.object(p, "_request", return_value=ok({"data": {}})), pytest.raises(APIError) as exc:
            p.publish_post("at", PublishContent(text="x"))
        assert exc.value.ambiguous_write is True


class TestDribbble:
    def provider(self):
        return DribbbleProvider(CREDS)

    @pytest.fixture
    def image(self, tmp_path):
        def make(size=(800, 600), name="shot.png"):
            path = tmp_path / name
            Image.new("RGB", size, "red").save(path)
            return str(path)

        return make

    def test_declarations(self):
        p = self.provider()
        assert p.auth_type is AuthType.OAUTH2 and p.publish_is_async is True
        assert p.supported_post_types == [PostType.IMAGE]
        assert requires_media("dribbble") is True and is_video_only("dribbble") is False
        rules = DribbbleProvider.media_rules[PostType.IMAGE]
        assert (rules.max_items, rules.min_width, rules.max_width, rules.max_image_bytes) == (
            1,
            400,
            800,
            8 * 1024 * 1024,
        )

    def test_exchange_requires_the_upload_scope(self):
        p = self.provider()
        with (
            patch.object(p, "_request", return_value=ok({"access_token": "at", "scope": "public"})),
            pytest.raises(OAuthError, match="upload"),
        ):
            p.exchange_code("c", "https://app/cb/")
        with patch.object(p, "_request", return_value=ok({"access_token": "at", "scope": "public upload"})):
            tokens = p.exchange_code("c", "https://app/cb/")
        assert tokens.access_token == "at" and tokens.refresh_token is None

    def test_profile(self):
        p = self.provider()
        user = {"id": 5, "name": "Dee", "login": "dee", "avatar_url": "https://a/x.png", "followers_count": 12}
        with patch.object(p, "_request", return_value=ok(user)):
            profile = p.get_profile("at")
        assert (profile.platform_id, profile.handle, profile.follower_count) == ("5", "dee", 12)

    def test_a_shot_is_a_multipart_upload_and_the_id_comes_from_location(self, image):
        p = self.provider()
        content = PublishContent(
            text="Why this works", title="Dashboard", media_files=[image()], extra={"tags": "ui, #dashboard, ui"}
        )
        reply = ok(status=202, headers={"Location": "https://api.dribbble.com/v2/shots/998877"})
        with patch.object(p, "_request", return_value=reply) as request:
            result = p.publish_post("at", content)
        parts = request.call_args.kwargs["files"]
        assert [name for name, _ in parts] == ["image", "title", "description", "tags[]", "tags[]"]
        assert dict(parts)["title"] == (None, "Dashboard")
        assert [value for name, (_, value) in parts if name == "tags[]"] == ["ui", "dashboard"]
        assert result.platform_post_id == "998877"

    @pytest.mark.parametrize(
        ("size", "message"), [((1200, 900), "400x300 or 800x600"), ((400, 400), "400x300 or 800x600")]
    )
    def test_the_image_must_be_an_accepted_size(self, image, size, message):
        p = self.provider()
        with patch.object(p, "_request") as request, pytest.raises(PublishError, match=message) as exc:
            p.publish_post("at", PublishContent(title="T", media_files=[image(size)]))
        assert exc.value.retryable is False
        request.assert_not_called()

    def test_title_image_count_and_tags_are_checked_first(self, image):
        p = self.provider()
        many = ",".join(f"t{i}" for i in range(13))
        for content in (
            PublishContent(media_files=[image()]),
            PublishContent(title="T"),
            PublishContent(title="T", media_files=[image(), image(name="b.png")]),
            PublishContent(title="T", media_files=[image()], extra={"tags": many}),
        ):
            with patch.object(p, "_request") as request, pytest.raises(PublishError):
                p.publish_post("at", content)
            request.assert_not_called()

    def test_no_location_is_ambiguous(self, image):
        p = self.provider()
        with patch.object(p, "_request", return_value=ok(status=202)), pytest.raises(APIError) as exc:
            p.publish_post("at", PublishContent(title="T", media_files=[image()]))
        assert exc.value.ambiguous_write is True

    def test_status_is_pending_until_the_shot_exists(self):
        p = self.provider()
        with patch.object(p, "_request", side_effect=APIError("nf", status_code=404)):
            assert p.check_publish_status("at", "1").state is PublishState.PENDING
        with patch.object(p, "_request", return_value=ok({"id": 1})):
            status = p.check_publish_status("at", "1")
        assert (status.state, status.platform_post_id) == (PublishState.COMPLETE, "1")
        with patch.object(p, "_request", side_effect=APIError("boom", status_code=500)), pytest.raises(APIError):
            p.check_publish_status("at", "1")
