"""Tumblr provider (OAuth2, NPF posts, blog picker), with every HTTP call mocked."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest

from providers import get_provider
from providers.exceptions import OAuthError, PublishError
from providers.tumblr import TumblrProvider, parse_tags
from providers.types import AuthType, OAuthTokens, PublishContent, ThreadMode

CREDS = {"client_id": "key", "client_secret": "secret", "blog": "myblog"}


def provider(**extra) -> TumblrProvider:
    return TumblrProvider({**CREDS, **extra})


def ok(body, status=200):
    return MagicMock(status_code=status, json=MagicMock(return_value=body))


def created(post_id="1234567890"):
    return ok({"meta": {"status": 201}, "response": {"id": post_id}}, 201)


class TestDeclarations:
    def test_oauth_blog_picker_without_threads(self):
        p = provider()
        assert p.auth_type is AuthType.OAUTH2
        assert p.required_scopes == ["basic", "write", "offline_access"]
        assert TumblrProvider.has_destination_picker is True
        assert TumblrProvider.thread.mode is ThreadMode.NONE
        assert isinstance(get_provider("tumblr", CREDS), TumblrProvider)

    def test_credentials_for_account_name_the_blog(self):
        assert TumblrProvider.credentials_for_account(SimpleNamespace(account_handle="myblog")) == {"blog": "myblog"}

    def test_permalink(self):
        account = SimpleNamespace(account_handle="my-blog")
        assert TumblrProvider.permalink_for("123", account) == "https://my-blog.tumblr.com/post/123"
        assert TumblrProvider.permalink_for("../x", account) == ""

    def test_tags(self):
        assert parse_tags("#Travel, photo,travel ,,") == ["Travel", "photo"]


class TestOAuth:
    def test_authorize_url_asks_for_a_refreshable_grant(self):
        parsed = urlparse(provider().get_auth_url("https://app.example/cb/", "st"))
        query = parse_qs(parsed.query)
        assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == "https://www.tumblr.com/oauth2/authorize"
        assert query["scope"] == ["basic write offline_access"]
        assert query["response_type"] == ["code"]
        assert query["client_id"] == ["key"]

    def test_exchange_sends_the_app_credentials(self):
        p = provider()
        body = {"access_token": "at", "refresh_token": "rt", "expires_in": 2520}
        with patch.object(p, "_request", return_value=ok(body)) as request:
            tokens = p.exchange_code("code", "https://app.example/cb/")
        assert request.call_args.args[:2] == ("POST", "https://api.tumblr.com/v2/oauth2/token")
        assert request.call_args.kwargs["data"] == {
            "grant_type": "authorization_code",
            "code": "code",
            "redirect_uri": "https://app.example/cb/",
            "client_id": "key",
            "client_secret": "secret",
        }
        assert (tokens.access_token, tokens.refresh_token, tokens.expires_in) == ("at", "rt", 2520)

    def test_an_error_body_is_an_oauth_error_that_does_not_leak_the_body(self):
        p = provider()
        with (
            patch.object(p, "_request", return_value=ok({"error": "bad", "secret": "x"})),
            pytest.raises(OAuthError) as exc,
        ):
            p.exchange_code("c", "https://x/")
        assert "secret" not in str(exc.value)

    def test_refresh_keeps_the_old_refresh_token_when_none_is_returned(self):
        p = provider()
        with patch.object(p, "_request", return_value=ok({"access_token": "new", "expires_in": 2520})) as request:
            tokens = p.refresh_token("rt")
        assert request.call_args.kwargs["data"]["grant_type"] == "refresh_token"
        assert (tokens.access_token, tokens.refresh_token) == ("new", "rt")


class TestBlogs:
    BLOGS = {
        "response": {
            "user": {
                "blogs": [
                    {"name": "second", "title": "Second", "uuid": "t:2", "followers": 3},
                    {"name": "first", "title": "", "uuid": "t:1", "primary": True, "followers": 9},
                ]
            }
        }
    }

    def test_profile_is_the_primary_blog(self):
        p = provider()
        with patch.object(p, "_request", return_value=ok(self.BLOGS)):
            profile = p.get_profile("at")
        assert (profile.platform_id, profile.handle, profile.name, profile.follower_count) == (
            "t:1",
            "first",
            "first",
            9,
        )

    def test_picker_lists_every_blog_with_the_grants_tokens(self):
        p = provider()
        with patch.object(p, "_request", return_value=ok(self.BLOGS)):
            rows = p.list_destinations(OAuthTokens(access_token="at", refresh_token="rt", expires_in=2520))
        assert [r["id"] for r in rows] == ["t:2", "t:1"]
        assert all(r["access_token"] == "at" and r["refresh_token"] == "rt" and r["expires_in"] == 2520 for r in rows)


class TestPublishing:
    def test_a_text_post_is_npf_with_tags_and_state(self):
        p = provider()
        content = PublishContent(
            text="First paragraph\n\nSecond", title="Hello", extra={"tags": "a, b", "state": "draft"}
        )
        with patch.object(p, "_request", return_value=created()) as request:
            result = p.publish_post("at", content)
        assert request.call_args.args[:2] == ("POST", "https://api.tumblr.com/v2/blog/myblog.tumblr.com/posts")
        body = request.call_args.kwargs["json"]
        assert body["content"] == [
            {"type": "text", "subtype": "heading1", "text": "Hello"},
            {"type": "text", "text": "First paragraph"},
            {"type": "text", "text": "Second"},
        ]
        assert (body["state"], body["tags"]) == ("draft", "a,b")
        assert result.platform_post_id == "1234567890"
        assert result.url == "https://myblog.tumblr.com/post/1234567890"

    def test_a_link_is_a_link_block(self):
        p = provider()
        with patch.object(p, "_request", return_value=created()) as request:
            p.publish_post("at", PublishContent(text="Read this", link_url="https://example.com/a"))
        assert {"type": "link", "url": "https://example.com/a"} in request.call_args.kwargs["json"]["content"]

    def test_media_goes_up_as_multipart_with_identifiers(self, tmp_path):
        image = tmp_path / "pic.png"
        image.write_bytes(b"png")
        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"mp4")
        p = provider()
        content = PublishContent(
            text="Look",
            media_files=[str(image), str(clip)],
            media_types=["image", "video"],
            extra={"alt_text": "A picture"},
        )
        with patch.object(p, "_request", return_value=created()) as request:
            p.publish_post("at", content)
        files = request.call_args.kwargs["files"]
        assert set(files) == {"json", "media-0", "media-1"}
        body = json.loads(files["json"][1])
        assert body["content"][1] == {
            "type": "image",
            "media": [{"type": "image/png", "identifier": "media-0"}],
            "alt_text": "A picture",
        }
        assert body["content"][2] == {"type": "video", "media": {"type": "video/mp4", "identifier": "media-1"}}
        assert files["media-0"][2] == "image/png"

    def test_an_empty_post_is_refused_before_any_call(self):
        p = provider()
        with patch.object(p, "_request") as request, pytest.raises(PublishError) as exc:
            p.publish_post("at", PublishContent())
        assert exc.value.retryable is False
        request.assert_not_called()

    def test_no_blog_is_a_permanent_error(self):
        with pytest.raises(PublishError):
            TumblrProvider({"client_id": "k", "client_secret": "s"}).publish_post("at", PublishContent(text="x"))

    def test_a_reply_without_an_id_is_ambiguous(self):
        from providers.exceptions import APIError

        p = provider()
        with patch.object(p, "_request", return_value=ok({"response": {}})), pytest.raises(APIError) as exc:
            p.publish_post("at", PublishContent(text="x"))
        assert exc.value.ambiguous_write is True

    def test_delete(self):
        p = provider()
        with patch.object(p, "_request", return_value=ok({})) as request:
            assert p.delete_post("at", "55") is True
        assert request.call_args.args[:2] == ("POST", "https://api.tumblr.com/v2/blog/myblog.tumblr.com/post/delete")
        assert request.call_args.kwargs["data"] == {"id": "55"}
