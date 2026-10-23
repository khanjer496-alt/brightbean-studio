"""Meta's data deletion callback: parse and verify the signed request, then delete.

Format (checked 2026-10-23):
https://developers.facebook.com/docs/development/create-an-app/app-dashboard/data-deletion-callback
A POST with a ``signed_request`` field of ``<base64url signature>.<base64url payload>``
(unpadded); the signature is HMAC-SHA256 of the still-encoded payload under the
app secret, and the payload is JSON with ``algorithm``, ``issued_at``,
``expires`` and an app-scoped ``user_id``. The response is JSON with a ``url``
to check status and a ``confirmation_code``.

The deployment can run several Meta apps (Facebook Login, Instagram Login,
Threads) and organizations can bring their own, so every configured secret is
tried. A request signed with an organization's own secret only reaches that
organization's accounts: the organization knows that secret and could
otherwise forge deletions for anyone.
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

from apps.credentials.models import app_secret_owners
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


class VerifiedRequest(dict):
    """The verified payload; ``organization_id`` is set when an organization's own app signed it."""

    organization_id = None


def parse_signed_request(signed_request: str, secret_owners=None) -> VerifiedRequest:
    """Return the verified payload, or raise InvalidSignedRequestError."""
    if not signed_request or "." not in signed_request:
        raise InvalidSignedRequestError("Malformed signed_request.")
    encoded_signature, encoded_payload = signed_request.split(".", 1)
    signature = _b64url_decode(encoded_signature)
    owners = app_secret_owners(*META_PLATFORMS) if secret_owners is None else secret_owners
    # Every candidate is compared, so timing does not reveal which one matched.
    matches = [
        organization_id
        for secret, organization_id in owners
        if secret
        and hmac.compare_digest(hmac.new(secret.encode(), encoded_payload.encode(), hashlib.sha256).digest(), signature)
    ]
    if not matches:
        raise InvalidSignedRequestError("Signature does not match.")
    try:
        payload = json.loads(_b64url_decode(encoded_payload))
    except ValueError as exc:
        raise InvalidSignedRequestError("Payload is not JSON.") from exc
    if not isinstance(payload, dict) or str(payload.get("algorithm", "")).upper() != "HMAC-SHA256":
        raise InvalidSignedRequestError("Unsupported algorithm.")
    if not payload.get("user_id") or not isinstance(payload["user_id"], str | int):
        raise InvalidSignedRequestError("No user_id.")
    issued_at = payload.get("issued_at")
    if isinstance(issued_at, int) and time.time() - issued_at > MAX_REQUEST_AGE_SECONDS:
        raise InvalidSignedRequestError("Request is too old.")
    verified = VerifiedRequest(payload)
    # The deployment's own app (None) is unscoped; otherwise the first owner.
    verified.organization_id = None if None in matches else matches[0]
    return verified


def accounts_for_meta_user(user_id: str, organization_id=None):
    accounts = SocialAccount.objects.filter(
        Q(meta_user_id=user_id) | Q(platform__in=META_PLATFORMS, account_platform_id=user_id)
    )
    if organization_id is not None:
        accounts = accounts.filter(workspace__organization_id=organization_id)
    return accounts.select_related("workspace")


def delete_for_meta_user(payload: VerifiedRequest) -> DataDeletionRequest:
    """Delete the data of the user in a verified payload; idempotent per signed request."""
    user_id = str(payload["user_id"])
    organization_id = getattr(payload, "organization_id", None)
    request_key = hashlib.sha256(
        f"meta:{organization_id or ''}:{user_id}:{payload.get('issued_at', '')}".encode()
    ).hexdigest()
    existing = DataDeletionRequest.objects.filter(request_key=request_key).first()
    if existing:
        return existing

    removed = 0
    for account in list(accounts_for_meta_user(user_id, organization_id)):
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
