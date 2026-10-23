"""Serve a page on its own domain when ``LINKINBIO_CUSTOM_DOMAINS_ENABLED`` is on.

A request whose Host names a page's ``custom_domain`` is routed to that page by
rewriting its path under ``/l/<slug>/``; everything else (and every request
while the setting is off, the default) passes through untouched without a
query. Only the public page paths are rewritten, so the app itself is never
reachable on a customer's domain.
"""

from __future__ import annotations

from django.conf import settings

from .models import BioPage


class CustomDomainMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if getattr(settings, "LINKINBIO_CUSTOM_DOMAINS_ENABLED", False):
            host = request.META.get("HTTP_HOST", "").split(":")[0].lower().rstrip(".")
            own = {h.lower() for h in settings.ALLOWED_HOSTS if not h.startswith(".") and h != "*"}
            if host and host not in own:
                slug = (
                    BioPage.objects.filter(custom_domain=host, is_published=True).values_list("slug", flat=True).first()
                )
                if slug and self._is_public_path(request.path_info):
                    request.path_info = f"/l/{slug}{request.path_info}"
                    request.path = request.path_info
        return self.get_response(request)

    @staticmethod
    def _is_public_path(path: str) -> bool:
        return path == "/" or path in ("/click/", "/theme.css")
