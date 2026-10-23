"""The public side: ``/l/<slug>/``, its accent stylesheet and the click beacon.

No login, no cookies, no session. The rendered page is cached for a minute per
slug (counting still happens on every request), the accent colour comes from a
same-origin stylesheet so the page needs no inline style or script, and every
outbound link carries ``rel="noopener nofollow ugc"``.
"""

from __future__ import annotations

import uuid
from urllib.parse import urljoin

from django.conf import settings
from django.core.cache import cache
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.text import Truncator
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST

from . import services
from .models import BioPage

# Text on the accent colour: black or white, whichever reads better.
_LIGHT_ACCENT_LUMINANCE = 0.55


def _published(slug: str) -> BioPage:
    return get_object_or_404(
        BioPage.objects.select_related("workspace__organization", "avatar"), slug=slug, is_published=True
    )


def _accent_ink(hex_color: str) -> str:
    r, g, b = (int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5))
    luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return "#111111" if luminance > _LIGHT_ACCENT_LUMINANCE else "#ffffff"


# Browser chrome colour and colour scheme per preset, so the phone's address bar matches the page.
_THEME_CHROME = {
    BioPage.Theme.CLEAN: ("#f7f7f5", "light"),
    BioPage.Theme.MIDNIGHT: ("#101218", "dark"),
    BioPage.Theme.SUNRISE: ("#fff4e8", "light"),
    BioPage.Theme.FOREST: ("#eef4ee", "light"),
}


def _absolute(url: str) -> str:
    """``url`` made absolute against APP_URL, never the request's Host, so the cached page is the same everywhere."""
    return urljoin(f"{settings.APP_URL.rstrip('/')}/", url) if url else ""


def canonical_url(page: BioPage) -> str:
    if page.custom_domain and getattr(settings, "LINKINBIO_CUSTOM_DOMAINS_ENABLED", False):
        return f"https://{page.custom_domain}/"
    return _absolute(reverse("linkinbio_public:page", kwargs={"slug": page.slug}))


def _render(page: BioPage) -> str:
    avatar = page.avatar.preview_url if page.avatar_id else ""
    theme_color, color_scheme = _THEME_CHROME.get(page.theme, _THEME_CHROME[BioPage.Theme.CLEAN])
    return render_to_string(
        "linkinbio/public.html",
        {
            "page": page,
            "avatar_url": avatar,
            "og_image": _absolute(avatar),
            "canonical_url": canonical_url(page),
            "description": Truncator(page.bio or f"{page.title}: links").chars(160),
            "theme_color": theme_color,
            "color_scheme": color_scheme,
            "app_url": _absolute("/"),
            "links": services.visible_links(page),
            "socials": services.social_links(page),
            "tiles": services.latest_posts(page) if page.show_latest_posts else [],
            "show_badge": services.shows_badge(page),
        },
    )


@require_http_methods(["GET", "HEAD"])
def public_page(request, slug):
    page = _published(slug)
    key = services.page_cache_key(page.slug)
    html = cache.get(key)
    if html is None:
        html = _render(page)
        cache.set(key, html, services.PAGE_CACHE_SECONDS)
    services.record_view(page, request)
    response = HttpResponse(html)
    response["Cache-Control"] = "no-cache"
    response["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


@require_http_methods(["GET", "HEAD"])
def theme_css(request, slug):
    page = _published(slug)
    accent = page.accent_color
    body = ""
    if accent:
        body = f"body.bio{{--bio-accent:{accent};--bio-accent-ink:{_accent_ink(accent)};}}\n"
    response = HttpResponse(body, content_type="text/css; charset=utf-8")
    response["Cache-Control"] = "public, max-age=60"
    return response


@csrf_exempt
@require_POST
def click(request, slug):
    page = _published(slug)
    try:
        target = uuid.UUID(request.POST.get("l", ""))
    except ValueError as exc:
        raise Http404 from exc
    outcome = services.record_click(page, target, request)
    return HttpResponse(status=429 if outcome == "limited" else 204)
