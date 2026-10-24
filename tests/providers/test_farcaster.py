"""Farcaster provider (Neynar API key + approved signer), with every HTTP call mocked."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest

from providers import get_provider
from providers.exceptions import APIError, OAuthError, PublishError
from providers.farcaster import FarcasterProvider, normalize_channel
from providers.types import AuthType, PublishContent, PublishResult, ThreadMode

CREDS = {"api_key": "neynar-key"}
HASH = "0x" + "ab" * 20


def provider(**creds) -> FarcasterProvider:
    return FarcasterProvider({**CREDS, **creds})


def ok(body, status=200):
    return MagicMock(status_code=status, json=MagicMock(return_value=body))


def cast_reply(hash_=HASH):
    return ok({"success": True, "cast": {"hash": hash_, "author": {"fid": 3, "username": "dwr"}}})


def error_response(status):
    return httpx.Response(status, json={"message": "no"}, request=httpx.Request("POST", "https://api.neynar.com/x"))


class TestDeclarations:
    def test_bring_your_own_key(self):
        p = provider()
        assert p.auth_type is AuthType.SESSION
        assert FarcasterProvider.user_supplied_credentials is True
        assert FarcasterProvider.connect_url_name == "connect_farcaster"
        assert FarcasterProvider.thread.mode is ThreadMode.REPLY_CHAIN
        assert p.max_caption_length == 320
        assert isinstance(get_provider("farcaster", CREDS), FarcasterProvider)

    def test_channel(self):
        assert normalize_channel("/Dev") == "dev"
        assert normalize_channel("bad channel") == ""
        assert FarcasterProvider.validate_post_settings({}) == ""
        assert "Channel must be" in FarcasterProvider.validate_post_settings({"channel": "Bad Channel!"})

    def test_permalink(self):
        account = SimpleNamespace(account_handle="dwr")
        assert FarcasterProvider.permalink_for(HASH, account) == f"https://farcaster.xyz/dwr/{HASH}"
        assert FarcasterProvider.permalink_for("zz", account) == ""

    def test_a_missing_key_is_refused_before_any_call(self):
        with patch("providers.base.httpx.Client") as client, pytest.raises(OAuthError):
            FarcasterProvider({}).connect("signer")
        client.assert_not_called()


class TestConnect:
    def test_an_approved_signer_connects_its_account(self):
        p = provider()
        replies = [
            ok({"signer_uuid": "s", "status": "approved", "fid": 3}),
            ok(
                {
                    "users": [
                        {
                            "fid": 3,
                            "username": "dwr",
                            "display_name": "Dan",
                            "pfp_url": "https://p/x.png",
                            "follower_count": 10,
                        }
                    ]
                }
            ),
        ]
        with patch.object(p, "_request", side_effect=replies) as request:
            tokens, profile = p.connect("s")
        assert request.call_args_list[0].kwargs["headers"] == {"x-api-key": "neynar-key"}
        assert request.call_args_list[0].kwargs["params"] == {"signer_uuid": "s"}
        assert request.call_args_list[1].kwargs["params"] == {"fids": "3"}
        assert tokens.access_token == "s"
        assert (profile.platform_id, profile.handle, profile.name, profile.follower_count) == ("3", "dwr", "Dan", 10)

    @pytest.mark.parametrize(
        "signer", [{"status": "pending_approval", "fid": 3}, {"status": "approved"}, {"status": "revoked", "fid": 1}]
    )
    def test_an_unapproved_signer_is_refused_with_its_status(self, signer):
        p = provider()
        with patch.object(p, "_request", return_value=ok(signer)), pytest.raises(OAuthError) as exc:
            p.connect("s")
        assert "not approved" in str(exc.value) and exc.value.retryable is False


class TestPublishing:
    def test_a_cast_with_channel_and_embeds(self):
        p = provider()
        content = PublishContent(
            text="gm",
            link_url="https://e.com",
            media_urls=["https://cdn/x.png"],
            extra={"channel": "Dev"},
            idempotency_key="k1",
        )
        with patch.object(p, "_request", return_value=cast_reply()) as request:
            result = p.publish_post("signer", content)
        assert request.call_args.args[:2] == ("POST", "https://api.neynar.com/v2/farcaster/cast")
        body = request.call_args.kwargs["json"]
        assert body["signer_uuid"] == "signer" and body["text"] == "gm" and body["channel_id"] == "dev"
        assert body["embeds"] == [{"url": "https://e.com"}, {"url": "https://cdn/x.png"}]
        assert len(body["idem"]) == 16
        assert (result.platform_post_id, result.url) == (HASH, f"https://farcaster.xyz/dwr/{HASH}")

    def test_the_idempotency_key_is_stable_across_retries(self):
        p = provider()
        bodies = []
        for _ in range(2):
            with patch.object(p, "_request", return_value=cast_reply()) as request:
                p.publish_post("signer", PublishContent(text="gm", idempotency_key="same"))
            bodies.append(request.call_args.kwargs["json"]["idem"])
        assert bodies[0] == bodies[1]

    @pytest.mark.parametrize(
        "content",
        [
            PublishContent(),
            PublishContent(text="x" * 321),
            PublishContent(text="é" * 161),
            PublishContent(text="x", media_urls=["https://a/1.png", "https://a/2.png", "https://a/3.png"]),
        ],
    )
    def test_permanent_refusals_make_no_call(self, content):
        p = provider()
        with patch.object(p, "_request") as request, pytest.raises(PublishError) as exc:
            p.publish_post("signer", content)
        assert exc.value.retryable is False
        request.assert_not_called()

    def test_320_bytes_is_allowed(self):
        p = provider()
        with patch.object(p, "_request", return_value=cast_reply()):
            p.publish_post("signer", PublishContent(text="é" * 160))

    def test_no_hash_back_is_ambiguous(self):
        p = provider()
        with patch.object(p, "_request", return_value=ok({"success": True})), pytest.raises(APIError) as exc:
            p.publish_post("signer", PublishContent(text="x"))
        assert exc.value.ambiguous_write is True

    def test_a_thread_part_replies_to_the_part_before(self):
        p = provider()
        root = PublishResult(platform_post_id="0x" + "11" * 20, extra={"fid": 3})
        parent = PublishResult(platform_post_id="0x" + "22" * 20, extra={"fid": 3})
        with patch.object(p, "_request", return_value=cast_reply()) as request:
            p.publish_thread_part(
                "signer", PublishContent(text="two", extra={"channel": "dev"}), root=root, parent=parent
            )
        body = request.call_args.kwargs["json"]
        assert body["parent"] == "0x" + "22" * 20 and body["parent_author_fid"] == 3
        assert "channel_id" not in body

    def test_delete(self):
        p = provider()
        with patch.object(p, "_request", return_value=ok({"success": True})) as request:
            assert p.delete_post("signer", HASH) is True
        assert request.call_args.args[:2] == ("DELETE", "https://api.neynar.com/v2/farcaster/cast")
        assert request.call_args.kwargs["json"] == {"signer_uuid": "signer", "target_hash": HASH}


class TestErrors:
    def test_a_plan_without_publishing_says_so_and_is_not_retried(self):
        error = provider()._error_for_response(error_response(402))
        assert isinstance(error, APIError) and error.status_code == 402 and error.retryable is False
        assert "plan" in str(error)

    def test_a_mismatched_key_or_unapproved_signer_is_explained(self):
        error = provider()._error_for_response(error_response(403))
        assert error.retryable is False and "signer" in str(error)
