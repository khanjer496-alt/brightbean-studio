"""Endpoint management, signing and delivery for outbound webhooks.

Delivery is database-driven, like the notification retry sweep: ``emit`` writes
one ``WebhookDelivery`` row per subscribed endpoint, a background task makes the
first attempt, and a one-minute sweep makes every later one when
``next_attempt_at`` comes due. A row is claimed by pushing ``next_attempt_at``
forward under a conditional UPDATE, so the task and the sweep (or two sweeps)
can never send the same delivery at once, and a worker that dies mid-send
leaves a row the sweep picks up again once the claim lapses.
"""

import hashlib
import hmac
import json
import logging
import secrets
import time
import uuid
from datetime import timedelta
from urllib.parse import urlsplit, urlunsplit

import httpx
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from apps.common.validators import is_safe_url, resolve_public_ip

from .models import SUBSCRIBABLE_EVENT_TYPES, DeliveryStatus, EventType, WebhookDelivery, WebhookEndpoint

logger = logging.getLogger(__name__)

SIGNATURE_HEADER = "PostDelegate-Signature"
EVENT_HEADER = "PostDelegate-Event"
DELIVERY_HEADER = "PostDelegate-Delivery"

# Version of the envelope and ``data`` shapes, sent as ``api_version`` in every
# payload. Bump it (and keep the old shape for existing endpoints) on any
# breaking change; adding a field is not one.
API_VERSION = "2026-10-01"

# Either of these lets a member manage the workspace's webhooks: owners hold
# both, managers hold the second (they already connect the workspace's
# integrations), and a custom role can grant either.
MANAGE_PERMISSIONS = ("manage_workspace_settings", "manage_social_accounts")

SECRET_PREFIX = "whsec_"
REQUEST_TIMEOUT_SECONDS = 10.0
# Seconds to wait after failed attempt 1, 2, ... : 1m, 5m, 30m, 2h, 12h. One
# more attempt than there are waits, so a delivery is tried six times in all.
RETRY_BACKOFF_SECONDS = [60, 5 * 60, 30 * 60, 2 * 60 * 60, 12 * 60 * 60]
MAX_ATTEMPTS = len(RETRY_BACKOFF_SECONDS) + 1
# A claimed row that is not settled within this long is treated as abandoned.
CLAIM_LEASE_SECONDS = 2 * 60
# Failed attempts in a row (across deliveries) before an endpoint is switched off.
AUTO_DISABLE_THRESHOLD = 15
MAX_ENDPOINTS_PER_WORKSPACE = 20
RESPONSE_BODY_LIMIT = 2000
ERROR_LIMIT = 500
# Signatures older than this are refused by ``verify_signature``.
DEFAULT_TOLERANCE_SECONDS = 5 * 60


class WebhookError(ValueError):
    """A request to create or change an endpoint that cannot be honoured."""


def can_manage(effective_permissions) -> bool:
    return any(effective_permissions.get(key, False) for key in MANAGE_PERMISSIONS)


def key_covers_workspace(api_key) -> bool:
    """Whether an API key may manage webhooks: its account allowlist must cover the whole workspace.

    Events are workspace-wide (every account's posts and inbox messages), so a
    key scoped to some accounts could otherwise use a webhook to read the rest.
    """
    from apps.social_accounts.models import SocialAccount

    allowed = {account.id for account in api_key.social_accounts.all()}
    workspace_accounts = SocialAccount.objects.for_workspace(api_key.workspace_id).values_list("id", flat=True)
    return all(account_id in allowed for account_id in workspace_accounts)


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------


