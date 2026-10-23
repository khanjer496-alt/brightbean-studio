"""Turning a source into the plain text a run works from.

Every reader returns a :class:`Source`: the text (capped at ``MAX_SOURCE_CHARS``),
a label and where it came from. The text is *data* from then on. The prompts
wrap it in a ``<source>`` tag and tell the model never to follow instructions
inside it; nothing here interprets it.

* ``from_url``: the composer's SSRF-safe fetch (``ai_writer.services.fetch_link``),
  then a small readability pass: prefer ``<article>`` / ``<main>``, drop
  navigation, forms and scripts. No headless browser.
* ``from_pdf``: ``pypdf``, after a size check, a ``%PDF-`` check, refusing
  encrypted files and capping pages and characters. Nothing in the file is run.
* ``from_post``: a published post of the workspace.
* ``from_video``: a video link on a platform whose API returns captions for the
  connected account's own videos, otherwise the caller pastes a transcript.
"""

from __future__ import annotations

import html
import io
import logging
import re
from dataclasses import dataclass
from datetime import timedelta
from html.parser import HTMLParser

from django.conf import settings

from apps.ai_writer import services as ai_services

logger = logging.getLogger(__name__)

MAX_SOURCE_CHARS = 24_000
MIN_SOURCE_CHARS = 40
_REFRESH_WINDOW = timedelta(minutes=5)
MAX_LABEL_CHARS = 255


class TranscriptNeededError(ValueError):
    """No transcript could be read through the platform's API; the person pastes one instead."""

    code = "transcript_needed"


@dataclass(frozen=True)
class Source:
    type: str
    text: str
    label: str = ""
    url: str = ""
    post: object | None = None
    account: object | None = None


def _finish(source_type: str, text: str, **extra) -> Source:
    text = re.sub(r"[ \t\r\f\v]+", " ", text or "")
    text = re.sub(r"\s*\n\s*\n\s*", "\n\n", text).strip()[:MAX_SOURCE_CHARS]
    if len(text) < MIN_SOURCE_CHARS:
        raise ValueError("There is too little text in that source to work from.")
    extra["label"] = (extra.get("label") or "")[:MAX_LABEL_CHARS]
    return Source(type=source_type, text=text, **extra)


# ---------------------------------------------------------------------------
# Pasted text
# ---------------------------------------------------------------------------


def from_text(text: str, label: str = "") -> Source:
    return _finish("text", text, label=label or "Pasted text")


# ---------------------------------------------------------------------------
# Web pages
# ---------------------------------------------------------------------------

_SKIP_TAGS = frozenset(
    {"script", "style", "noscript", "svg", "template", "nav", "header", "footer", "aside", "form", "iframe", "button"}
)
_BLOCK_TAGS = frozenset(
    {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "tr", "section"}
)
_MIN_CONTAINER_CHARS = 200


class _ReadableParser(HTMLParser):
    """Collects text three ways (article, main, whole body) so the best container can win."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.og_title = ""
        self._in_title = False
        self._skip: list[str] = []
        self._open: dict[str, int] = {"article": 0, "main": 0}
        self.buffers: dict[str, list[str]] = {"article": [], "main": [], "body": []}

    def handle_starttag(self, tag, attrs):
        if tag == "meta":
            values = dict(attrs)
            if values.get("property") == "og:title":
                self.og_title = values.get("content") or ""
            return
        if tag == "title":
            self._in_title = True
        if tag in _SKIP_TAGS:
            self._skip.append(tag)
            return
        if tag in self._open:
            self._open[tag] += 1
        if tag in _BLOCK_TAGS:
            self._write("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in _SKIP_TAGS:
            if tag in self._skip:
                # Close back to the matching tag, tolerating sloppy markup.
                while self._skip and self._skip.pop() != tag:
                    pass
            return
        if tag in self._open and self._open[tag]:
            self._open[tag] -= 1
        if tag in _BLOCK_TAGS:
            self._write("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
            return
        if not self._skip:
            self._write(data)

    def _write(self, text):
        self.buffers["body"].append(text)
        for name, depth in self._open.items():
            if depth:
                self.buffers[name].append(text)


def readable_text(markup: str) -> tuple[str, str]:
    """``(title, text)`` of an HTML page's main content, scripts, menus and forms removed."""
    parser = _ReadableParser()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:  # noqa: BLE001 - the stdlib parser can choke on hostile markup; fall back to tag stripping
        logger.info("Readable-text pass failed; stripping tags instead", exc_info=True)
        return ai_services._page_text(markup)
    title = html.unescape(parser.og_title or parser.title).strip()
    chosen = ""
    for name in ("article", "main", "body"):
        candidate = "".join(parser.buffers[name])
        if name == "body" or len(candidate.strip()) >= _MIN_CONTAINER_CHARS:
            chosen = candidate
            break
    return title[:300], chosen


def from_url(url: str) -> Source:
    title, text = ai_services.fetch_link(url, extract=readable_text)
    return _finish("url", text, label=title or url, url=url.strip())


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------


