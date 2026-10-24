"""Nostr provider: signed kind-1 notes published to the user's relays.

There is no platform account and no OAuth. The user gives the private key of the identity to
post as (``nsec1…`` or 64 hex characters) and the relays to publish to. The key is the
account's credential: it is stored encrypted like every other token (``oauth_access_token``),
never logged, and never sent anywhere except into a local signing call. The relay list lives
in ``SocialAccount.platform_data`` (it is not secret).

Protocol, checked 2026-10-24:

- NIP-01 (events, ``["EVENT", …]`` / ``["OK", …]`` / ``["REQ", …]`` frames, BIP-340 Schnorr
  signatures over the event id): https://github.com/nostr-protocol/nips/blob/master/01.md
- NIP-10 (``e`` tags with ``root`` / ``reply`` markers): https://github.com/nostr-protocol/nips/blob/master/10.md
- NIP-19 (bech32 ``nsec``, ``npub``, ``note``): https://github.com/nostr-protocol/nips/blob/master/19.md
- NIP-09 (kind 5 deletion requests): https://github.com/nostr-protocol/nips/blob/master/09.md
- NIP-24 (``t`` hashtag tags) and NIP-92 (``imeta`` media tags).

Relays are addresses the user typed, so they are treated like any user-supplied URL: ``wss`` only,
resolved to a public address, and the socket is opened to that address (not re-resolved) to keep
a DNS answer from changing between the check and the connection.
"""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import re
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from urllib.parse import urlsplit

from apps.common.validators import resolve_public_ip

from .base import SocialProvider
from .exceptions import APIError, OAuthError, PublishError
from .types import (
    AccountProfile,
    AuthType,
    MediaRules,
    MediaType,
    OAuthTokens,
    PostType,
    PublishContent,
    PublishResult,
    RateLimitConfig,
    ThreadCapability,
    ThreadMode,
)

logger = logging.getLogger(__name__)

# Relays a new account starts with. Anyone can change the list when connecting.
DEFAULT_RELAYS = (
    "wss://relay.damus.io",
    "wss://nos.lol",
    "wss://relay.primal.net",
    "wss://offchain.pub",
)
MAX_RELAYS = 10
RELAY_TIMEOUT = 10.0
MAX_NOTE_LENGTH = 5000

_HEX64 = re.compile(r"[0-9a-fA-F]{64}")
_HASHTAG = re.compile(r"(?<!\w)#(\w+)", re.UNICODE)

_NIP_DOCS = "https://github.com/nostr-protocol/nips/blob/master/92.md"
NOSTR_MEDIA_RULES = {
    # Media is shared as links in the note (NIP-92 describes them), so size is the host's concern.
    PostType.IMAGE: MediaRules(source=_NIP_DOCS),
    PostType.VIDEO: MediaRules(source=_NIP_DOCS),
}

# ---------------------------------------------------------------------------
# NIP-19 bech32 (BIP-173 checksum, no segwit rules)
# ---------------------------------------------------------------------------

_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _polymod(values: list[int]) -> int:
    generators = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    chk = 1
    for value in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ value
        for i, generator in enumerate(generators):
            if (top >> i) & 1:
                chk ^= generator
    return chk


def _hrp_expand(hrp: str) -> list[int]:
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _convert_bits(data: bytes | list[int], from_bits: int, to_bits: int, pad: bool) -> list[int] | None:
    acc = bits = 0
    out: list[int] = []
    max_value = (1 << to_bits) - 1
    for value in data:
        if value >> from_bits:
            return None
        acc = (acc << from_bits) | value
        bits += from_bits
        while bits >= to_bits:
            bits -= to_bits
            out.append((acc >> bits) & max_value)
    if pad and bits:
        out.append((acc << (to_bits - bits)) & max_value)
    elif not pad and (bits >= from_bits or (acc << (to_bits - bits)) & max_value):
        return None
    return out


