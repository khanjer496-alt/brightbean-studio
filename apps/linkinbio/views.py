"""Link-in-bio editor: pages, their links and counters, inside a workspace.

Seeing pages needs ``view_analytics``; creating and editing them needs the
channel-management permission as well (owners and managers), like reports.
Every change is a plain form post that redirects back to the editor, so it
works without script and reads the same to a screen reader.
"""

from __future__ import annotations

import datetime as dt
import zoneinfo

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from apps.billing.entitlements import PlanLimitError, get_entitlements
from apps.composer.models import PlatformPost
from apps.media_library.models import MediaAsset
from apps.members.models import WorkspaceMembership
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

from . import services
from .models import BioLink, BioPage

MEDIA_CHOICES = 24
POST_LINK_CHOICES = 12
STATS_RANGES = (7, 30, 90)


def _workspace(request, workspace_id, *, manage: bool = False) -> Workspace:
    workspace = get_object_or_404(Workspace, id=workspace_id)
    membership = WorkspaceMembership.objects.filter(user=request.user, workspace=workspace).first()
    if membership is None:
        raise PermissionDenied("You are not a member of this workspace.")
    if not services.can_view(membership):
        raise PermissionDenied("Permission denied: view_analytics")
    if manage and not services.can_manage(membership):
        raise PermissionDenied("Permission denied: managing link-in-bio pages")
    request.bio_membership = membership
    return workspace


def _page(workspace, page_id) -> BioPage:
    return get_object_or_404(
        BioPage.objects.select_related("workspace__organization", "avatar"), pk=page_id, workspace=workspace
    )


def _back(workspace, page, anchor: str = ""):
    url = reverse("linkinbio:edit", kwargs={"workspace_id": workspace.id, "page_id": page.id})
    return redirect(f"{url}#{anchor}" if anchor else url)