def sign_payload(secret: str, timestamp: int, body: bytes) -> str:
    """The ``PostDelegate-Signature`` value: ``t=<ts>,v1=<hmac-sha256 of "ts.body">``."""
    signed = f"{timestamp}.".encode() + body
    digest = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def verify_signature(
    secret: str,
    body: bytes,
    header: str,
    *,
    tolerance: int | None = DEFAULT_TOLERANCE_SECONDS,
    now: float | None = None,
) -> bool:
    """Check a ``PostDelegate-Signature`` header against the raw request body.

    The reference verifier for receivers (also shown in the docs). Constant-time
    compare, and the timestamp must be within ``tolerance`` seconds of ``now`` so
    a captured request cannot be replayed later; pass ``tolerance=None`` to skip.
    """
    try:
        parts = dict(item.strip().split("=", 1) for item in (header or "").split(","))
        timestamp = int(parts["t"])
    except (KeyError, ValueError):
        return False
    if tolerance is not None and abs((time.time() if now is None else now) - timestamp) > tolerance:
        return False
    expected = sign_payload(secret, timestamp, body).split("v1=", 1)[1]
    # Bytes, so a non-ASCII header is a mismatch rather than a TypeError.
    return hmac.compare_digest(expected.encode(), parts.get("v1", "").encode("utf-8", errors="replace"))


def generate_secret() -> str:
    return SECRET_PREFIX + secrets.token_urlsafe(32)


# ---------------------------------------------------------------------------
# Endpoint management
# ---------------------------------------------------------------------------


def validate_endpoint_url(url: str) -> str:
    """Return the cleaned URL, or raise ``WebhookError`` if it is not a public http(s) endpoint."""
    url = (url or "").strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise WebhookError("Enter a full URL starting with https:// (or http://).")
    if parts.username or parts.password:
        raise WebhookError("Remove the username and password from the URL.")
    if not is_safe_url(url):
        raise WebhookError("That URL does not point to a public server, so we cannot deliver to it.")
    return url


def clean_event_types(event_types) -> list[str]:
    if not isinstance(event_types, (list, tuple)):
        raise WebhookError("Choose at least one event type.")
    unknown = sorted({e for e in event_types if e not in SUBSCRIBABLE_EVENT_TYPES})
    if unknown:
        raise WebhookError(f"Unknown event type: {', '.join(map(str, unknown))}.")
    chosen = [e for e in SUBSCRIBABLE_EVENT_TYPES if e in event_types]
    if not chosen:
        raise WebhookError("Choose at least one event type.")
    return chosen


def create_endpoint(workspace, *, url, event_types, description="", is_enabled=True, created_by=None):
    """Create an endpoint. Returns ``(endpoint, plaintext_secret)``; the secret is never retrievable again."""
    url = validate_endpoint_url(url)
    event_types = clean_event_types(event_types)
    if WebhookEndpoint.objects.filter(workspace=workspace).count() >= MAX_ENDPOINTS_PER_WORKSPACE:
        raise WebhookError(f"A workspace can have at most {MAX_ENDPOINTS_PER_WORKSPACE} webhook endpoints.")
    secret = generate_secret()
    endpoint = WebhookEndpoint.objects.create(
        workspace=workspace,
        url=url,
        description=description.strip()[:255],
        is_enabled=is_enabled,
        event_types=event_types,
        secret=secret,
        secret_hint=secret[-4:],
        created_by=created_by,
    )
    return endpoint, secret


def update_endpoint(endpoint, *, url=None, event_types=None, description=None, is_enabled=None):
    """Apply the given changes. Enabling an endpoint clears its failure streak and auto-disable state."""
    fields = ["updated_at"]
    if url is not None and url != endpoint.url:
        endpoint.url = validate_endpoint_url(url)
        fields.append("url")
    if event_types is not None:
        endpoint.event_types = clean_event_types(event_types)
        fields.append("event_types")
    if description is not None:
        endpoint.description = description.strip()[:255]
        fields.append("description")
    if is_enabled is not None and is_enabled != endpoint.is_enabled:
        endpoint.is_enabled = is_enabled
        fields.append("is_enabled")
        if is_enabled:
            endpoint.consecutive_failures = 0
            endpoint.disabled_at = None
            endpoint.disabled_reason = ""
            fields += ["consecutive_failures", "disabled_at", "disabled_reason"]
    endpoint.save(update_fields=fields)
    return endpoint


def rotate_secret(endpoint) -> str:
    """Replace the signing secret. Returns the new plaintext, shown once."""
    secret = generate_secret()
    endpoint.secret = secret
    endpoint.secret_hint = secret[-4:]
    endpoint.save(update_fields=["secret", "secret_hint", "updated_at"])
    return secret


