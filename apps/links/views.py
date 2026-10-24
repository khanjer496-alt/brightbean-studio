"""Short links and UTM settings for a workspace.

Settings need a manage permission (owners and managers by default); the link list needs analytics access;
making a link needs the right to write posts. Links are always looked up through ``request.workspace``, so an
id from another workspace is a 404.
"""

import functools

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from . import services
from .models import LinkSettings, ShortLink
from .shorteners import SHORTENERS, ShortenerError

LIST_LIMIT = 100


def _needs(*keys):
    def decorator(view_func):
        @functools.wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            membership = request.workspace_membership
            if not membership:
                raise PermissionDenied("You are not a member of this workspace.")
            perms = membership.effective_permissions
            if not any(perms.get(key, False) for key in keys):
                raise PermissionDenied(f"Permission denied: {keys[0]}")
            return view_func(request, *args, **kwargs)

        return login_required(_wrapped)

    return decorator


manage_links = _needs("manage_workspace_settings", "manage_social_accounts")


def _custom_text(link_settings) -> str:
    return "\n".join(f"{name}={value}" for name, value in (link_settings.utm_custom or {}).items())


def _parse_custom(raw: str) -> dict:
    pairs = {}
    for line in (raw or "").splitlines():
        if not line.strip():
            continue
        name, _, value = line.partition("=")
        pairs[name.strip()] = value.strip()
    return pairs


def _settings_context(request, link_settings, **extra):
    return {
        "workspace": request.workspace,
        "settings_active": "links",
        "link_settings": link_settings,
        "providers": [(key, cls.label) for key, cls in SHORTENERS.items()],
        "modes": LinkSettings.ShortenMode.choices,
        "campaign_sources": LinkSettings.CampaignFrom.choices,
        "custom_text": _custom_text(link_settings),
        "has_key": bool(link_settings.api_key),
        "preview": services.utm_preview(
            request.workspace, url="https://example.com/offer?ref=1", platform="linkedin", title="Spring sale"
        )
        if link_settings.utm_enabled
        else "",
        "custom_domains_enabled": _custom_domains_enabled(),
        **extra,
    }


@manage_links
@require_http_methods(["GET", "POST"])
def settings_page(request, workspace_id):
    link_settings = services.get_settings(request.workspace)
    if request.method == "POST":
        post = request.POST
        try:
            services.update_settings(
                request.workspace,
                api_key=post.get("api_key", ""),
                clear_api_key=post.get("clear_api_key") == "on",
                shorten_mode=post.get("shorten_mode", link_settings.shorten_mode),
                provider=post.get("provider", link_settings.provider),
                provider_domain=post.get("provider_domain", ""),
                provider_base_url=post.get("provider_base_url", ""),
                custom_short_domain=post.get("custom_short_domain", link_settings.custom_short_domain or "")
                if _custom_domains_enabled()
                else link_settings.custom_short_domain,
                utm_enabled=post.get("utm_enabled") == "on",
                utm_medium=post.get("utm_medium", ""),
                utm_campaign_from=post.get("utm_campaign_from", link_settings.utm_campaign_from),
                utm_campaign=post.get("utm_campaign", ""),
                utm_custom=_parse_custom(post.get("utm_custom", "")),
            )
        except services.LinkError as exc:
            messages.error(request, exc.message)
            link_settings = services.get_settings(request.workspace)
        else:
            messages.success(request, "Link settings saved.")
            return redirect("links:settings", workspace_id=request.workspace.id)
    return render(request, "links/settings.html", _settings_context(request, link_settings))


def _custom_domains_enabled() -> bool:
    from django.conf import settings

    return settings.LINKS_CUSTOM_DOMAINS_ENABLED


@manage_links
@require_GET
def utm_preview(request, workspace_id):
    """The address as each network would get it, for the settings page's live preview."""
    url = request.GET.get("url", "").strip() or "https://example.com/offer"
    try:
        url = services.validate_destination(url)
    except services.LinkError as exc:
        return render(request, "links/_utm_preview.html", {"error": exc.message})
    rows = []
    for platform in ("linkedin", "x", "instagram"):
        rows.append(
            {
                "platform": platform,
                "url": services.utm_preview(
                    request.workspace, url=url, platform=platform, title=request.GET.get("title", "Spring sale")
                ),
            }
        )
    return render(request, "links/_utm_preview.html", {"rows": rows})


@_needs("view_analytics", "create_posts")
@require_GET
def link_list(request, workspace_id):
    summary = services.stats_summary(request.workspace, 30)
    clicks = {item["link_id"]: item for item in summary["links"]}
    links = list(services.link_listing(request.workspace)[:LIST_LIMIT])
    for link in links:
        link.stat = clicks.get(link.pk, {"clicks": 0, "unique_clicks": 0, "previews": 0})
    perms = request.workspace_membership.effective_permissions
    return render(
        request,
        "links/list.html",
        {
            "workspace": request.workspace,
            "settings_active": "links",
            "links": links,
            "summary": summary,
            "problems": services.link_problems(request.workspace),
            "can_create": perms.get("create_posts", False),
            "can_manage": any(perms.get(k, False) for k in ("manage_workspace_settings", "manage_social_accounts")),
        },
    )


@_needs("create_posts")
@require_POST
def link_create(request, workspace_id):
    try:
        link = services.create_short_link(request.workspace, request.POST.get("url", ""), user=request.user)
    except (services.LinkError, ShortenerError) as exc:
        messages.error(request, getattr(exc, "message", str(exc)))
    else:
        messages.success(request, f"Short link ready: {link.short_url}")
    return redirect("links:list", workspace_id=request.workspace.id)


@_needs("manage_workspace_settings", "manage_social_accounts")
@require_POST
def link_toggle(request, workspace_id, link_id):
    link = get_object_or_404(ShortLink, pk=link_id, workspace=request.workspace)
    services.set_active(link, not link.is_active)
    messages.success(
        request, "Link turned on." if link.is_active else "Link turned off. It now shows a not-found page."
    )
    return redirect("links:list", workspace_id=request.workspace.id)


@_needs("create_posts")
@require_POST
def should_shorten(request, workspace_id):
    """The composer's prompt check (Postiz's ``/should-shortlink``): does the text hold a link worth asking about?

    ``ask`` is true only in "ask" mode, when the post has a web address that isn't already short.
    """
    link_settings = services.find_settings(request.workspace.id)
    texts = request.POST.getlist("messages") or [request.POST.get("caption", "")]
    urls = [url for text in texts for url in services.extract_urls(text)]
    pending = [url for url in urls if not services.is_short_url(request.workspace.id, url, link_settings)]
    mode = link_settings.shorten_mode if link_settings else LinkSettings.ShortenMode.NEVER
    return JsonResponse(
        {
            "mode": mode,
            "ask": mode == LinkSettings.ShortenMode.ASK and bool(pending),
            "has_links": bool(pending),
            "short_length": services.expected_short_length(link_settings),
        }
    )
