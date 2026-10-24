"""``/s/<code>``: the built-in short-link redirect. No login, no cookies, nothing stored about the visitor."""

import logging

from django.core.exceptions import ValidationError
from django.http import Http404, HttpResponse, HttpResponsePermanentRedirect
from django.views.decorators.http import require_http_methods

from apps.linkinbio.validators import validate_http_url

from . import services
from .models import ShortLink

logger = logging.getLogger(__name__)


@require_http_methods(["GET", "HEAD"])
def follow(request, code):
    # Only links we serve ourselves, and only while they are on. An unknown, external-service or disabled link
    # is a plain 404, so this view can never be pointed at an address a workspace member didn't create here.
    links = ShortLink.objects.filter(code=code, provider="builtin", is_active=True)
    domain_workspace = getattr(request, "short_domain_workspace_id", "")
    if domain_workspace:
        links = links.filter(workspace_id=domain_workspace)
    link = links.first()
    if link is None:
        raise Http404
    # Re-checked on the way out: whatever the row holds, only a plain web address is ever sent to.
    try:
        validate_http_url(link.destination_url)
    except ValidationError as exc:
        raise Http404 from exc
    try:
        outcome = services.record_hit(link, request)
    except Exception:  # counting must never stop the redirect
        logger.exception("Counting a visit to short link %s failed", link.pk)
        outcome = services.IGNORED
    if outcome == "limited":
        return HttpResponse("Too many requests.", status=429, headers={"Retry-After": "60"})
    response = HttpResponsePermanentRedirect(link.destination_url)
    # A 301 is cacheable by default, which would hide later clicks and keep a disabled link working.
    response["Cache-Control"] = "no-store"
    response["Referrer-Policy"] = "no-referrer"
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response