# ---------------------------------------------------------------------------
# Emitting
# ---------------------------------------------------------------------------


def build_envelope(event_type: str, workspace, data: dict, *, event_id: uuid.UUID, created_at=None) -> dict:
    return {
        "id": str(event_id),
        "type": event_type,
        "api_version": API_VERSION,
        "created_at": (created_at or timezone.now()).isoformat(),
        "workspace": {"id": str(workspace.id), "name": workspace.name},
        "data": data,
    }


def emit(event_type: str, workspace, data: dict, *, event_id: uuid.UUID | None = None) -> list[WebhookDelivery]:
    """Queue ``event_type`` for every enabled endpoint of ``workspace`` that subscribes to it.

    Never raises: this is called from publishing, approval and inbox paths that
    must not fail because a webhook could not be queued. The delivery rows are
    written inside the caller's transaction (a rolled-back change emits
    nothing); the first attempt is queued once it commits. Emitting the same
    ``event_id`` twice does not create a second delivery.
    """
    try:
        endpoints = list(
            WebhookEndpoint.objects.filter(
                workspace_id=workspace.id, is_enabled=True, event_types__contains=[event_type]
            )
        )
        if not endpoints:
            return []
        event_id = event_id or uuid.uuid4()
        envelope = build_envelope(event_type, workspace, data, event_id=event_id)
        return [d for d in (_queue_delivery(endpoint, envelope) for endpoint in endpoints) if d is not None]
    except Exception:
        logger.exception("Could not queue webhook event %s for workspace %s", event_type, workspace.id)
        return []


def _queue_delivery(endpoint, envelope: dict) -> WebhookDelivery | None:
    with transaction.atomic():
        delivery, created = WebhookDelivery.objects.get_or_create(
            endpoint=endpoint,
            event_id=envelope["id"],
            defaults={
                "event_type": envelope["type"],
                "payload": envelope,
                "next_attempt_at": timezone.now(),
            },
        )
    if not created:
        return None
    _enqueue_first_attempt(delivery)
    return delivery


def _enqueue_first_attempt(delivery) -> None:
    from .tasks import deliver_webhook

    delivery_id = str(delivery.id)
    # If this never runs (a crash between commit and enqueue), the sweep finds
    # the row anyway: it is already due.
    transaction.on_commit(lambda: deliver_webhook(delivery_id))


def send_test_event(endpoint, *, requested_by=None) -> WebhookDelivery:
    """Queue a ``webhook.test`` ping to one endpoint, whether or not it is enabled."""
    data = {"message": "This is a test event from PostDelegate."}
    if requested_by is not None:
        data["requested_by"] = requested_by.email
    envelope = build_envelope(EventType.WEBHOOK_TEST, endpoint.workspace, data, event_id=uuid.uuid4())
    return _queue_delivery(endpoint, envelope)


def resend(delivery) -> WebhookDelivery:
    """Send a delivery again, as the same delivery id with the same payload, starting a fresh retry schedule.

    Refused (``WebhookError``) while an attempt may be in flight: a claimed row
    has ``next_attempt_at`` at most one lease ahead, so only a row that is
    settled, or waiting longer than that for its next retry, is safe to reset
    without the two sends overlapping.
    """
    now = timezone.now()
    reset = (
        WebhookDelivery.objects.filter(pk=delivery.pk)
        .exclude(
            status=DeliveryStatus.PENDING,
            next_attempt_at__lte=now + timedelta(seconds=CLAIM_LEASE_SECONDS),
        )
        .update(status=DeliveryStatus.PENDING, attempts=0, next_attempt_at=now)
    )
    if not reset:
        raise WebhookError("This delivery is being sent or retried within the next few minutes already.")
    delivery.refresh_from_db()
    _enqueue_first_attempt(delivery)
    return delivery


# ---------------------------------------------------------------------------
# Delivering
# ---------------------------------------------------------------------------


