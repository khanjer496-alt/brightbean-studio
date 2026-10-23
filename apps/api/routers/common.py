"""Glue between the Ninja routers and :mod:`apps.api.ops`.

Every route built on it does the same four things, in the same order as the
older routers: charge the HTTP rate limits, run the shared operation as the
calling :class:`~apps.api.ops.Actor`, map a refusal onto ``HttpError`` (the
exception handlers in ``apps.api.api`` give it the uniform error envelope),
and write the audit row.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from ninja.errors import HttpError

from apps.api.limits import enforce_http_rate_limits
from apps.api.middleware import log_audit_entry
from apps.api.ops import Actor, OpError


def run_op(
    request,
    op: Callable[..., Any],
    *args: Any,
    action: str,
    is_write: bool,
    target_id: uuid.UUID | Callable[[Any], uuid.UUID | None] | None = None,
    status_code: int = 200,
    **kwargs: Any,
) -> Any:
    enforce_http_rate_limits(request, is_write=is_write)
    try:
        result = op(Actor.from_request(request), *args, **kwargs)
    except OpError as exc:
        raise HttpError(exc.status, exc.message) from exc
    log_audit_entry(
        request,
        action=action,
        target_id=target_id(result) if callable(target_id) else target_id,
        status_code=status_code,
    )
    return result
