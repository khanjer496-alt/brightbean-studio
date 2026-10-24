"""Check that a platform that fetches media by URL will be able to.

Meta, Pinterest and Google download the file from the address we hand them.
When that address is private (a local ``APP_URL``), answers 404/403, serves the
wrong type, or is a signed link about to expire, the platform's own error is
vague ("media fetch failed", 2207052). Checking first turns it into a sentence
the user can act on, and spares a container, an upload and the retries.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import httpx
from django.conf import settings

from apps.common.validators import is_safe_url
from providers.exceptions import PublishError

logger = logging.getLogger(__name__)

PROBE_TIMEOUT = 10.0
# A platform fetches the file when the container is created, then may poll for minutes.
MIN_SIGNED_URL_LIFETIME = 15 * 60

_TYPE_PREFIXES = {"image": ("image/",), "gif": ("image/",), "video": ("video/",), "document": ("application/pdf",)}
# Object stores answer this when the upload carried no content type; the platform sniffs the bytes.
_GENERIC_TYPES = ("application/octet-stream", "binary/octet-stream")


def check_public_media_urls(urls: list[str], media_types: list[str], *, platform: str) -> None:
    """Raise a ``PublishError`` naming the first of ``urls`` the platform could not download.

    Switched off by ``PUBLISH_MEDIA_PREFLIGHT = False``. Uses a one-byte ranged
    GET rather than HEAD: signed object-store links are valid for GET only, so a
    HEAD on a perfectly good link answers 403.
    """
    if not getattr(settings, "PUBLISH_MEDIA_PREFLIGHT", True):
        return
    for index, url in enumerate(urls):
        kind = media_types[index] if index < len(media_types) else ""
        _check_one(url, kind, platform=platform, position=index + 1, total=len(urls))


def _fail(message: str, platform: str, *, retryable: bool = False) -> PublishError:
    return PublishError(message, platform=platform, retryable=retryable)


def _check_one(url: str, kind: str, *, platform: str, position: int, total: int) -> None:
    which = f"Media item {position} of {total}" if total > 1 else "The media"

    if not is_safe_url(url):
        raise _fail(
            f"{which} is not at a public web address ({urlparse(url).hostname or 'unknown host'}), so {platform} "
            "cannot download it. Set APP_URL to the public https address of this site, or serve media from "
            "public storage.",
            platform,
        )

    left = _signed_url_seconds_left(url)
    if left is not None and left < MIN_SIGNED_URL_LIFETIME:
        raise _fail(
            f"{which} has a download link that expires in {max(left, 0) // 60} minutes, before {platform} can "
            "fetch it reliably. Raise the signed-link lifetime (AWS_QUERYSTRING_EXPIRE).",
            platform,
        )

    try:
        resp = httpx.get(url, headers={"Range": "bytes=0-0"}, timeout=PROBE_TIMEOUT, follow_redirects=False)
    except httpx.HTTPError as exc:
        raise _fail(
            f"{which} could not be reached to check it ({type(exc).__name__}); {platform} would not be able to "
            "download it either.",
            platform,
            retryable=True,
        ) from exc

    if resp.status_code >= 500:
        raise _fail(f"{which} is served with a server error ({resp.status_code}).", platform, retryable=True)
    if 300 <= resp.status_code < 400:
        # Redirected storage or CDN: the platform follows it, and we won't chase it from inside the worker.
        return
    if resp.status_code >= 400:
        raise _fail(
            f"{which} answers {resp.status_code} at its public address, so {platform} cannot download it. "
            "Check that the file still exists and that its link is public or still valid.",
            platform,
        )

    content_type = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
    wanted = _TYPE_PREFIXES.get(kind)
    if wanted and content_type and content_type not in _GENERIC_TYPES and not content_type.startswith(wanted):
        raise _fail(
            f"{which} is served as {content_type}, not as a {kind}, so {platform} will refuse it. "
            "Check how media is served from this site's storage.",
            platform,
        )


def _signed_url_seconds_left(url: str) -> int | None:
    """Seconds until a signed (S3/R2 SigV4 or legacy ``Expires``) link stops working, or None for an unsigned one."""
    query = parse_qs(urlparse(url).query)
    try:
        if "X-Amz-Expires" in query and "X-Amz-Date" in query:
            signed_at = datetime.strptime(query["X-Amz-Date"][0], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
            return int(signed_at.timestamp() + int(query["X-Amz-Expires"][0]) - time.time())
        if "Expires" in query:
            return int(query["Expires"][0]) - int(time.time())
    except (ValueError, IndexError):
        return None
    return None