def _pinned(url: str, ip: str) -> tuple[str, dict, dict]:
    """``url`` rewritten to connect to ``ip``, plus the Host header and TLS name that keep it addressed to the host."""
    parts = urlsplit(url)
    host = f"[{ip}]" if ":" in ip else ip
    netloc = f"{host}:{parts.port}" if parts.port else host
    host_header = f"{parts.hostname}:{parts.port}" if parts.port else parts.hostname
    extensions = {"sni_hostname": parts.hostname} if parts.scheme == "https" else {}
    return urlunsplit(parts._replace(netloc=netloc)), {"Host": host_header}, extensions


def _send_request(url: str, body: bytes, headers: dict, *, pinned_ip: str | None = None, transport=None):
    """POST ``body`` and return ``(status_code, truncated response text)``.

    With ``pinned_ip`` the connection goes to that address (the one that was
    just checked to be public) rather than resolving the name again, so a DNS
    answer that changes between the check and the connect cannot point the
    request at an internal host. The certificate is still checked against the
    hostname. Redirects are never followed: a 302 to a private address is the
    classic way around URL validation, so any redirect is reported as a failure.
    """
    extensions = {}
    if pinned_ip:
        url, host_header, extensions = _pinned(url, pinned_ip)
        headers = {**headers, **host_header}
    client = httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=False, transport=transport)
    with client, client.stream("POST", url, content=body, headers=headers, extensions=extensions) as response:
        received = bytearray()
        for chunk in response.iter_bytes():
            received += chunk
            if len(received) >= RESPONSE_BODY_LIMIT * 4:
                break
        text = bytes(received).decode("utf-8", errors="replace")
        return response.status_code, text[:RESPONSE_BODY_LIMIT]


def request_headers(delivery, body: bytes, *, now: int | None = None) -> dict:
    timestamp = int(time.time()) if now is None else now
    return {
        "Content-Type": "application/json",
        "User-Agent": "PostDelegate-Webhooks/1",
        SIGNATURE_HEADER: sign_payload(delivery.endpoint.secret, timestamp, body),
        EVENT_HEADER: delivery.event_type,
        DELIVERY_HEADER: str(delivery.id),
    }


def encode_payload(payload: dict) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str).encode("utf-8")


def _claim(delivery_id) -> WebhookDelivery | None:
    now = timezone.now()
    claimed = WebhookDelivery.objects.filter(
        pk=delivery_id, status=DeliveryStatus.PENDING, next_attempt_at__lte=now
    ).update(next_attempt_at=now + timedelta(seconds=CLAIM_LEASE_SECONDS))
    if not claimed:
        return None
    return WebhookDelivery.objects.select_related("endpoint", "endpoint__workspace").get(pk=delivery_id)


def attempt_delivery(delivery_id) -> bool:
    """Make one attempt for a due delivery. Returns True if a request was made."""
    delivery = _claim(delivery_id)
    if delivery is None:
        return False
    endpoint = delivery.endpoint

    if not endpoint.is_enabled and delivery.event_type != EventType.WEBHOOK_TEST:
        _settle_failure(delivery, status_code=None, duration_ms=None, error="Endpoint is disabled.", final=True)
        return False

    # Checked again now, not just when the endpoint was saved: DNS can change
    # between the two. The request then goes to the address checked here.
    pinned_ip = resolve_public_ip(endpoint.url)
    if pinned_ip is None:
        _settle_failure(
            delivery,
            status_code=None,
            duration_ms=None,
            error="URL rejected: it must be a public http(s) endpoint.",
            final=True,
        )
        return False

    body = encode_payload(delivery.payload)
    started = time.monotonic()
    status_code, response_text, error = None, "", ""
    try:
        status_code, response_text = _send_request(
            endpoint.url, body, request_headers(delivery, body), pinned_ip=pinned_ip
        )
    except httpx.TimeoutException:
        error = f"No response within {int(REQUEST_TIMEOUT_SECONDS)} seconds."
    except httpx.HTTPError as exc:
        error = f"{type(exc).__name__}: {exc}"[:ERROR_LIMIT]
    duration_ms = int((time.monotonic() - started) * 1000)

    if status_code is not None and 200 <= status_code < 300:
        _settle_success(delivery, status_code, duration_ms, response_text)
        return True
    if status_code is not None:
        error = (
            f"Redirects are not followed (HTTP {status_code})." if 300 <= status_code < 400 else f"HTTP {status_code}"
        )
    _settle_failure(
        delivery, status_code=status_code, duration_ms=duration_ms, error=error, response_text=response_text
    )
    return True


