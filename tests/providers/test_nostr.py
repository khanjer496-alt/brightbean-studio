"""Nostr provider (signed kind-1 notes to relays), with every relay connection mocked."""

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from coincurve import PublicKeyXOnly

from providers import get_provider
from providers.exceptions import APIError, OAuthError, PublishError
from providers.nostr import (
    DEFAULT_RELAYS,
    NostrProvider,
    bech32_decode,
    bech32_encode,
    build_event,
    normalize_relays,
    parse_secret_key,
    public_key_hex,
    publish_to_relay,
    relay_session,
)
from providers.types import AuthType, PublishContent, PublishResult, ThreadMode

# The NIP-19 test vectors.
NSEC = "nsec1vl029mgpspedva04g90vltkh6fvh240zqtv9k0t9af8935ke9laqsnlfe5"
SECRET_HEX = "67dea2ed018072d675f5415ecfaed7d2597555e202d85b3d65ea4e58d2d92ffa"
PUBKEY = "7e7e9c42a91bfef19fa929e5fda1b72e0ebc1a4c1141673e2794234d86addf4e"
NPUB = "npub10elfcs4fr0l0r8af98jlmgdh9c8tcxjvz9qkw038js35mp4dma8qzvjptg"
RELAYS = ["wss://a.example", "wss://b.example"]


def provider() -> NostrProvider:
    return NostrProvider({"relays": RELAYS})


def outcomes(*results):
    """publish_to_relay answers, one per relay in order."""
    return patch("providers.nostr.publish_to_relay", side_effect=list(results))


class TestKeys:
    def test_nip19_vectors(self):
        assert parse_secret_key(NSEC).hex() == SECRET_HEX
        assert public_key_hex(bytes.fromhex(SECRET_HEX)) == PUBKEY
        assert bech32_encode("npub", bytes.fromhex(PUBKEY)) == NPUB
        assert bech32_decode(NPUB) == ("npub", bytes.fromhex(PUBKEY))

    def test_hex_and_nsec_are_the_same_key(self):
        assert parse_secret_key(SECRET_HEX.upper()) == parse_secret_key(NSEC)

    @pytest.mark.parametrize(
        "raw",
        ["", "nsec1bad", "npub10elfcs4fr0l0r8af98jlmgdh9c8tcxjvz9qkw038js35mp4dma8qzvjptg", "zz" * 32, NSEC[:-1] + "q"],
    )
    def test_bad_keys_are_refused(self, raw):
        with pytest.raises(ValueError):
            parse_secret_key(raw)

    def test_an_out_of_range_key_is_refused(self):
        with pytest.raises(ValueError):
            public_key_hex(bytes(32))

    def test_relays_are_wss_only_deduplicated_and_capped(self):
        raw = "wss://a.example/, ws://plain.example\nwss://a.example https://x.example wss://user:pw@h.example wss://b.example/path"
        assert normalize_relays(raw) == ["wss://a.example", "wss://b.example/path"]
        assert len(normalize_relays([f"wss://r{i}.example" for i in range(30)])) == 10


class TestEvents:
    def test_the_event_is_a_valid_nip01_signature(self):
        event = build_event(
            bytes.fromhex(SECRET_HEX), kind=1, content="héllo", tags=[["t", "x"]], created_at=1700000000
        )
        serialized = json.dumps(
            [0, PUBKEY, 1700000000, 1, [["t", "x"]], "héllo"], separators=(",", ":"), ensure_ascii=False
        )
        assert event["id"] == hashlib.sha256(serialized.encode()).hexdigest()
        assert event["pubkey"] == PUBKEY
        assert PublicKeyXOnly(bytes.fromhex(PUBKEY)).verify(bytes.fromhex(event["sig"]), bytes.fromhex(event["id"]))