def from_pdf(file_obj, name: str = "") -> Source:
    """Text of an uploaded PDF. Refuses oversized, encrypted, non-PDF and image-only files."""
    from pypdf import PdfReader

    max_bytes = settings.REPURPOSE_MAX_PDF_BYTES
    data = file_obj.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(f"That PDF is larger than {max_bytes // (1024 * 1024)} MB.")
    if not data.startswith(b"%PDF-"):
        raise ValueError("That file isn't a PDF.")
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            raise ValueError("That PDF is password protected.")
        if len(reader.pages) > settings.REPURPOSE_MAX_PDF_PAGES:
            raise ValueError(f"That PDF has more than {settings.REPURPOSE_MAX_PDF_PAGES} pages.")
        chunks, size = [], 0
        for page in reader.pages:
            chunks.append(page.extract_text() or "")
            size += len(chunks[-1])
            if size >= MAX_SOURCE_CHARS:
                break
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001 - pypdf raises many types on malformed files
        logger.info("PDF could not be read: %s", type(exc).__name__)
        raise ValueError("That PDF couldn't be read. It may be damaged.") from exc
    try:
        return _finish("pdf", "\n\n".join(chunks), label=name or "PDF")
    except ValueError as exc:
        raise ValueError("No text could be read from that PDF (scanned pages need OCR first).") from exc


# ---------------------------------------------------------------------------
# A published post
# ---------------------------------------------------------------------------


def from_post(post) -> Source:
    """A published post's caption and thread, to be remixed for other networks."""
    from apps.composer.models import PlatformPost
    from apps.composer.services import post_thread_parts

    if not post.platform_posts.filter(status=PlatformPost.Status.PUBLISHED).exists():
        raise ValueError("Only a published post can be remixed.")
    pieces = [post.caption or ""]
    pieces += [part.caption for part in post_thread_parts(post) if part.caption]
    return _finish(
        "post", "\n\n".join(pieces), label=(post.title or post.caption_snippet or "Published post"), post=post
    )


# ---------------------------------------------------------------------------
# Video transcripts
# ---------------------------------------------------------------------------

_SRT_INDEX = re.compile(r"^\d+$")
_SRT_TIME = re.compile(r"^\d{1,2}:\d{2}:\d{2}[,.]\d{3}\s*-->")


def srt_to_text(srt: str) -> str:
    """Caption lines without cue numbers, timings or markup, as running text."""
    lines = []
    for raw in (srt or "").splitlines():
        line = raw.strip()
        if not line or _SRT_INDEX.match(line) or _SRT_TIME.match(line):
            continue
        lines.append(html.unescape(re.sub(r"<[^>]+>", "", line)))
    return " ".join(lines)


def _video_provider_for(url: str):
    """``(platform, video id)`` for the platform whose link this is, or ``("", "")``."""
    from providers import PROVIDER_REGISTRY

    for platform, cls in PROVIDER_REGISTRY.items():
        if cls.owned_video_transcripts:
            video_id = cls.video_id_from_url(url)
            if video_id:
                return platform, video_id
    return "", ""


def from_video(url: str, *, transcript: str = "", accounts=()) -> Source:
    """A video's transcript: pasted by the person, or read from the platform for a video on their own channel.

    Only the platform's official captions API is used, and only for a video
    owned by one of the workspace's connected ``accounts``. Anything else
    raises :class:`TranscriptNeededError` so the person pastes the transcript.
    """
    url = (url or "").strip()
    platform, video_id = _video_provider_for(url)
    if not platform:
        raise ValueError("That isn't a link to a supported video. Paste the transcript as text instead.")
    label = f"{platform.title()} video {video_id}"
    if (transcript or "").strip():
        return _finish("video", transcript, label=label, url=url)

    text = None
    for account in accounts:
        if account.platform != platform:
            continue
        text = _read_owned_transcript(account, video_id)
        if text:
            return _finish("video", text, label=label, url=url, account=account)
    raise TranscriptNeededError(
        "A transcript can only be read for videos on a channel connected to this workspace, and only when the "
        "video has captions. Paste the transcript instead."
    )


def _read_owned_transcript(account, video_id: str) -> str | None:
    from apps.publisher.engine import _resolve_publish_credentials
    from providers import get_provider
    from providers.exceptions import ProviderError, QuotaExceededError

    if account.connection_status != account.ConnectionStatus.CONNECTED:
        return None
    try:
        provider = get_provider(account.platform, _resolve_publish_credentials(account))
        access_token = account.oauth_access_token
        if account.oauth_refresh_token and account.token_expires_within(_REFRESH_WINDOW):
            access_token = account.refresh_oauth_token(provider, enqueue_backfill=False)
        srt = provider.fetch_owned_video_transcript(
            access_token, video_id=video_id, channel_id=account.account_platform_id
        )
    except QuotaExceededError as exc:
        raise TranscriptNeededError(
            f"{account.get_platform_display()} has used up its API quota for today. Paste the transcript instead."
        ) from exc
    except ProviderError:
        logger.info("Transcript fetch failed for account %s", account.pk, exc_info=True)
        return None
    return srt_to_text(srt) if srt else None
