"""``/s/<code>``: the built-in short-link redirect. No login, no cookies, nothing stored about the visitor."""

from urllib.parse import urlsplit

from django.http import Http404, HttpResponse, HttpResponsePermanentRedirect
from django.views.decorators.http import require_http_methods

from . import services
from .models import ShortLink


@require_http_methods(["GET", "HEAD"])
def follow(request, code):
    # Only links we serve ourselves, and only while they are on. An unknown, external-service or disabled link
    # is a plain 404, so this view can never be pointed at an address a workspace member didn't create here.
    link = ShortLink.objects.filter(code=code, provider="builtin", is_active=True).first()
    if link is None:
        raise Http404
    # Re-checked on the way out: whatever the row holds, only a web address is ever sent to.
    if urlsplit(link.destination_url).scheme.lower() not in ("http", "https"):
        raise Http404
    outcome = services.record_hit(link, request)
    if outcome == "limited":
        return HttpResponse("Too many requests.", status=429, headers={"Retry-After": "60"})
    response = HttpResponsePermanentRedirect(link.destination_url)
    # A 301 is cacheable by default, which would hide later clicks and keep a disabled link working.
    response["Cache-Control"] = "no-store"
    response["Referrer-Policy"] = "no-referrer"
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response