class TestDeclarations:
    def test_session_auth_with_a_reply_chain(self):
        p = provider()
        assert p.auth_type is AuthType.SESSION
        assert NostrProvider.thread.mode is ThreadMode.REPLY_CHAIN
        assert NostrProvider.connect_url_name == "connect_nostr"
        assert isinstance(get_provider("nostr", {}), NostrProvider)

    def test_default_relays_when_the_account_has_none(self):
        assert NostrProvider({}).relays == list(DEFAULT_RELAYS)

    def test_relays_come_from_the_account(self):
        account = SimpleNamespace(platform_data={"relays": RELAYS})
        assert NostrProvider.credentials_for_account(account) == {"relays": RELAYS}
        assert NostrProvider.credentials_for_account(SimpleNamespace(platform_data={})) == {}

    def test_links(self):
        account = SimpleNamespace(account_platform_id=PUBKEY)
        assert NostrProvider.profile_url(account) == f"https://njump.me/{NPUB}"
        assert NostrProvider.permalink_for(PUBKEY, account).startswith("https://njump.me/note1")
        assert NostrProvider.permalink_for("nope", account) == ""


class TestConnect:
    def test_profile_comes_from_the_relays_and_the_token_is_hex(self):
        p = provider()
        meta = {"display_name": "Alice", "name": "alice", "picture": "https://img.example/a.png"}
        with patch("providers.nostr._fetch_profile", return_value=meta) as fetch:
            tokens, profile = p.connect(NSEC)
        fetch.assert_called_once_with(RELAYS, PUBKEY)
        assert tokens.access_token == SECRET_HEX
        assert (profile.platform_id, profile.name, profile.handle) == (PUBKEY, "Alice", "alice")
        assert profile.avatar_url == "https://img.example/a.png"
        assert profile.extra["account_data"] == {"relays": RELAYS}

    def test_no_profile_connects_under_the_npub(self):
        with patch("providers.nostr._fetch_profile", return_value={}):
            _tokens, profile = provider().connect(SECRET_HEX)
        assert profile.name.startswith("npub1") and profile.avatar_url is None

    def test_a_plain_http_picture_is_not_stored(self):
        with patch("providers.nostr._fetch_profile", return_value={"name": "a", "picture": "http://x/a.png"}):
            _tokens, profile = provider().connect(SECRET_HEX)
        assert profile.avatar_url is None

    def test_a_bad_key_never_touches_a_relay(self):
        with patch("providers.nostr._fetch_profile") as fetch, pytest.raises(ValueError):
            provider().connect("not a key")
        fetch.assert_not_called()

    def test_health_check_validates_the_stored_key(self):
        assert provider().get_profile(SECRET_HEX).platform_id == PUBKEY
        with pytest.raises(OAuthError):
            provider().get_profile("garbage")


