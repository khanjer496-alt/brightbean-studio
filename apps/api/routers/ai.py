"""``/api/v1/ai/*`` — the AI writing assistant, for agents.

Thin adapters over :mod:`apps.ai_writer.services`, the same functions the
composer's AI panel and the MCP ``generate_caption`` / ``adapt_caption``
tools call — so the brand voice, the plan's daily AI allowance and the
per-platform character limits apply identically everywhere.

Nothing is saved: the routes return text for the agent to pass to
``POST /posts/`` (as ``caption`` or ``platform_overrides``). They require
``create_posts``, and every ``social_account_id`` must be in the key's
allowlist.
"""

from __future__ import annotations

import uuid

from django.http import HttpRequest
from ninja import Router
from ninja.errors import HttpError

from apps.ai_writer.client import AIError
from apps.ai_writer.models import AIRequestLog
from apps.ai_writer.services import AIContext, adapt_for_networks, write_caption
from apps.api.limits import enforce_http_rate_limits
from apps.api.middleware import log_audit_entry
from apps.api.schemas import (
    AdaptCaptionRequest,
    AdaptCaptionResponse,
    AdaptedCaption,
    GenerateCaptionRequest,
    GenerateCaptionResponse,
)

router = Router(tags=["ai"])


def _require_perm(request: HttpRequest, key: str) -> None:
    membership = getattr(request, "workspace_membership", None)
    if membership is None or not membership.effective_permissions.get(key, False):
        raise HttpError(403, f"Permission denied: {key}")


def _allowlisted_accounts(request: HttpRequest, ids: list[uuid.UUID]) -> list:
    """The requested accounts, in order; 403 if any is outside the key's allowlist."""
    allowed = {sa.id: sa for sa in request.api_key.social_accounts.all()}  # type: ignore[attr-defined]
    missing = [str(i) for i in ids if i not in allowed]
    if missing:
        raise HttpError(403, f"social_account_id not in this key's allowlist: {', '.join(missing)}")
    return [allowed[i] for i in dict.fromkeys(ids)]


def _context(request: HttpRequest) -> AIContext:
    return AIContext(
        workspace=request.workspace,  # type: ignore[attr-defined]
        user=request.user if not request.user.is_anonymous else None,
        source=AIRequestLog.Source.API,
    )


def _ai_http_error(exc: AIError) -> HttpError:
    # 429 is reserved for our own quota envelope (``tier``, ``retry_after``…),
    # so a busy provider is reported as temporarily unavailable instead.
    status = 503 if exc.status_code == 429 else exc.status_code
    return HttpError(status, exc.message)


@router.post("/caption", response=GenerateCaptionResponse, summary="Write a caption with AI")
def generate_caption(request, payload: GenerateCaptionRequest):
    enforce_http_rate_limits(request, is_write=True)
    _require_perm(request, "create_posts")
    accounts = _allowlisted_accounts(request, payload.social_account_ids)
    try:
        caption = write_caption(_context(request), payload.prompt, accounts)
    except AIError as exc:
        raise _ai_http_error(exc) from exc
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc
    log_audit_entry(request, action="ai.caption.generate", target_id=None, status_code=200)
    return GenerateCaptionResponse(caption=caption)


@router.post("/adapt", response=AdaptCaptionResponse, summary="Adapt a caption for each network with AI")
def adapt_caption(request, payload: AdaptCaptionRequest):
    enforce_http_rate_limits(request, is_write=True)
    _require_perm(request, "create_posts")
    accounts = _allowlisted_accounts(request, payload.social_account_ids)
    try:
        adapted = adapt_for_networks(_context(request), payload.caption, accounts)
    except AIError as exc:
        raise _ai_http_error(exc) from exc
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc
    log_audit_entry(request, action="ai.caption.adapt", target_id=None, status_code=200)
    return AdaptCaptionResponse(
        captions=[
            AdaptedCaption(
                social_account_id=account.id,
                platform=account.platform,
                caption=adapted[str(account.id)],
                length=account.caption_wire_length(adapted[str(account.id)]),
                char_limit=account.char_limit,
            )
            for account in accounts
        ]
    )
