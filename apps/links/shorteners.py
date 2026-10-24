"""Short-link services: our own redirect and the customer's Dub, Short.io or Kutt account.

This is the only module that knows a provider by name. Everything else asks
:func:`shortener_for` for an object with ``create`` and ``clicks`` and treats the
services alike. An external service is called with the *customer's* API key (kept
encrypted in :class:`~apps.links.models.LinkSettings`); the key is never logged or
returned in an error message.

API references checked while writing this module:

* Dub: ``POST https://api.dub.co/links`` with ``Authorization: Bearer <key>`` and a body of ``url`` (required),
  ``domain`` and ``key`` (optional); the response carries ``id``, ``key``, ``shortLink`` and ``clicks``.
  ``GET /links/info?linkId=<id>`` returns the link with its ``clicks``.
  https://dub.co/docs/api-reference/endpoint/create-a-link,
  https://dub.co/docs/api-reference/endpoint/retrieve-a-link
* Short.io: ``POST https://api.short.io/links`` with the secret key in the ``Authorization`` header (no ``Bearer``
  prefix) and a body of ``originalURL`` and ``domain`` (both required); the response carries ``idString`` and
  ``shortURL``. Totals come from ``GET https://statistics.short.io/statistics/link/<id>?period=last30&tz=UTC``
  (``totalClicks``). https://developers.short.io/reference/post_links
* Kutt: ``POST <base>/api/v2/links`` with ``X-API-KEY`` and a body of ``target`` (required), ``domain`` and
  ``reuse``; the response carries ``id``, ``link`` and ``visit_count``; ``GET <base>/api/v2/links/<id>/stats``
  returns the link's visit totals. (The Kutt header and field names follow the Kutt server routes and the
  reference implementation in this repository's TypeScript app; Kutt's hosted docs were unreachable.)
"""

from __future__ import annotations

import logging
import secrets
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from django.conf import settings as django_settings

from apps.common.validators import is_safe_url

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 8.0
CODE_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
CODE_LENGTH = 7
# Tests swap this for ``httpx.MockTransport``; production leaves it unset.
_TRANSPORT: httpx.BaseTransport | None = None


class ShortenerError(Exception):
    """The service refused or could not be reached. The message is safe to show the customer."""


@dataclass(frozen=True)
class ShortResult:
    short_url: str
    code: str
    external_id: str = ""


def new_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def app_base_url(custom_short_domain: str | None = None) -> str:
    """Where built-in links are served: the custom short domain when one is set, else the app's own address."""
    if custom_short_domain:
        return f"https://{custom_short_domain}"
    return django_settings.APP_URL.rstrip("/")


def _call(method: str, url: str, *, headers: dict, json: dict | None = None) -> Any:
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS, transport=_TRANSPORT, follow_redirects=False) as client:
            response = client.request(method, url, headers=headers, json=json)
    except httpx.HTTPError as exc:
        raise ShortenerError("The link service could not be reached. Try again in a moment.") from exc
    if response.status_code in (401, 403):
        raise ShortenerError("The link service refused the API key. Check it in Links settings.")
    if response.status_code == 429:
        raise ShortenerError("The link service is rate limiting this workspace. Try again in a moment.")
    if response.status_code >= 400:
        raise ShortenerError(f"The link service answered with an error ({response.status_code}).")
    try:
        return response.json()
    except ValueError as exc:
        raise ShortenerError("The link service sent a reply we couldn't read.") from exc


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class Shortener(ABC):
    key: str = ""
    label: str = ""
    #: Whether the customer must name a domain for new links.
    needs_domain: bool = False
    #: Whether a customer-supplied API key is required.
    needs_api_key: bool = True

    def __init__(self, link_settings):
        self.settings = link_settings

    @abstractmethod
    def create(self, url: str) -> ShortResult: ...

    def settings_problem(self) -> tuple[str, str] | None:
        """``(field, message)`` when the saved settings can't work with this service, else None."""
        return None

    def clicks(self, link) -> int | None:
        """The service's own click total for ``link``, or None when it can't say."""
        return None

    def expected_length(self) -> int:
        """Typical length of a short URL from this service, used for character counts before one exists."""
        domain = self.settings.provider_domain or self.default_domain
        return len("https://") + len(domain) + 1 + CODE_LENGTH

    default_domain: str = ""


class BuiltinShortener(Shortener):
    key = "builtin"
    label = "Built-in (our own address)"
    needs_api_key = False

    def create(self, url: str) -> ShortResult:
        code = new_code()
        return ShortResult(short_url=f"{app_base_url(self.settings.custom_short_domain)}/s/{code}", code=code)

    def expected_length(self) -> int:
        return len(f"{app_base_url(self.settings.custom_short_domain)}/s/") + CODE_LENGTH