class TestPublishing:
    def test_a_note_is_signed_and_sent_to_every_relay(self):
        content = PublishContent(text="Hello #Nostr world", media_urls=["https://cdn.example/p.png"])
        with outcomes(("ok", ""), ("unreachable", "")) as publish:
            result = provider().publish_post(SECRET_HEX, content)
        sent = publish.call_args_list[0].args[1]
        assert sent["kind"] == 1
        assert sent["content"] == "Hello #Nostr world\n\nhttps://cdn.example/p.png"
        assert ["t", "nostr"] in sent["tags"]
        assert ["imeta", "url https://cdn.example/p.png", "m image/png"] in sent["tags"]
        assert [call.args[0] for call in publish.call_args_list] == RELAYS
        assert result.platform_post_id == sent["id"]
        assert result.extra["relays"] == ["wss://a.example"]
        assert result.url.startswith("https://njump.me/note1")

    def test_an_unconfirmed_note_is_ambiguous_and_not_resent(self):
        with outcomes(("unknown", ""), ("unreachable", "")), pytest.raises(APIError) as exc:
            provider().publish_post(SECRET_HEX, PublishContent(text="x"))
        assert exc.value.ambiguous_write is True

    def test_all_relays_down_is_retryable(self):
        with outcomes(("unreachable", ""), ("unreachable", "")), pytest.raises(APIError) as exc:
            provider().publish_post(SECRET_HEX, PublishContent(text="x"))
        assert exc.value.retryable is True and exc.value.ambiguous_write is False

    def test_a_refusal_is_permanent_and_a_rate_limit_is_not(self):
        with outcomes(("rejected", "blocked: no"), ("rejected", "")), pytest.raises(PublishError) as exc:
            provider().publish_post(SECRET_HEX, PublishContent(text="x"))
        assert exc.value.retryable is False
        with outcomes(("rejected", "rate-limited: slow down"), ("unreachable", "")), pytest.raises(PublishError) as exc:
            provider().publish_post(SECRET_HEX, PublishContent(text="x"))
        assert exc.value.retryable is True

    def test_nothing_to_post_and_an_invalid_key_make_no_connection(self):
        with outcomes() as publish:
            with pytest.raises(PublishError):
                provider().publish_post(SECRET_HEX, PublishContent())
            with pytest.raises(OAuthError):
                provider().publish_post("bad", PublishContent(text="x"))
            with pytest.raises(PublishError):
                provider().publish_post(SECRET_HEX, PublishContent(text="x" * 5001))
        publish.assert_not_called()

    def test_thread_parts_reply_with_nip10_markers(self):
        root = PublishResult(platform_post_id="aa" * 32, extra={"pubkey": PUBKEY})
        parent = PublishResult(platform_post_id="bb" * 32, extra={"pubkey": PUBKEY})
        with outcomes(("ok", ""), ("ok", "")) as publish:
            provider().publish_thread_part(SECRET_HEX, PublishContent(text="two"), root=root, parent=parent)
        tags = publish.call_args_list[0].args[1]["tags"]
        assert tags[:3] == [["e", "aa" * 32, "", "root"], ["e", "bb" * 32, "", "reply"], ["p", PUBKEY]]

    def test_the_second_part_replies_to_the_root_only(self):
        root = PublishResult(platform_post_id="aa" * 32, extra={"pubkey": PUBKEY})
        with outcomes(("ok", ""), ("ok", "")) as publish:
            provider().publish_thread_part(SECRET_HEX, PublishContent(text="two"), root=root, parent=root)
        assert publish.call_args_list[0].args[1]["tags"][:2] == [["e", "aa" * 32, "", "root"], ["p", PUBKEY]]

    def test_delete_is_a_kind_5_request(self):
        with outcomes(("ok", ""), ("ok", "")) as publish:
            assert provider().delete_post(SECRET_HEX, "cc" * 32) is True
        event = publish.call_args_list[0].args[1]
        assert event["kind"] == 5 and ["e", "cc" * 32] in event["tags"]


class TestRelayIO:
    def test_a_private_relay_is_never_dialled(self):
        with (
            patch("providers.nostr.resolve_public_ip", return_value=None),
            patch("providers.nostr.socket") as sock,
            pytest.raises(ConnectionError),
        ):
            relay_session("wss://internal.example", ["EVENT", {}], lambda f: True)
        sock.create_connection.assert_not_called()

    def test_the_socket_goes_to_the_resolved_public_address(self):
        ws = MagicMock()
        ws.__enter__.return_value = ws
        ws.recv.return_value = json.dumps(["OK", "id1", True, ""])
        with (
            patch("providers.nostr.resolve_public_ip", return_value="203.0.113.9"),
            patch("providers.nostr.socket.create_connection", return_value="SOCK") as dial,
            patch("websockets.sync.client.connect", return_value=ws) as connect,
        ):
            frames = relay_session("wss://r.example:444/x", ["EVENT", {"id": "id1"}], lambda f: f[0] == "OK")
        assert dial.call_args.args[0] == ("203.0.113.9", 444)
        assert connect.call_args.kwargs["sock"] == "SOCK"
        assert connect.call_args.kwargs["server_hostname"] == "r.example"
        assert frames == [["OK", "id1", True, ""]]

    def test_outcomes(self):
        event = {"id": "id1"}
        with patch("providers.nostr.relay_session", return_value=[["OK", "id1", True, ""]]):
            assert publish_to_relay("wss://r", event) == ("ok", "")
        with patch("providers.nostr.relay_session", return_value=[["OK", "id1", False, "blocked: x"]]):
            assert publish_to_relay("wss://r", event) == ("rejected", "blocked: x")
        with patch("providers.nostr.relay_session", side_effect=TimeoutError):
            assert publish_to_relay("wss://r", event)[0] == "unknown"
        with patch("providers.nostr.relay_session", side_effect=ConnectionError):
            assert publish_to_relay("wss://r", event)[0] == "unreachable"
