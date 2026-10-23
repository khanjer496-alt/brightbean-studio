"""Serve a page on its own domain when ``LINKINBIO_CUSTOM_DOMAINS_ENABLED`` is on.

A request whose Host names a page's ``custom_domain`` is routed to that page by
rewriting ``/`` (and the beacon's ``/click/``) under ``/l/<slug>/``. On such a
host only the page itself, its own ``/l/<slug>/…`` addresses and static/media
files are served; every other path is a 404, so the app (sign-in, workspaces,
the API) is never reachable on a customer's domain.

The Host is the one Django validated against ``ALLOWED_HOSTS`` (the operator
adds customer domains there); an unknown or forged Host passes through untouched
for the usual ``DisallowedHost`` handling, without a database query. While the
setting is off, the default, nothing here runs.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import DisallowedHost
from django.http import HttpResponseNotFound

from .models import BioPage

DOMAIN_CACHE_SECONDS = 60


def domain_cache_key(host: str) -> str:
    return f"linkinbio:domain:{host}"


def _own_hosts() -> set[str]:
    hosts = {h.lower() for h in settings.ALLOWED_HOSTS if not h.startswith(".") and h != "*"}
    app_host = urlsplit(settings.APP_URL).hostname
    if app_host:
        hosts.add(app_host.lower())
    return hosts


def slug_for_domain(host: str) -> str:
    """The published page's slug served on ``host``, or "" (looked up once a minute per host)."""
    key = domain_cache_key(host)
    slug = cache.get(key)
    if slug is None:
        slug = (
            BioPage.objects.filter(custom_domain=host, is_published=True).values_list("slug", flat=True).first() or ""
        )
        cache.set(key, slug, DOMAIN_CACHE_SECONDS)
    return slug


class CustomDomainMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if getattr(settings, "LINKINBIO_CUSTOM_DOMAINS_ENABLED", False):
            try:
                host = request.get_host().split(":")[0].lower().rstrip(".")
            except DisallowedHost:
                host = ""
            if host and host not in _own_hosts():
                slug = slug_for_domain(host)
                if slug:
                    path = request.path_info
                    if path in ("/", "/click/"):
                        request.path_info = request.path = f"/l/{slug}{path}"
                    elif not self._is_page_asset(path, slug):
                        return HttpResponseNotFound()
        return self.get_response(request)

    @staticmethod
    def _is_page_asset(path: str, slug: str) -> bool:
        prefixes = [f"/l/{slug}/", settings.STATIC_URL or "/static/"]
        if settings.MEDIA_URL and settings.MEDIA_URL.startswith("/"):
            prefixes.append(settings.MEDIA_URL)
        return any(path.startswith(prefix) for prefix in prefixes)
