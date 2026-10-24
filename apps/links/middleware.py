"""Serve short links on the workspace's own short domain when ``LINKS_CUSTOM_DOMAINS_ENABLED`` is on.

``https://go.example.com/<code>`` is rewritten to ``/s/<code>``. On such a host nothing else of the app is
reachable: every other path is a 404. A host that is not a workspace's short domain passes through untouched,
and while the setting is off, the default, nothing here runs.
"""

from __future__ import annotations

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import DisallowedHost
from django.http import HttpResponseNotFound

from apps.linkinbio.middleware import _own_hosts

from .models import LinkSettings

DOMAIN_CACHE_SECONDS = 60


def is_short_domain(host: str) -> bool:
    key = f"links:domain:{host}"
    known = cache.get(key)
    if known is None:
        known = LinkSettings.objects.filter(custom_short_domain=host).exists()
        cache.set(key, known, DOMAIN_CACHE_SECONDS)
    return known


class ShortDomainMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if getattr(settings, "LINKS_CUSTOM_DOMAINS_ENABLED", False):
            try:
                host = request.get_host().split(":")[0].lower().rstrip(".")
            except DisallowedHost:
                host = ""
            if host and host not in _own_hosts() and is_short_domain(host):
                path = request.path_info.strip("/")
                if not path or "/" in path:
                    return HttpResponseNotFound()
                request.path_info = request.path = f"/s/{path}"
        return self.get_response(request)
