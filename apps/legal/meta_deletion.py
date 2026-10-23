"""Meta's data deletion callback: parse and verify the signed request, then delete.

Format: https://developers.facebook.com/docs/development/create-an-app/app-dashboard/data-deletion-callback
A POST with a ``signed_request`` field of ``<base64url signature>.<base64url payload>``;
the signature is HMAC-SHA256 of the encoded payload under the app secret, and the
payload is JSON with ``algorithm``, ``issued_at`` and ``user_id``. The response is
JSON with a ``url`` to check status and a ``confirmation_code``.
"""

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import time

from django.db import IntegrityError, transaction
from django.db.models import Q

from apps.credentials.models import resolve_app_secrets
from apps.social_accounts.models import SocialAccount
from apps.social_accounts.removal import remove_account

from .models import DataDeletionRequest

META_PLATFORMS = ("facebook", "instagram", "instagram_login", "threads")
# Meta retries for a while after a failure; anything older is not a retry.
MAX_REQUEST_AGE_SECONDS = 7 * 24 * 60 * 60


class InvalidSignedRequestError(Exception):
    pass


def _b64url_decode(value: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError) as exc:
        raise InvalidSignedRequestError("Malformed encoding.") from exc


def parse_signed_request(signed_request: str, secrets_to_try=None) -> dict:
    """Return the verified payload, or raise InvalidSignedRequestError."""
    if not signed_request or "." not in signed_request:
        raise InvalidSignedRequestError("Malformed signed_request.")
    encoded_signature, encoded_payload = signed_request.split(".", 1)
    signature = _b64url_decode(encoded_signature)
    candidates = resolve_app_secrets(*META_PLATFORMS) if secrets_to_try is None else secrets_to_try
    verified = any(
        hmac.compare_digest(hmac.new(secret.encode(), encoded_payload.encode(), hashlib.sha256).digest(), signature)
        for secret in candidates
        if secret
    )
    if not verified:
        raise InvalidSignedRequestError("Signature does not match.")
    try:
        payload = json.loads(_b64url_decode(encoded_payload))
    except ValueError as exc:
        raise InvalidSignedRequestError("Payload is not JSON.") from exc
    if not isinstance(payload, dict) or str(payload.get("algorithm", "")).upper() != "HMAC-SHA256":
        raise InvalidSignedRequestError("Unsupported algorithm.")
    if not payload.get("user_id"):
        raise InvalidSignedRequestError("No user_id.")
    issued_at = payload.get("issued_at")
    if isinstance(issued_at, int) and time.time() - issued_at > MAX_REQUEST_AGE_SECONDS:
        raise InvalidSignedRequestError("Request is too old.")
    return payload


def accounts_for_meta_user(user_id: str):
    return SocialAccount.objects.filter(
        Q(meta_user_id=user_id) | Q(platform__in=META_PLATFORMS, account_platform_id=user_id)
    ).select_related("workspace")


def delete_for_meta_user(payload: dict) -> DataDeletionRequest:
    """Delete the data of the user in a verified payload; idempotent per signed request."""
    user_id = str(payload["user_id"])
    request_key = hashlib.sha256(f"meta:{user_id}:{payload.get('issued_at', '')}".encode()).hexdigest()
    existing = DataDeletionRequest.objects.filter(request_key=request_key).first()
    if existing:
        return existing

    removed = 0
    for account in list(accounts_for_meta_user(user_id)):
        remove_account(account)
        removed += 1
    try:
        with transaction.atomic():
            return DataDeletionRequest.objects.create(
                code=secrets.token_hex(12),
                source=DataDeletionRequest.Source.META,
                external_user_id=user_id,
                request_key=request_key,
                accounts_removed=removed,
            )
    except IntegrityError:
        return DataDeletionRequest.objects.get(request_key=request_key)
