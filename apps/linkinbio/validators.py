"""What may be a page address, a link target or a custom domain."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError

SLUG_MAX = 40
URL_MAX = 2000

_SLUG_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{1,38}[a-z0-9])")
_DOMAIN_RE = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
_CONTROL_OR_SPACE = re.compile(r"[\x00-\x20\x7f]")

# Words that would shadow a route of the app, read as an official page of this service or of a
# platform, or that people mistake for support. Brand names also match as part of a slug
# (``RESERVED_FRAGMENTS``), so "instagram-support" is refused as well as "instagram".
RESERVED_SLUGS = frozenset(
    [
        "abuse",
        "about",
        "account",
        "accounts",
        "admin",
        "administrator",
        "api",
        "app",
        "apps",
        "assets",
        "auth",
        "billing",
        "blog",
        "cdn",
        "client",
        "clients",
        "contact",
        "dashboard",
        "dev",
        "docs",
        "download",
        "faq",
        "help",
        "home",
        "inbox",
        "info",
        "l",
        "legal",
        "login",
        "logout",
        "mail",
        "mcp",
        "media",
        "me",
        "meta",
        "moderator",
        "news",
        "null",
        "oauth",
        "official",
        "password",
        "pricing",
        "privacy",
        "register",
        "reports",
        "root",
        "security",
        "settings",
        "signin",
        "signup",
        "staff",
        "static",
        "status",
        "support",
        "system",
        "team",
        "terms",
        "threads",
        "undefined",
        "verified",
        "verify",
        "webhooks",
        "workspace",
        "www",
    ]
)
RESERVED_FRAGMENTS = (
    "postdelegate",
    "brightbean",
    "instagram",
    "facebook",
    "tiktok",
    "twitter",
    "youtube",
    "linkedin",
    "bluesky",
    "pinterest",
    "mastodon",
    "reddit",
    "google",
    "whatsapp",
    "telegram",
    "discord",
    "snapchat",
    "x-support",
    "x-official",
)


def validate_slug(value: str) -> None:
    if not _SLUG_RE.fullmatch(value or ""):
        raise ValidationError(
            "Use 3 to 40 lowercase letters, numbers or hyphens, starting and ending with a letter or number."
        )
    if "--" in value:
        raise ValidationError("Hyphens can't come twice in a row.")
    if value in RESERVED_SLUGS or any(fragment in value for fragment in RESERVED_FRAGMENTS):
        raise ValidationError("That address is reserved. Pick another.")


def validate_http_url(value: str) -> None:
    """Only absolute http(s) addresses: nothing that runs script or opens another app."""
    value = value or ""
    if len(value) > URL_MAX:
        raise ValidationError(f"Use an address of at most {URL_MAX} characters.")
    if _CONTROL_OR_SPACE.search(value):
        raise ValidationError("The address can't contain spaces or control characters.")
    try:
        parts = urlsplit(value)
        host = parts.hostname
        parts.port  # noqa: B018 - raises ValueError on a bad port
    except ValueError as exc:
        raise ValidationError("That isn't a valid web address.") from exc
    if parts.scheme.lower() not in ("http", "https") or not host:
        raise ValidationError("Use a full web address starting with http:// or https://.")
    if parts.username is not None or parts.password is not None:
        raise ValidationError("Addresses with a username or password aren't allowed.")


def validate_domain(value: str) -> None:
    if not _DOMAIN_RE.fullmatch(value or "") or len(value) > 253:
        raise ValidationError("Enter a domain name such as links.example.com.")