def bech32_encode(hrp: str, data: bytes) -> str:
    five = _convert_bits(data, 8, 5, True) or []
    checksum_input = _hrp_expand(hrp) + five + [0] * 6
    mod = _polymod(checksum_input) ^ 1
    checksum = [(mod >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(_CHARSET[d] for d in five + checksum)


def bech32_decode(text: str) -> tuple[str, bytes] | None:
    """``(hrp, data)`` of a valid bech32 string, or None. Nostr strings are longer than BIP-173's 90."""
    if text != text.lower() and text != text.upper():
        return None
    text = text.lower()
    pos = text.rfind("1")
    if pos < 1 or pos + 7 > len(text) or any(c not in _CHARSET for c in text[pos + 1 :]):
        return None
    hrp = text[:pos]
    values = [_CHARSET.find(c) for c in text[pos + 1 :]]
    if _polymod(_hrp_expand(hrp) + values) != 1:
        return None
    data = _convert_bits(values[:-6], 5, 8, False)
    return (hrp, bytes(data)) if data is not None else None


def parse_secret_key(raw: str) -> bytes:
    """The 32-byte secret key from an ``nsec1…`` or 64-hex string, or ValueError."""
    value = (raw or "").strip()
    if _HEX64.fullmatch(value):
        key = bytes.fromhex(value)
    else:
        decoded = bech32_decode(value) if value.lower().startswith("nsec1") else None
        if not decoded or decoded[0] != "nsec" or len(decoded[1]) != 32:
            raise ValueError("Enter the private key as nsec1… or 64 hexadecimal characters.")
        key = decoded[1]
    return key


def public_key_hex(secret: bytes) -> str:
    from coincurve import PrivateKey

    try:
        return PrivateKey(secret).public_key_xonly.format().hex()
    except ValueError as exc:
        raise ValueError("That is not a valid Nostr private key.") from exc


def normalize_relays(raw) -> list[str]:
    """Relay addresses from a list or a comma/space/newline separated string: ``wss`` only, no repeats, capped."""
    items = re.split(r"[\s,]+", raw) if isinstance(raw, str) else list(raw or [])
    relays: list[str] = []
    for item in items:
        parts = urlsplit(str(item).strip())
        if parts.scheme != "wss" or not parts.hostname or parts.username or parts.password:
            continue
        url = f"wss://{parts.netloc}{parts.path.rstrip('/')}"
        if url not in relays:
            relays.append(url)
    return relays[:MAX_RELAYS]


# ---------------------------------------------------------------------------
# Events and relay I/O
# ---------------------------------------------------------------------------


def build_event(
    secret: bytes, *, kind: int, content: str, tags: list[list[str]], created_at: int | None = None
) -> dict:
    """A NIP-01 event, id computed and signed with BIP-340."""
    from coincurve import PrivateKey

    key = PrivateKey(secret)
    pubkey = key.public_key_xonly.format().hex()
    created_at = int(time.time()) if created_at is None else created_at
    serialized = json.dumps([0, pubkey, created_at, kind, tags, content], separators=(",", ":"), ensure_ascii=False)
    event_id = hashlib.sha256(serialized.encode()).digest()
    return {
        "id": event_id.hex(),
        "pubkey": pubkey,
        "created_at": created_at,
        "kind": kind,
        "tags": tags,
        "content": content,
        "sig": key.sign_schnorr(event_id).hex(),
    }


def relay_session(url: str, frame: list, done, timeout: float = RELAY_TIMEOUT) -> list[list]:
    """Send ``frame`` to the relay and collect the frames it answers with until ``done(frame)`` is true.

    Opens the socket to a public address resolved once (never a private one), over TLS. Raises
    ``ConnectionError`` when the relay can't be reached, ``TimeoutError`` when it stops answering.
    """
    from websockets.sync.client import connect

    parts = urlsplit(url)
    ip = resolve_public_ip(f"https://{parts.netloc}")
    if not ip:
        raise ConnectionError("relay address is not public")
    deadline = time.monotonic() + timeout
    try:
        sock = socket.create_connection((ip, parts.port or 443), timeout=timeout)
        ws = connect(url, sock=sock, server_hostname=parts.hostname, open_timeout=timeout, max_size=2**20)
    except Exception as exc:  # OSError, or websockets' own handshake errors
        raise ConnectionError("relay unreachable") from exc
    frames: list[list] = []
    with ws:
        ws.send(json.dumps(frame, separators=(",", ":"), ensure_ascii=False))
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("relay did not answer")
            try:
                message = json.loads(ws.recv(timeout=remaining))
            except TimeoutError:
                raise
            except Exception as exc:  # ValueError for a bad frame, or the connection closing
                raise ConnectionError("relay closed the connection") from exc
            if isinstance(message, list):
                frames.append(message)
                if done(message):
                    return frames


def publish_to_relay(url: str, event: dict) -> tuple[str, str]:
    """``(outcome, reason)``: ``ok``, ``rejected`` (the relay said no), ``unreachable`` or ``unknown`` (no answer)."""
    try:
        frames = relay_session(url, ["EVENT", event], lambda f: len(f) >= 3 and f[0] == "OK" and f[1] == event["id"])
    except ConnectionError:
        return "unreachable", ""
    except TimeoutError:
        return "unknown", ""
    ok = frames[-1]
    return ("ok", "") if ok[2] is True else ("rejected", str(ok[3] if len(ok) > 3 else ""))


def _fetch_profile(relays: list[str], pubkey: str) -> dict:
    """The kind-0 profile content from the first relay that has one, or {}."""

    def ask(url: str) -> dict:
        sub = "profile"
        try:
            frames = relay_session(
                url,
                ["REQ", sub, {"kinds": [0], "authors": [pubkey], "limit": 1}],
                lambda f: f[0] in ("EOSE", "CLOSED"),
                timeout=6.0,
            )
        except (ConnectionError, TimeoutError):
            return {}
        for frame in frames:
            if frame[0] == "EVENT" and len(frame) >= 3 and isinstance(frame[2], dict):
                try:
                    data = json.loads(frame[2].get("content") or "{}")
                except ValueError:
                    return {}
                return data if isinstance(data, dict) else {}
        return {}

    with ThreadPoolExecutor(max_workers=max(1, len(relays))) as pool:
        for result in pool.map(ask, relays):
            if result:
                return result
    return {}


class NostrProvider(SocialProvider):
    """Nostr provider: kind-1 notes signed locally and sent to the account's relays."""

    works_without_platform_app = True
    connect_url_name = "connect_nostr"
    needs_local_media = False
    media_rules = NOSTR_MEDIA_RULES
    supports_connection_link = False

    # A reply to the part before it (NIP-10), so the parts read as one thread under the profile.
    thread = ThreadCapability(mode=ThreadMode.REPLY_CHAIN, max_parts=25, media_per_part=True)

    writing_style = (
        "Direct and conversational, written for an audience that values plain text. Light on emoji and "
        "promotion; hashtags are few and lowercase."
    )

    def __init__(self, credentials: dict | None = None):
        super().__init__(credentials)
        self.relays: list[str] = normalize_relays(self.credentials.get("relays")) or list(DEFAULT_RELAYS)

    @classmethod
    def credentials_for_account(cls, account) -> dict:
        relays = (account.platform_data or {}).get("relays")
        return {"relays": relays} if relays else {}

    @classmethod
    def profile_url(cls, account) -> str:
        pubkey = (account.account_platform_id or "").strip()
        if not _HEX64.fullmatch(pubkey):
            return ""
        return f"https://njump.me/{bech32_encode('npub', bytes.fromhex(pubkey))}"

    @classmethod
    def permalink_for(cls, platform_post_id: str, account) -> str:
        event_id = (platform_post_id or "").strip()
        if not _HEX64.fullmatch(event_id):
            return ""
        return f"https://njump.me/{bech32_encode('note', bytes.fromhex(event_id))}"

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    @property
    def platform_name(self) -> str:
        return "Nostr"

    @property
    def auth_type(self) -> AuthType:
        return AuthType.SESSION

    @property
    def max_caption_length(self) -> int:
        return MAX_NOTE_LENGTH

    @property
    def supported_post_types(self) -> list[PostType]:
        return [PostType.TEXT, PostType.IMAGE, PostType.VIDEO, PostType.LINK]

    @property
    def supported_media_types(self) -> list[MediaType]:
        return [MediaType.JPEG, MediaType.PNG, MediaType.GIF, MediaType.WEBP, MediaType.MP4, MediaType.MOV]

    @property
    def required_scopes(self) -> list[str]:
        return []

    @property
    def rate_limits(self) -> RateLimitConfig:
        # Relays set their own limits; keep well under what a free public relay tolerates.
        return RateLimitConfig(requests_per_hour=600, requests_per_day=5000, publish_per_day=100)

    # ------------------------------------------------------------------
    # Connect
    # ------------------------------------------------------------------

    def connect(self, secret_key: str) -> tuple[OAuthTokens, AccountProfile]:
        """Validate the typed private key and read the identity's profile from the relays.

        The token is the key as 64 hex characters, so the stored form is the same whichever way
        the user typed it.
        """
        secret = parse_secret_key(secret_key)
        pubkey = public_key_hex(secret)
        info = _fetch_profile(self.relays, pubkey)
        npub = bech32_encode("npub", bytes.fromhex(pubkey))
        name = str(info.get("display_name") or info.get("displayName") or info.get("name") or "").strip()
        picture = str(info.get("picture") or "")
        profile = AccountProfile(
            platform_id=pubkey,
            name=name or f"{npub[:12]}…",
            handle=str(info.get("name") or "").strip() or npub[:16],
            avatar_url=picture if picture.startswith("https://") and len(picture) <= 2000 else None,
            extra={"account_data": {"relays": self.relays}},
        )
        return OAuthTokens(access_token=secret.hex()), profile

    def get_profile(self, access_token: str) -> AccountProfile:
        """Health check: the stored key still parses and still belongs to the account's identity."""
        try:
            secret = parse_secret_key(access_token)
            pubkey = public_key_hex(secret)
        except ValueError as exc:
            raise OAuthError("The stored Nostr key is not valid", platform=self.platform_name) from exc
        return AccountProfile(platform_id=pubkey, name=pubkey[:12])

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    @staticmethod
    def _note_tags(content: PublishContent, reply_tags: list[list[str]]) -> list[list[str]]:
        tags = list(reply_tags)
        seen: set[str] = set()
        for tag in _HASHTAG.findall(content.text or ""):
            if tag.lower() not in seen:
                seen.add(tag.lower())
                tags.append(["t", tag.lower()])
        for url in content.media_urls:
            mime = mimetypes.guess_type(urlsplit(url).path)[0]
            tags.append(["imeta", f"url {url}", *([f"m {mime}"] if mime else [])])
        return tags

    def _send(self, secret: bytes, event_kind: int, text: str, tags: list[list[str]]) -> dict:
        event = build_event(secret, kind=event_kind, content=text, tags=tags)
        with ThreadPoolExecutor(max_workers=len(self.relays)) as pool:
            outcomes = list(pool.map(lambda url: publish_to_relay(url, event), self.relays))
        kinds = [outcome for outcome, _ in outcomes]
        if "ok" in kinds:
            accepted = [url for url, (outcome, _) in zip(self.relays, outcomes, strict=True) if outcome == "ok"]
            return {"event": event, "relays": accepted}
        if "unknown" in kinds:
            # A relay took the note and never said so: do not sign and send a second copy.
            raise APIError(
                "No relay confirmed the note",
                platform=self.platform_name,
                ambiguous_write=True,
            )
        if "rejected" in kinds:
            reasons = {reason for outcome, reason in outcomes if outcome == "rejected"}
            limited = any(reason.startswith(("rate-limited", "blocked: rate")) for reason in reasons)
            raise PublishError(
                "Your relays are rate limiting this account. Try again later."
                if limited
                else "None of your relays accepted the note. Check the relay list and that it allows your key to post.",
                platform=self.platform_name,
                retryable=limited,
            )
        raise APIError("None of your relays could be reached", platform=self.platform_name, retryable=True)

    def _body(self, content: PublishContent) -> str:
        text = (content.text or "").strip()
        urls = [url for url in content.media_urls if url not in text]
        return "\n\n".join(part for part in [text, *urls] if part)

    def publish_post(self, access_token: str, content: PublishContent) -> PublishResult:
        try:
            secret = parse_secret_key(access_token)
        except ValueError as exc:
            raise OAuthError("The stored Nostr key is not valid", platform=self.platform_name) from exc
        body = self._body(content)
        if not body:
            raise PublishError("Nostr needs some text or media to post.", platform=self.platform_name, retryable=False)
        if len(content.text or "") > MAX_NOTE_LENGTH:
            raise PublishError(
                f"Nostr notes are limited to {MAX_NOTE_LENGTH} characters here.",
                platform=self.platform_name,
                retryable=False,
            )
        tags = self._note_tags(content, content.extra.get("_reply_tags") or [])
        sent = self._send(secret, 1, body, tags)
        event_id = sent["event"]["id"]
        return PublishResult(
            platform_post_id=event_id,
            url=f"https://njump.me/{bech32_encode('note', bytes.fromhex(event_id))}",
            extra={"relays": sent["relays"], "pubkey": sent["event"]["pubkey"]},
        )

    def publish_thread_part(
        self, access_token: str, content: PublishContent, *, root: PublishResult, parent: PublishResult
    ) -> PublishResult:
        """A reply note: a root marker on the first note, and a reply marker on the part before when it differs."""
        author = root.extra.get("pubkey") or ""
        reply_tags = [["e", root.platform_post_id, "", "root"]]
        if parent.platform_post_id != root.platform_post_id:
            reply_tags.append(["e", parent.platform_post_id, "", "reply"])
        if author:
            reply_tags.append(["p", author])
        return self.publish_post(access_token, replace(content, extra={**content.extra, "_reply_tags": reply_tags}))

    def delete_post(self, access_token: str, post_id: str) -> bool:
        """Ask the relays to delete the note (NIP-09; relays may keep it)."""
        secret = parse_secret_key(access_token)
        self._send(secret, 5, "", [["e", post_id], ["k", "1"]])
        return True
