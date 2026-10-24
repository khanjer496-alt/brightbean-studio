"""Reading a feed and its articles for the autoposter: the composer's SSRF-safe fetch and parser, plus conditional requests.

Every request goes through ``safe_fetch`` (``apps.composer.views._safe_fetch_feed``): http(s) only, each redirect hop
re-checked against private and reserved addresses, the body streamed and capped. Nothing here talks to the network any
other way, so tests replace ``safe_fetch`` and nothing else.
"""

from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urljoin

from django.conf import settings

from apps.composer import views as feed_reader
from apps.composer.views import _first_child_text, _parse_feed_document

FEED_MAX_BYTES = 5 * 1024 * 1024
ARTICLE_MAX_BYTES = 1024 * 1024
IMAGE_MAX_BYTES = 5 * 1024 * 1024
MAX_ENTRIES = 50
IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"})
_HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
_META = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_ATTR = re.compile(r"""([a-zA-Z:_-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")


def safe_fetch(url, headers, *, max_bytes, content_types=None):
    """The SSRF-safe GET. ``(response, final_url)``, or ``(None, None)`` when refused or unreachable."""
    return feed_reader._safe_fetch_feed(url, headers, max_bytes=max_bytes, content_types=content_types)


def _agent(kind: str) -> str:
    return f"{settings.BRAND_SHORT_NAME} {kind}/1.0"


@dataclass
class Entry:
    key: str
    title: str
    link: str
    summary: str
    image_url: str
    published_at: datetime | None


@dataclass
class FetchResult:
    # "ok", "not_modified" or "error"
    outcome: str
    entries: list[Entry] = field(default_factory=list)
    etag: str = ""
    last_modified: str = ""
    error: str = ""
    title: str = ""
    website_url: str = ""


def item_key(guid: str, link: str, title: str) -> str:
    """The identity of an item: its GUID, else its link, else its title."""
    return hashlib.sha256((guid or link or title).strip().encode()).hexdigest()


def fetch(feed_url: str, *, etag: str = "", last_modified: str = "") -> FetchResult:
    """Fetch and parse a feed, conditionally when the server gave a validator last time."""
    headers = {
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.1",
        "User-Agent": _agent("RSS Autoposter"),
    }
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    response, _ = safe_fetch(feed_url, headers, max_bytes=FEED_MAX_BYTES)
    if response is None:
        return FetchResult("error", error="The feed could not be reached, is too large, or is not a public address.")
    if response.status_code == 304:
        return FetchResult("not_modified", etag=etag, last_modified=last_modified)
    if response.status_code >= 400:
        return FetchResult("error", error=f"The feed answered with an error (HTTP {response.status_code}).")
    document = _parse_feed_document(response.content)
    if not document:
        return FetchResult("error", error="The address does not return a valid RSS or Atom feed.")
    entries = []
    for raw in document["entries"][:MAX_ENTRIES]:
        event = feed_reader._build_event_from_entry(_Named(document["title"]), document, raw)
        guid = _first_child_text(raw, "guid", "id")
        link = event["link"] if _is_web(event["link"]) else ""
        entries.append(
            Entry(
                key=item_key(guid, link, event["title"]),
                title=event["title"][:300],
                link=link,
                summary=event["summary"],
                image_url=event["image_url"] if _is_web(event["image_url"]) else "",
                published_at=event["published_at"],
            )
        )
    return FetchResult(
        "ok",
        entries=entries,
        title=(document.get("title") or "").strip(),
        website_url=(document.get("website_url") or "").strip(),
        etag=response.headers.get("ETag", "")[:255],
        last_modified=response.headers.get("Last-Modified", "")[:255],
    )


class _Named:
    """The bits of a ``Feed`` the shared entry builder reads, without needing one saved."""

    id = ""
    favicon_url = ""
    website_url = ""

    def __init__(self, name):
        self.name = name


def _is_web(url: str) -> bool:
    return bool(url) and url.lower().startswith(("http://", "https://"))


def article_image(url: str) -> str:
    """The article's own image (``og:image``, else ``twitter:image``), or "" when the page has none or can't be read."""
    response, final_url = safe_fetch(
        url,
        {"Accept": "text/html,application/xhtml+xml;q=0.9", "User-Agent": _agent("Link Reader")},
        max_bytes=ARTICLE_MAX_BYTES,
        content_types=_HTML_TYPES,
    )
    if response is None or response.status_code >= 400:
        return ""
    found: dict[str, str] = {}
    for tag in _META.findall(response.text[:200_000]):
        attrs = {m[0].lower(): html.unescape(m[1] or m[2]) for m in _ATTR.findall(tag)}
        name = (attrs.get("property") or attrs.get("name") or "").lower()
        if name in ("og:image", "og:image:url", "og:image:secure_url", "twitter:image") and attrs.get("content"):
            found.setdefault(name, attrs["content"].strip())
    for name in ("og:image", "og:image:secure_url", "og:image:url", "twitter:image"):
        if found.get(name):
            absolute = urljoin(final_url or url, found[name])
            return absolute if _is_web(absolute) else ""
    return ""


def download_image(url: str) -> tuple[bytes, str] | None:
    """``(bytes, content type)`` of an image at a public address, within the size cap, else None."""
    response, _ = safe_fetch(
        url,
        {"Accept": "image/*", "User-Agent": _agent("Link Reader")},
        max_bytes=IMAGE_MAX_BYTES,
        content_types=IMAGE_TYPES,
    )
    if response is None or response.status_code >= 400 or not response.content:
        return None
    content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
    return response.content, content_type