def _parse_local(value: str, workspace) -> dt.datetime | None:
    """A ``datetime-local`` value read in the workspace's timezone; blank is no limit."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError as exc:
        raise services.BioError("Enter the date and time as YYYY-MM-DD HH:MM.") from exc
    tz = zoneinfo.ZoneInfo(workspace.effective_timezone or "UTC")
    return parsed.replace(tzinfo=tz) if parsed.tzinfo is None else parsed


def _local_value(value, workspace) -> str:
    if value is None:
        return ""
    tz = zoneinfo.ZoneInfo(workspace.effective_timezone or "UTC")
    return value.astimezone(tz).strftime("%Y-%m-%dT%H:%M")


def _fail(request, exc: services.BioError):
    messages.error(request, exc.message)


@login_required
@require_GET
def page_list(request, workspace_id):
    workspace = _workspace(request, workspace_id)
    ent = get_entitlements(workspace.organization)
    pages = list(workspace.bio_pages.all())
    return render(
        request,
        "linkinbio/list.html",
        {
            "workspace": workspace,
            "pages": pages,
            "can_manage": services.can_manage(request.bio_membership),
            "page_limit": ent.max_bio_pages,
            "shows_badge": ent.shows_bio_badge,
        },
    )


@login_required
@require_POST
def page_create(request, workspace_id):
    workspace = _workspace(request, workspace_id, manage=True)
    try:
        page = services.create_page(
            workspace, user=request.user, title=request.POST.get("title", ""), slug=request.POST.get("slug", "")
        )
    except PlanLimitError as exc:
        messages.error(request, exc.message)
        return redirect("linkinbio:list", workspace_id=workspace.id)
    except services.BioError as exc:
        _fail(request, exc)
        return redirect("linkinbio:list", workspace_id=workspace.id)
    messages.success(request, "Page created. Add your first link below.")
    return _back(workspace, page)


def _media_choices(workspace):
    return list(
        MediaAsset.objects.filter(services._library_q(workspace), media_type=MediaAsset.MediaType.IMAGE)
        .exclude(file="")
        .order_by("-created_at")[:MEDIA_CHOICES]
    )


def _post_link_rows(workspace) -> list[dict]:
    rows, seen = [], set()
    published = (
        PlatformPost.objects.filter(post__workspace=workspace, status=PlatformPost.Status.PUBLISHED)
        .select_related("post", "post__bio_link", "social_account")
        .order_by("-published_at", "id")[: POST_LINK_CHOICES * 3]
    )
    for platform_post in published:
        post = platform_post.post
        if post.id in seen:
            continue
        seen.add(post.id)
        link = getattr(post, "bio_link", None)
        rows.append(
            {
                "post": post,
                "caption": (post.caption or post.title or "Untitled post")[:90],
                "published_at": platform_post.published_at,
                "url": link.url if link else "",
            }
        )
        if len(rows) >= POST_LINK_CHOICES:
            break
    return rows


@login_required
@require_GET
def page_edit(request, workspace_id, page_id):
    workspace = _workspace(request, workspace_id)
    page = _page(workspace, page_id)
    can_manage = services.can_manage(request.bio_membership)
    try:
        days = int(request.GET.get("days", 30))
    except ValueError:
        days = 30
    if days not in STATS_RANGES:
        days = 30
    now = dt.datetime.now(dt.UTC)
    links = [
        {
            "link": link,
            "visible": link.is_visible(now),
            "starts_local": _local_value(link.starts_at, workspace),
            "ends_local": _local_value(link.ends_at, workspace),
        }
        for link in page.links.select_related("thumbnail").order_by("position", "id")
    ]
    selected = set(page.social_accounts.values_list("id", flat=True))
    from providers import PROVIDER_REGISTRY

    accounts = [
        {
            "account": account,
            "has_profile": bool(
                PROVIDER_REGISTRY.get(account.platform) and PROVIDER_REGISTRY[account.platform].profile_url(account)
            ),
            "selected": account.id in selected,
        }
        for account in SocialAccount.objects.filter(workspace=workspace).order_by("account_name")
    ]
    public_path = reverse("linkinbio_public:page", kwargs={"slug": page.slug})
    kit = services._brand_kit(page)
    return render(
        request,
        "linkinbio/edit.html",
        {
            "workspace": workspace,
            "page": page,
            "links": links,
            "can_manage": can_manage,
            "themes": BioPage.Theme.choices,
            "brand_logo": None if page.avatar_id else services.effective_avatar(page, kit),
            "brand_accent": kit.primary_color,
            "icons": BioLink.Icon.choices,
            "media_choices": _media_choices(workspace) if can_manage else [],
            "accounts": accounts,
            "post_rows": _post_link_rows(workspace),
            "stats": services.stats_summary(page, days),
            "stats_ranges": STATS_RANGES,
            "public_url": request.build_absolute_uri(public_path),
            "timezone_name": workspace.effective_timezone or "UTC",
            "shows_badge": services.shows_badge(page),
            "custom_domains_enabled": settings.LINKINBIO_CUSTOM_DOMAINS_ENABLED,
            "cname_target": settings.LINKINBIO_CNAME_TARGET or request.get_host(),
        },
    )


@login_required
@require_POST
def page_save(request, workspace_id, page_id):
    workspace = _workspace(request, workspace_id, manage=True)
    page = _page(workspace, page_id)
    post = request.POST
    data = {
        "title": post.get("title", ""),
        "slug": post.get("slug", ""),
        "bio": post.get("bio", ""),
        "theme": post.get("theme", page.theme),
        "accent_color": post.get("accent_color", ""),
        "avatar": post.get("avatar") or None,
        "show_latest_posts": post.get("show_latest_posts") == "on",
        "posts_account": post.get("posts_account") or None,
        "is_published": post.get("is_published") == "on",
    }
    if "custom_domain" in post:
        data["custom_domain"] = post.get("custom_domain", "")
    try:
        services.update_page(page, social_account_ids=post.getlist("social_accounts"), **data)
    except services.BioError as exc:
        _fail(request, exc)
        return _back(workspace, page)
    except ValueError:
        messages.error(request, "One of the choices wasn't valid.")
        return _back(workspace, page)
    messages.success(request, "Page saved.")
    return _back(workspace, page)


@login_required
@require_POST
def page_delete(request, workspace_id, page_id):
    workspace = _workspace(request, workspace_id, manage=True)
    services.delete_page(_page(workspace, page_id))
    messages.success(request, "Page deleted.")
    return redirect("linkinbio:list", workspace_id=workspace.id)


def _link_data(request, workspace) -> dict:
    post = request.POST
    return {
        "title": post.get("title", ""),
        "url": post.get("url", ""),
        "icon": post.get("icon", ""),
        "thumbnail": post.get("thumbnail") or None,
        "starts_at": _parse_local(post.get("starts_at", ""), workspace),
        "ends_at": _parse_local(post.get("ends_at", ""), workspace),
        "is_active": post.get("is_active", "on") == "on",
    }


@login_required
@require_POST
def link_add(request, workspace_id, page_id):
    workspace = _workspace(request, workspace_id, manage=True)
    page = _page(workspace, page_id)
    try:
        services.add_link(page, **_link_data(request, workspace))
    except services.BioError as exc:
        _fail(request, exc)
    else:
        messages.success(request, "Link added.")
    return _back(workspace, page, "links")


@login_required
@require_POST
def link_update(request, workspace_id, page_id, link_id):
    workspace = _workspace(request, workspace_id, manage=True)
    page = _page(workspace, page_id)
    link = get_object_or_404(BioLink, pk=link_id, page=page)
    data = _link_data(request, workspace)
    data["is_active"] = request.POST.get("is_active") == "on"
    try:
        services.update_link(link, **data)
    except services.BioError as exc:
        _fail(request, exc)
    else:
        messages.success(request, "Link saved.")
    return _back(workspace, page, f"link-{link.id}")


@login_required
@require_POST
def link_delete(request, workspace_id, page_id, link_id):
    workspace = _workspace(request, workspace_id, manage=True)
    page = _page(workspace, page_id)
    services.delete_link(get_object_or_404(BioLink, pk=link_id, page=page))
    messages.success(request, "Link removed.")
    return _back(workspace, page, "links")


@login_required
@require_POST
def link_move(request, workspace_id, page_id, link_id):
    workspace = _workspace(request, workspace_id, manage=True)
    page = _page(workspace, page_id)
    link = get_object_or_404(BioLink, pk=link_id, page=page)
    try:
        services.move_link(link, request.POST.get("direction", ""))
    except services.BioError as exc:
        _fail(request, exc)
    return _back(workspace, page, f"link-{link.id}")


@login_required
@require_POST
def post_link_save(request, workspace_id, page_id, post_id):
    workspace = _workspace(request, workspace_id, manage=True)
    page = _page(workspace, page_id)
    try:
        services.set_post_link(workspace, post_id, request.POST.get("url", ""))
    except services.BioError as exc:
        _fail(request, exc)
    else:
        messages.success(request, "Post link saved.")
    return _back(workspace, page, "posts")
