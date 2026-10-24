"""Meta Graph API error taxonomy, shared by Facebook, Instagram, Instagram Login and Threads.

Graph answers almost every failure with HTTP 400 and the real verdict in the
body: ``{"error": {"code", "error_subcode", "type", "is_transient", "fbtrace_id",
...}}``. The status says nothing about whether the token is dead, the app is
throttled, or the media is unusable, so the base ``_request`` handling (429 is a
rate limit, everything else is a generic ``APIError``) sent a revoked token and a
bad aspect ratio down the same three blind retries.

:class:`MetaGraphErrors` is mixed into the Meta providers and overrides the
``_error_for_response`` hook, so the generic stack (``error_messages``, the
publish engine) routes on the exception types it already knows and never has to
know the platform.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

import httpx

from .exceptions import APIError, AuthRejectedError, ProviderError, PublishError, RateLimitError

logger = logging.getLogger(__name__)

# 190 is the access-token family (subcodes 458 not authorized, 459 checkpoint,
# 460 password changed, 463 expired, 464 unconfirmed user, 467 invalid); 102 is an
# expired API session; 459/460/463/467/490 also arrive as top-level codes on some
# endpoints. Missing-permission codes (10, 200-299) are deliberately absent: they
# say one call lacks a scope, not that the whole grant is dead, and analytics and
# messaging tell those two apart by reading the body themselves.
RECONNECT_CODES = frozenset({102, 190, 459, 460, 463, 467, 490})
RECONNECT_SUBCODES = frozenset({458, 459, 460, 463, 464, 467})

# 4 app, 17 user, 32 page, 613 custom-endpoint call limits; 368 a temporary block
# on posting; 80001-80014 the business-use-case limits (Pages, Instagram, ...).
RATE_LIMIT_CODES = frozenset({4, 17, 32, 368, 613, *range(80001, 80015)})
# "You're posting too fast", surfaced under code 368 or with no useful code; and
# Instagram's cap on posts per 24 hours.
RATE_LIMIT_SUBCODES = frozenset({1390008, 2207042})
# 1 "unknown error" and 2 "service temporarily unavailable" are Meta asking for
# a retry.
TRANSIENT_CODES = frozenset({1, 2})

# How long to wait when Meta gives no hint of its own. App and user limits roll
# over hourly; a posting block and the daily cap last longer.
DEFAULT_RATE_LIMIT_SECONDS = 15 * 60
POSTING_BLOCK_SECONDS = 60 * 60
MAX_RATE_LIMIT_SECONDS = 24 * 60 * 60

# Code 100 with this subcode is a Meta-side fault on a GET of a just-created
# container or just-published media, not a verdict on the request.
READ_AFTER_WRITE_QUIRK = (100, 33)

_USAGE_HEADERS = ("x-business-use-case-usage", "x-app-usage", "x-ad-account-usage")
_CONTAINER_CODE = re.compile(r"\b(22070\d\d|3600\d)\b")


@dataclass(frozen=True)
class GraphError:
    """The parts of a Graph ``error`` object that decide what happens next."""

    code: int | None
    subcode: int | None
    type: str
    message: str
    transient: bool
    trace_id: str

    @property
    def reconnect(self) -> bool:
        return self.code in RECONNECT_CODES or (self.code == 190 and self.subcode in RECONNECT_SUBCODES)

    @property
    def rate_limited(self) -> bool:
        return self.code in RATE_LIMIT_CODES or self.subcode in RATE_LIMIT_SUBCODES


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_graph_error(body) -> GraphError | None:
    """The Graph ``error`` object in ``body``, or None when it isn't one.

    Tolerant of every shape the field takes in the wild: OAuth endpoints send a
    bare string and a few proxies send a list, neither of which is a Graph error.
    """
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return None
    return GraphError(
        code=_as_int(error.get("code")),
        subcode=_as_int(error.get("error_subcode")),
        type=str(error.get("type") or ""),
        message=str(error.get("message") or ""),
        transient=error.get("is_transient") is True,
        trace_id=str(error.get("fbtrace_id") or ""),
    )


def is_read_after_write_quirk(exc: Exception) -> bool:
    """Whether ``exc`` is Meta's code 100 / subcode 33 on a read of something just created."""
    error = parse_graph_error(getattr(exc, "raw_response", None))
    return error is not None and (error.code, error.subcode) == READ_AFTER_WRITE_QUIRK


def _usage_wait_seconds(headers: httpx.Headers) -> int | None:
    """How long Meta says the app has to wait, from its usage headers.

    Only the business-use-case header carries a figure
    (``estimated_time_to_regain_access``, in minutes); the others report a
    percentage, which is no help here.
    """
    for name in _USAGE_HEADERS:
        raw = headers.get(name)
        if not raw:
            continue
        try:
            usage = json.loads(raw)
        except ValueError:
            continue
        minutes = 0
        for entry in usage.values() if isinstance(usage, dict) else []:
            for item in entry if isinstance(entry, list) else [entry]:
                if isinstance(item, dict):
                    minutes = max(minutes, _as_int(item.get("estimated_time_to_regain_access")) or 0)
        if minutes:
            return minutes * 60
    return None


