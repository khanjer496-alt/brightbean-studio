import hashlib
import hmac

from apps.webhooks.services import generate_secret, sign_payload, verify_signature


def test_signature_is_hmac_sha256_of_timestamp_dot_body():
    header = sign_payload("whsec_abc", 1700000000, b'{"a":1}')
    expected = hmac.new(b"whsec_abc", b'1700000000.{"a":1}', hashlib.sha256).hexdigest()
    assert header == f"t=1700000000,v1={expected}"


def test_verify_accepts_a_good_signature():
    header = sign_payload("whsec_abc", 1700000000, b"body")
    assert verify_signature("whsec_abc", b"body", header, now=1700000100)


def test_verify_rejects_tampered_body_wrong_secret_and_garbage():
    header = sign_payload("whsec_abc", 1700000000, b"body")
    assert not verify_signature("whsec_abc", b"bodY", header, now=1700000000)
    assert not verify_signature("whsec_other", b"body", header, now=1700000000)
    assert not verify_signature("whsec_abc", b"body", "nonsense", now=1700000000)
    assert not verify_signature("whsec_abc", b"body", "t=abc,v1=00", now=1700000000)
    assert not verify_signature("whsec_abc", b"body", "t=1700000000", now=1700000000)


def test_verify_rejects_stale_timestamps_unless_tolerance_is_off():
    header = sign_payload("whsec_abc", 1700000000, b"body")
    assert not verify_signature("whsec_abc", b"body", header, now=1700000000 + 301)
    assert verify_signature("whsec_abc", b"body", header, tolerance=None, now=1700009999)


def test_generated_secrets_are_prefixed_and_unique():
    first, second = generate_secret(), generate_secret()
    assert first.startswith("whsec_") and first != second
