"""Shared behaviour of the blog and newsletter providers (Medium, WordPress, Hashnode, Listmonk).

All four publish a titled Markdown article from a pasted credential, so the title rule, the tag and canonical
resolution and the cover image are written once here. DEV.to predates this and keeps its own copies.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from .base import SocialProvider
from .exceptions import PublishError
from .types import PublishContent

IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".gif", ".webp")


class BlogProvider(SocialProvider):
    """A provider whose posts are articles: a required title, a Markdown body, tags and a canonical link."""

    # Tags a post may carry, and the longest one, as the platform takes them (set per provider).
    max_tags: int = 5
    max_tag_length: int = 50
    max_title_length: int = 250

    # Publishes from hosted image URLs unless a provider uploads the file itself.
    needs_local_media = False

    def _require_title(self, content: PublishContent) -> str:
        title = (content.title or "").strip()
        if not title:
            raise PublishError(
                f"{self.platform_name} requires a title. Set the post title before publishing.",
                platform=self.platform_name,
                retryable=False,
            )
        if len(title) > self.max_title_length:
            raise PublishError(
                f"{self.platform_name} titles are limited to {self.max_title_length} characters.",
                platform=self.platform_name,
                retryable=False,
            )
        return title

    @staticmethod
    def _body(content: PublishContent) -> str:
        return content.description or content.text or ""

    def _tags(self, content: PublishContent) -> list[str]:
        """The post's tags (an explicit list, else its #hashtags), trimmed, deduplicated and capped."""
        raw = content.extra.get("tags") if isinstance(content.extra, dict) else None
        if not raw:
            raw = re.findall(r"(?<!\w)#(\w+)", content.text or "")
        tags: list[str] = []
        for tag in raw:
            clean = str(tag).strip().lstrip("#")
            if clean and len(clean) <= self.max_tag_length and clean.lower() not in {t.lower() for t in tags}:
                tags.append(clean)
            if len(tags) >= self.max_tags:
                break
        return tags

    @staticmethod
    def _canonical_url(content: PublishContent) -> str:
        """The address the article was first published at: the post's setting, else its link."""
        extra = content.extra if isinstance(content.extra, dict) else {}
        return str(extra.get("canonical_url") or content.link_url or "").strip()

    @staticmethod
    def _image_urls(content: PublishContent) -> list[str]:
        """The hosted images attached to the post, in order."""
        return [url for url in content.media_urls or [] if url.split("?", 1)[0].lower().endswith(IMAGE_SUFFIXES)]

    @staticmethod
    def validate_canonical_url(settings: dict) -> str:
        url = str((settings or {}).get("canonical_url") or "").strip()
        parts = urlsplit(url)
        if url and (parts.scheme not in ("http", "https") or not parts.netloc):
            return "Canonical URL must start with http:// or https://."
        return ""