def _retry_after_seconds(response: httpx.Response, error: GraphError) -> int:
    waited = _as_int(response.headers.get("Retry-After")) or _usage_wait_seconds(response.headers)
    if waited is None:
        long_block = error.code == 368 or error.subcode in RATE_LIMIT_SUBCODES
        waited = POSTING_BLOCK_SECONDS if long_block else DEFAULT_RATE_LIMIT_SECONDS
    return min(max(waited, 1), MAX_RATE_LIMIT_SECONDS)


@dataclass(frozen=True)
class ContainerFault:
    """What a media-container error code means, in words for the person who posted."""

    message: str
    retryable: bool = False


_FORMAT = ContainerFault(
    "Instagram doesn't support this media format. Use a JPEG photo or an MP4 video (H.264 video, AAC audio)."
)
_RATIO = ContainerFault("Instagram doesn't accept this aspect ratio. Photos must be between 4:5 and 1.91:1.")
_TOO_LARGE = ContainerFault("The file is too large for Instagram. Photos can be up to 8 MB; use a smaller file.")
_CAPTION = ContainerFault(
    "Instagram rejected the caption. It can be 2,200 characters at most, with up to 30 hashtags and 20 @mentions."
)
_RESTRICTED = ContainerFault(
    "Instagram has restricted this account from posting right now. Check the Instagram app for a notice."
)
_EXPIRED = ContainerFault("Instagram discarded the prepared media before it was published.", True)
_UNAVAILABLE = ContainerFault("Instagram had a temporary problem preparing the media.", True)
_FETCH = ContainerFault("Instagram could not download the media from our server.", True)

# Meta's documented content-publishing errors; 3600x are the older codes the same
# faults used to arrive under. A code not listed keeps the default handling
# rather than being guessed at. Only the faults a retry can fix are retryable.
CONTAINER_FAULTS: dict[int, ContainerFault] = {
    36000: _TOO_LARGE,
    36001: _FORMAT,
    36003: _RATIO,
    36004: _CAPTION,
    2207001: _UNAVAILABLE,
    2207003: ContainerFault(
        "Instagram timed out downloading the media from our server. Try a smaller file, then publish again."
    ),
    2207004: _TOO_LARGE,
    2207005: _FORMAT,
    2207006: _EXPIRED,
    2207008: _EXPIRED,
    2207009: _RATIO,
    2207010: _CAPTION,
    2207020: _EXPIRED,
    2207026: _FORMAT,
    2207027: _UNAVAILABLE,
    2207032: _UNAVAILABLE,
    2207050: _RESTRICTED,
    2207051: _RESTRICTED,
    2207052: _FETCH,
    2207053: _UNAVAILABLE,
}


def container_fault(code: int | None) -> ContainerFault | None:
    return CONTAINER_FAULTS.get(code) if code is not None else None


def container_status_error(status: str, *, platform: str, raw_response: dict | None = None) -> PublishError:
    """The error for a container whose status poll came back ``ERROR``.

    ``status`` is Meta's free text (``"Error: ... error code 2207003 ..."``), the
    only place the code appears on this path.
    """
    match = _CONTAINER_CODE.search(status or "")
    fault = container_fault(int(match.group(1))) if match else None
    if fault is None:
        return PublishError(
            f"{platform} container failed: {status or 'unknown error'}", platform=platform, raw_response=raw_response
        )
    return PublishError(fault.message, platform=platform, raw_response=raw_response, retryable=fault.retryable)


class MetaGraphErrors:
    """Mixin for providers that talk to the Graph API: classifies its error bodies.

    Goes before ``SocialProvider`` in the bases so ``super()`` reaches the base
    ``_error_for_response`` for everything this does not recognise. The contract
    that method states still holds: every result is an ``APIError`` carrying
    ``status_code`` or a ``RateLimitError``.
    """

    def _error_for_response(self, response: httpx.Response) -> ProviderError:
        base = super()._error_for_response(response)  # type: ignore[misc]
        try:
            error = parse_graph_error(response.json())
        except ValueError:
            return base
        if error is None:
            return base

        logger.warning(
            "%s Graph error: status=%s code=%s subcode=%s type=%s transient=%s fbtrace_id=%s",
            self.platform_name,  # type: ignore[attr-defined]
            response.status_code,
            error.code,
            error.subcode,
            error.type,
            error.transient,
            error.trace_id,
        )
        common = {"platform": base.platform, "raw_response": base.raw_response}
        message = str(base)

        if error.reconnect:
            return AuthRejectedError(message, status_code=response.status_code, **common)
        if error.rate_limited:
            return RateLimitError(message, retry_after=_retry_after_seconds(response, error), **common)

        if isinstance(base, RateLimitError):
            # An HTTP 429 whose body names no Graph limit stays the plain rate limit.
            return base

        fault = container_fault(error.subcode) or container_fault(error.code)
        if fault is not None:
            return APIError(
                message,
                status_code=response.status_code,
                retryable=fault.retryable,
                transient=fault.retryable,
                user_message=fault.message,
                **common,
            )
        if error.transient or error.code in TRANSIENT_CODES:
            return APIError(message, status_code=response.status_code, transient=True, **common)
        return base
