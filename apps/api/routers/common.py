"""Glue between the Ninja routers and :mod:`apps.api.ops`.

Every route built on it does the same things, in the same order as the older
routers: charge the HTTP rate limits, honour an ``Idempotency-Key`` header on
create routes, run the shared operation as the calling
:class:`~apps.api.ops.Actor`, map a refusal onto ``HttpError`` (the exception
handlers in ``apps.api.api`` give it the uniform error envelope), and write
the audit row.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import Any

from django.db import transaction
from ninja.errors import HttpError

from apps.api.limits import enforce_http_rate_limits
from apps.api.middleware import (
    claim_idempotency_slot,
    finalize_idempotent_response,
    fingerprint_request,
    log_audit_entry,
    release_idempotent_claim,
)
from apps.api.ops import Actor, OpError

#: ``IdempotencyRecord.key`` column width.
IDEMPOTENCY_KEY_MAX = 128


def run_op(
    request,
    op: Callable[..., Any],
    *args: Any,
    action: str,
    is_write: bool,
    target_id: uuid.UUID | Callable[[Any], uuid.UUID | None] | None = None,
    status_code: int = 200,
    idempotent: bool = False,
    **kwargs: Any,
) -> Any:
    """Run ``op`` for a route; with ``idempotent``, a retried ``Idempotency-Key`` replays the first response.

    Same claim → work → finalize/release contract as ``POST /posts/``: the
    work and the stored response commit together (a refusal rolls back
    anything the operation wrote), a refusal releases the claim so a
    corrected retry can go through, and reusing a key with a different body
    is a 422.
    """
    enforce_http_rate_limits(request, is_write=is_write)
    idempotency_key = (request.headers.get("Idempotency-Key") or None) if idempotent else None
    if idempotency_key is not None:
        replay = _claim(request, idempotency_key)
        if replay is not None:
            return replay
    try:
        with transaction.atomic():
            result = op(Actor.from_request(request), *args, **kwargs)
            if idempotency_key is not None:
                finalize_idempotent_response(
                    api_key=request.api_key,
                    idempotency_key=idempotency_key,
                    status_code=status_code,
                    body=result.model_dump(mode="json"),
                )
    except OpError as exc:
        release_idempotent_claim(api_key=request.api_key, idempotency_key=idempotency_key)
        if exc.refusal is not None:
            exc.refusal.log()  # the rollback above took the first entry with it
        raise HttpError(exc.status, exc.message) from exc
    except Exception:
        release_idempotent_claim(api_key=request.api_key, idempotency_key=idempotency_key)
        raise
    log_audit_entry(
        request,
        action=action,
        target_id=target_id(result) if callable(target_id) else target_id,
        status_code=status_code,
    )
    return result


def _claim(request, idempotency_key: str) -> dict | None:
    """Claim the key; the stored body when this is a replay, else ``None``."""
    if len(idempotency_key) > IDEMPOTENCY_KEY_MAX:
        raise HttpError(422, f"Idempotency-Key can be at most {IDEMPOTENCY_KEY_MAX} characters.")
    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        body = {}
    fingerprint = fingerprint_request(request.method or "POST", request.path, body)
    try:
        disposition, _status, replay_body = claim_idempotency_slot(
            api_key=request.api_key, idempotency_key=idempotency_key, fingerprint=fingerprint
        )
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc
    if disposition == "in_flight":
        raise HttpError(409, "An identical request with this Idempotency-Key is still in flight; retry shortly.")
    return replay_body if disposition == "replay" else None