class DubShortener(Shortener):
    key = "dub"
    label = "Dub"
    default_domain = "dub.sh"
    API = "https://api.dub.co"

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.settings.api_key}", "Content-Type": "application/json"}

    def create(self, url: str) -> ShortResult:
        body: dict[str, Any] = {"url": url}
        if self.settings.provider_domain:
            body["domain"] = self.settings.provider_domain
        data = _call("POST", f"{self.API}/links", headers=self._headers(), json=body)
        short = data.get("shortLink") if isinstance(data, dict) else None
        if not short:
            raise ShortenerError("Dub did not return a short link.")
        return ShortResult(short_url=short, code=str(data.get("key") or ""), external_id=str(data.get("id") or ""))

    def clicks(self, link) -> int | None:
        if not link.external_id:
            return None
        data = _call("GET", f"{self.API}/links/info?linkId={link.external_id}", headers=self._headers())
        return _int(data.get("clicks")) if isinstance(data, dict) else None


class ShortioShortener(Shortener):
    key = "shortio"
    label = "Short.io"
    needs_domain = True
    default_domain = "short.io"
    API = "https://api.short.io"
    STATS = "https://statistics.short.io"

    def _headers(self) -> dict:
        return {"Authorization": self.settings.api_key, "Content-Type": "application/json"}

    def settings_problem(self):
        if self.settings.api_key and not self.settings.provider_domain:
            return ("provider_domain", "Short.io needs the domain your links use.")
        return None

    def create(self, url: str) -> ShortResult:
        if not self.settings.provider_domain:
            raise ShortenerError("Short.io needs the domain your links use. Add it in Links settings.")
        data = _call(
            "POST",
            f"{self.API}/links",
            headers=self._headers(),
            json={"originalURL": url, "domain": self.settings.provider_domain},
        )
        short = data.get("shortURL") if isinstance(data, dict) else None
        if not short:
            raise ShortenerError("Short.io did not return a short link.")
        return ShortResult(
            short_url=short, code=str(data.get("path") or ""), external_id=str(data.get("idString") or "")
        )

    def clicks(self, link) -> int | None:
        if not link.external_id:
            return None
        data = _call(
            "GET", f"{self.STATS}/statistics/link/{link.external_id}?period=last30&tz=UTC", headers=self._headers()
        )
        return _int(data.get("totalClicks")) if isinstance(data, dict) else None


class KuttShortener(Shortener):
    key = "kutt"
    label = "Kutt"
    default_domain = "kutt.it"
    DEFAULT_BASE = "https://kutt.it/api/v2"

    def _base(self) -> str:
        base = (self.settings.provider_base_url or self.DEFAULT_BASE).rstrip("/")
        if base != self.DEFAULT_BASE and (not base.startswith("https://") or not is_safe_url(base)):
            raise ShortenerError("The Kutt address must be a public https:// address.")
        return base

    def _headers(self) -> dict:
        return {"X-API-KEY": self.settings.api_key, "Content-Type": "application/json"}

    def settings_problem(self):
        base = self.settings.provider_base_url
        if base and not base.startswith("https://"):
            return ("provider_base_url", "The Kutt address must start with https://")
        return None

    def create(self, url: str) -> ShortResult:
        body: dict[str, Any] = {"target": url, "reuse": False}
        if self.settings.provider_domain:
            body["domain"] = self.settings.provider_domain
        data = _call("POST", f"{self._base()}/links", headers=self._headers(), json=body)
        short = data.get("link") if isinstance(data, dict) else None
        if not short:
            raise ShortenerError("Kutt did not return a short link.")
        return ShortResult(short_url=short, code=str(data.get("address") or ""), external_id=str(data.get("id") or ""))

    def clicks(self, link) -> int | None:
        if not link.external_id:
            return None
        data = _call("GET", f"{self._base()}/links/{link.external_id}/stats", headers=self._headers())
        if not isinstance(data, dict):
            return None
        total = _int(data.get("total"))
        if total is None:
            total = _int((data.get("allTime") or {}).get("total"))
        return total

    def expected_length(self) -> int:
        base = urlsplit(self.settings.provider_base_url or self.DEFAULT_BASE).hostname or self.default_domain
        return len("https://") + len(self.settings.provider_domain or base) + 1 + 6


SHORTENERS: dict[str, type[Shortener]] = {
    cls.key: cls for cls in (BuiltinShortener, DubShortener, ShortioShortener, KuttShortener)
}


def settings_problem(link_settings) -> tuple[str, str] | None:
    """What is wrong with the chosen service's settings, if anything (checked when settings are saved)."""
    cls = SHORTENERS.get(link_settings.provider, BuiltinShortener)
    return cls(link_settings).settings_problem()


def shortener_for(link_settings) -> Shortener:
    """The service the workspace chose. An external service with no key falls back to the built-in one."""
    cls = SHORTENERS.get(link_settings.provider, BuiltinShortener)
    if cls.needs_api_key and not link_settings.api_key:
        cls = BuiltinShortener
    return cls(link_settings)