def _settle_success(delivery, status_code, duration_ms, response_text) -> None:
    now = timezone.now()
    # Only a row still pending is settled: an attempt that outlived its lease
    # must not overwrite the outcome of the attempt that replaced it.
    WebhookDelivery.objects.filter(pk=delivery.pk, status=DeliveryStatus.PENDING).update(
        status=DeliveryStatus.DELIVERED,
        attempts=delivery.attempts + 1,
        last_attempt_at=now,
        delivered_at=now,
        next_attempt_at=None,
        last_status_code=status_code,
        last_duration_ms=duration_ms,
        last_error="",
        response_body=response_text,
    )
    WebhookEndpoint.objects.filter(pk=delivery.endpoint_id).exclude(consecutive_failures=0).update(
        consecutive_failures=0
    )


def _settle_failure(delivery, *, status_code, duration_ms, error, response_text="", final=False) -> None:
    attempts = delivery.attempts + 1
    exhausted = final or attempts >= MAX_ATTEMPTS
    now = timezone.now()
    WebhookDelivery.objects.filter(pk=delivery.pk, status=DeliveryStatus.PENDING).update(
        status=DeliveryStatus.FAILED if exhausted else DeliveryStatus.PENDING,
        attempts=attempts,
        last_attempt_at=now,
        next_attempt_at=None if exhausted else now + timedelta(seconds=RETRY_BACKOFF_SECONDS[attempts - 1]),
        last_status_code=status_code,
        last_duration_ms=duration_ms,
        last_error=error[:ERROR_LIMIT],
        response_body=response_text,
    )
    if final and status_code is None and duration_ms is None:
        # Never sent, so it says nothing about whether the receiver is healthy.
        return
    _count_failure(delivery.endpoint_id)


def _count_failure(endpoint_id) -> None:
    WebhookEndpoint.objects.filter(pk=endpoint_id).update(consecutive_failures=F("consecutive_failures") + 1)
    endpoint = WebhookEndpoint.objects.select_related("workspace").get(pk=endpoint_id)
    if endpoint.is_enabled and endpoint.consecutive_failures >= AUTO_DISABLE_THRESHOLD:
        disabled = WebhookEndpoint.objects.filter(pk=endpoint_id, is_enabled=True).update(
            is_enabled=False,
            disabled_at=timezone.now(),
            disabled_reason=f"Disabled automatically after {endpoint.consecutive_failures} failed deliveries in a row.",
        )
        if disabled:
            _notify_disabled(endpoint)


def _notify_disabled(endpoint) -> None:
    """Tell the workspace's owners their endpoint was switched off. Best-effort."""
    try:
        from apps.members.models import WorkspaceMembership
        from apps.notifications.engine import notify
        from apps.notifications.models import EventType as NotificationEventType

        owners = WorkspaceMembership.objects.filter(
            workspace=endpoint.workspace, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
        ).select_related("user")
        for membership in owners:
            notify(
                user=membership.user,
                event_type=NotificationEventType.WEBHOOK_DISABLED,
                title="A webhook endpoint was turned off",
                body=(
                    f"{endpoint.url} failed {AUTO_DISABLE_THRESHOLD} deliveries in a row, so we stopped sending to it. "
                    "Fix the receiver, then turn it back on in the workspace's webhook settings."
                ),
                data={"endpoint_id": str(endpoint.id), "workspace_id": str(endpoint.workspace_id)},
            )
    except Exception:
        logger.exception("Could not notify the owners that webhook endpoint %s was disabled", endpoint.id)


def due_delivery_ids(limit: int) -> list:
    return list(
        WebhookDelivery.objects.filter(status=DeliveryStatus.PENDING, next_attempt_at__lte=timezone.now())
        .order_by("next_attempt_at")
        .values_list("id", flat=True)[:limit]
    )
