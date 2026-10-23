"""Link-in-bio rules shared by the editor, the public page, the REST API and MCP.

Nothing here reads a request except :func:`record_view` and :func:`record_click`,
which turn the visitor into a throw-away, day-salted token: the address and
browser are hashed together with the day and the server secret, used to dedupe
and rate-limit inside the cache, and never written to the database.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import logging
import uuid

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F, Prefetch, Q
from django.utils import timezone

from apps.billing.entitlements import check_can_add_bio_page, get_entitlements
from apps.composer.models import PlatformPost, PostMedia
from apps.credentials.models import PlatformCredential
from apps.media_library.models import MediaAsset
from apps.social_accounts.models import SocialAccount

from .models import NIL_TARGET, BioDailyStat, BioLink, BioPage, BioPostLink

logger = logging.getLogger(__name__)

PAGE_CACHE_SECONDS = 60
MAX_LINKS = 50
LATEST_POSTS_LIMIT = 12
# Per visitor token and per page, per minute.
CLICKS_PER_VISITOR_PER_MINUTE = 30
EVENTS_PER_PAGE_PER_MINUTE = 600
STATS_DAYS_MAX = 90

_BOT_MARKERS = ("bot", "crawl", "spider", "preview", "headless", "curl/", "wget/", "python-requests")

PAGE_FIELDS = (
    "slug",
    "title",
    "bio",
    "avatar",
    "theme",
    "accent_color",
    "show_latest_posts",
    "posts_account",
    "is_published",
    "custom_domain",
)
LINK_FIELDS = ("title", "url", "icon", "thumbnail", "starts_at", "ends_at", "is_active")


class BioError(Exception):
    """A refused change. ``field`` names the input at fault when there is one."""

    def __init__(self, message: str, field: str = ""):
        super().__init__(message)
        self.message = message
        self.field = field


# ---------------------------------------------------------------------------
# Who may do what
# ---------------------------------------------------------------------------


def can_view(membership) -> bool:
    return bool(membership and membership.effective_permissions.get("view_analytics", False))


def can_manage(membership) -> bool:
    """Editing pages is for roles that manage the channels (owner, manager)."""
    return bool(membership and membership.effective_permissions.get("manage_social_accounts", False))


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


def page_cache_key(slug: str) -> str:
    return f"linkinbio:page:{slug}"


def invalidate(*slugs: str) -> None:
    cache.delete_many([page_cache_key(s) for s in slugs if s])


def _clean(instance, exclude=()):
    try:
        instance.full_clean(exclude=list(exclude))
    except ValidationError as exc:
        field, messages = next(iter(exc.message_dict.items()))
        raise BioError(messages[0], "" if field == "__all__" else field) from exc


def _media(workspace, asset_id, field: str):
    """An image from this workspace's library (or its organization's shared one), or None for a blank value."""
    if not asset_id:
        return None
    try:
        asset = (
            MediaAsset.objects.filter(pk=asset_id)
            .filter(_library_q(workspace))
            .filter(media_type__in=(MediaAsset.MediaType.IMAGE, MediaAsset.MediaType.GIF))
            .first()
        )
    except (ValidationError, ValueError):
        asset = None
    if asset is None:
        raise BioError("Pick an image from this workspace's media library.", field)
    return asset


def _library_q(workspace) -> Q:
    return Q(workspace=workspace) | Q(workspace__isnull=True, organization_id=workspace.organization_id)


def _account(workspace, account_id, field: str):
    if not account_id:
        return None
    try:
        account = SocialAccount.objects.filter(pk=account_id, workspace=workspace).first()
    except (ValidationError, ValueError):
        account = None
    if account is None:
        raise BioError("That account isn't connected to this workspace.", field)
    return account


def _normalise_page_values(workspace, data: dict) -> dict:
    values = {k: data[k] for k in PAGE_FIELDS if k in data}
    if "slug" in values:
        values["slug"] = (values["slug"] or "").strip().lower()
    if "title" in values:
        values["title"] = (values["title"] or "").strip()
    if "bio" in values:
        values["bio"] = (values["bio"] or "").strip()
    if "accent_color" in values:
        values["accent_color"] = (values["accent_color"] or "").strip()
    if "custom_domain" in values:
        values["custom_domain"] = (values["custom_domain"] or "").strip().lower().rstrip(".") or None
    if "avatar" in values:
        values["avatar"] = _media(workspace, values["avatar"], "avatar")
    if "posts_account" in values:
        values["posts_account"] = _account(workspace, values["posts_account"], "posts_account")
    return values


def create_page(workspace, *, user=None, **data) -> BioPage:
    check_can_add_bio_page(workspace.organization)
    values = _normalise_page_values(workspace, data)
    values.setdefault("title", "")
    page = BioPage(workspace=workspace, **values)
    _clean(page)
    page.save()
    invalidate(page.slug)
    return page


def update_page(page: BioPage, *, social_account_ids=None, **data) -> BioPage:
    old_slug = page.slug
    for name, value in _normalise_page_values(page.workspace, data).items():
        setattr(page, name, value)
    _clean(page)
    with transaction.atomic():
        page.save()
        if social_account_ids is not None:
            try:
                accounts = list(SocialAccount.objects.filter(workspace=page.workspace, id__in=list(social_account_ids)))
            except (ValidationError, ValueError):
                accounts = []
            if len(accounts) != len(set(str(i) for i in social_account_ids)):
                raise BioError("One of those accounts isn't connected to this workspace.", "social_accounts")
            page.social_accounts.set(accounts)
    invalidate(old_slug, page.slug)
    return page


def delete_page(page: BioPage) -> None:
    slug = page.slug
    page.delete()
    invalidate(slug)


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------


def _link_values(page, data: dict) -> dict:
    values = {k: data[k] for k in LINK_FIELDS if k in data}
    for name in ("title", "url"):
        if name in values:
            values[name] = (values[name] or "").strip()
    if "icon" in values:
        values["icon"] = values["icon"] or ""
    if "thumbnail" in values:
        values["thumbnail"] = _media(page.workspace, values["thumbnail"], "thumbnail")
    return values


def add_link(page: BioPage, **data) -> BioLink:
    if page.links.count() >= MAX_LINKS:
        raise BioError(f"A page can have at most {MAX_LINKS} links.")
    last = page.links.order_by("-position").values_list("position", flat=True).first()
    link = BioLink(page=page, position=0 if last is None else last + 1, **_link_values(page, data))
    _clean(link, exclude=["page"])
    link.save()
    invalidate(page.slug)
    return link


def update_link(link: BioLink, **data) -> BioLink:
    for name, value in _link_values(link.page, data).items():
        setattr(link, name, value)
    _clean(link, exclude=["page"])
    link.save()
    invalidate(link.page.slug)
    return link


def delete_link(link: BioLink) -> None:
    page = link.page
    link.delete()
    _renumber(page)
    invalidate(page.slug)


def _renumber(page: BioPage) -> None:
    ordered = list(page.links.order_by("position", "created_at", "id"))
    for index, item in enumerate(ordered):
        item.position = index
    BioLink.objects.bulk_update(ordered, ["position"])


def move_link(link: BioLink, direction: str) -> BioLink:
    """Swap ``link`` with its neighbour above ("up") or below ("down"); a no-op at either end."""
    if direction not in ("up", "down"):
        raise BioError("direction must be up or down.", "direction")
    with transaction.atomic():
        page = BioPage.objects.select_for_update().get(pk=link.page_id)
        _renumber(page)
        ordered = list(page.links.order_by("position", "id"))
        index = next(i for i, item in enumerate(ordered) if item.pk == link.pk)
        other = index - 1 if direction == "up" else index + 1
        if 0 <= other < len(ordered):
            ordered[index], ordered[other] = ordered[other], ordered[index]
            for position, item in enumerate(ordered):
                item.position = position
            BioLink.objects.bulk_update(ordered, ["position"])
    invalidate(page.slug)
    link.refresh_from_db()
    return link


def set_post_link(workspace, post_id, url: str) -> BioPostLink | None:
    """Attach (or with a blank ``url``, remove) the address a published post's grid tile opens."""
    from apps.composer.models import Post

    post = Post.objects.filter(pk=post_id, workspace=workspace).first()
    if post is None:
        raise BioError("Post not found.", "post")
    url = (url or "").strip()
    if not url:
        BioPostLink.objects.filter(post=post).delete()
        invalidate(*workspace.bio_pages.values_list("slug", flat=True))
        return None
    link = BioPostLink.objects.filter(post=post).first() or BioPostLink(post=post)
    link.url = url
    _clean(link, exclude=["post"])
    link.save()
    invalidate(*workspace.bio_pages.values_list("slug", flat=True))
    return link


# ---------------------------------------------------------------------------
# What the public page shows
# ---------------------------------------------------------------------------


def visible_links(page: BioPage, now: dt.datetime | None = None) -> list[BioLink]:
    now = now or timezone.now()
    return [link for link in page.links.select_related("thumbnail").order_by("position", "id") if link.is_visible(now)]


def social_links(page: BioPage) -> list[dict]:
    """Icons for the page's chosen accounts that are still connected and have a public profile."""
    from providers import PROVIDER_REGISTRY

    rows = []
    for account in page.social_accounts.filter(connection_status=SocialAccount.ConnectionStatus.CONNECTED):
        provider = PROVIDER_REGISTRY.get(account.platform)
        url = provider.profile_url(account) if provider else ""
        if url:
            label = PlatformCredential.Platform(account.platform).label
            rows.append({"platform": account.platform, "label": label, "url": url, "name": account.account_name})
    return rows


def latest_posts(page: BioPage, limit: int = LATEST_POSTS_LIMIT) -> list[dict]:
    """Published posts, newest first, as tiles; a tile is a link only when a post link was attached."""
    media = Prefetch(
        "post__media_attachments",
        queryset=PostMedia.objects.select_related("media_asset").order_by("position", "id"),
    )
    qs = (
        PlatformPost.objects.filter(
            post__workspace_id=page.workspace_id,
            status=PlatformPost.Status.PUBLISHED,
            published_at__isnull=False,
        )
        .select_related("post", "post__bio_link")
        .prefetch_related(media)
        .order_by("-published_at", "id")
    )
    if page.posts_account_id:
        qs = qs.filter(social_account_id=page.posts_account_id)
    tiles, seen = [], set()
    for platform_post in qs[: limit * 4]:
        post = platform_post.post
        if post.id in seen:
            continue
        attachments = list(post.media_attachments.all())
        image = attachments[0].media_asset.preview_url if attachments else ""
        if not image:
            continue
        seen.add(post.id)
        link = getattr(post, "bio_link", None)
        tiles.append(
            {
                "id": link.id if link else None,
                "url": link.url if link else "",
                "image": image,
                "alt": (attachments[0].alt_text or post.caption or "")[:120],
            }
        )
        if len(tiles) >= limit:
            break
    return tiles


def shows_badge(page: BioPage) -> bool:
    return get_entitlements(page.workspace.organization).shows_bio_badge


# ---------------------------------------------------------------------------
# Counting: no cookies, no addresses stored
# ---------------------------------------------------------------------------


def _today() -> dt.date:
    return timezone.now().astimezone(dt.UTC).date()


def _visitor_token(request, day: dt.date) -> str:
    """Hash of address + browser, salted with the day and the server secret; lives only in the cache."""
    from apps.api.limits import _client_ip

    material = f"{day.isoformat()}|{_client_ip(request) or ''}|{request.META.get('HTTP_USER_AGENT', '')}"
    key = f"linkinbio:{settings.SECRET_KEY}".encode()
    return hmac.new(key, material.encode(), hashlib.sha256).hexdigest()[:32]


def is_bot(request) -> bool:
    agent = (request.META.get("HTTP_USER_AGENT") or "").lower()
    return not agent or any(marker in agent for marker in _BOT_MARKERS)


def _within(key: str, limit: int, window: int = 60) -> bool:
    """Count one hit on ``key`` and say whether it is still within ``limit`` per ``window`` seconds."""
    if cache.add(key, 1, window):
        return limit >= 1
    try:
        return cache.incr(key) <= limit
    except ValueError:
        cache.set(key, 1, window)
        return True


def _bump(page: BioPage, day: dt.date, kind: str, target: uuid.UUID, unique: bool) -> None:
    row, _created = BioDailyStat.objects.get_or_create(page=page, day=day, kind=kind, target_id=target)
    BioDailyStat.objects.filter(pk=row.pk).update(count=F("count") + 1, uniques=F("uniques") + int(unique))


def _first_today(page: BioPage, kind: str, target: uuid.UUID, token: str, day: dt.date) -> bool:
    return bool(cache.add(f"linkinbio:seen:{page.id}:{day}:{kind}:{target}:{token}", 1, 60 * 60 * 30))


def record_view(page: BioPage, request) -> bool:
    """Count one page view; False when it was skipped (bots, over the rate limit)."""
    if request.method != "GET" or is_bot(request):
        return False
    day = _today()
    token = _visitor_token(request, day)
    if not _within(f"linkinbio:rl:page:{page.id}", EVENTS_PER_PAGE_PER_MINUTE):
        return False
    if not _within(f"linkinbio:rl:view:{page.id}:{token}", CLICKS_PER_VISITOR_PER_MINUTE):
        return False
    _bump(page, day, BioDailyStat.Kind.VIEW, NIL_TARGET, _first_today(page, "view", NIL_TARGET, token, day))
    return True


def record_click(page: BioPage, target_id: uuid.UUID, request) -> str:
    """Count a click on a link or post tile of ``page``.

    Returns ``"counted"``, ``"ignored"`` (a bot, or a target that isn't on the page)
    or ``"limited"`` (too many from one visitor or onto one page).
    """
    if is_bot(request):
        return "ignored"
    link = next((item for item in visible_links(page) if item.id == target_id), None)
    if link is not None:
        kind = BioDailyStat.Kind.LINK
    elif BioPostLink.objects.filter(pk=target_id, post__workspace_id=page.workspace_id).exists():
        kind = BioDailyStat.Kind.TILE
    else:
        return "ignored"
    day = _today()
    token = _visitor_token(request, day)
    if not _within(f"linkinbio:rl:click:{page.id}:{token}", CLICKS_PER_VISITOR_PER_MINUTE) or not _within(
        f"linkinbio:rl:page:{page.id}", EVENTS_PER_PAGE_PER_MINUTE
    ):
        return "limited"
    _bump(page, day, kind, target_id, _first_today(page, kind, target_id, token, day))
    return "counted"


# ---------------------------------------------------------------------------
# Reading the counters
# ---------------------------------------------------------------------------


def stats_summary(page: BioPage, days: int = 30) -> dict:
    """Views, clicks and per-link clicks over the last ``days`` UTC days (today included)."""
    days = max(1, min(int(days), STATS_DAYS_MAX))
    end = _today()
    start = end - dt.timedelta(days=days - 1)
    rows = list(BioDailyStat.objects.filter(page=page, day__gte=start, day__lte=end))
    series = {start + dt.timedelta(days=i): {"views": 0, "clicks": 0} for i in range(days)}
    per_target: dict[uuid.UUID, dict] = {}
    totals = {"views": 0, "unique_visitors": 0, "clicks": 0}
    for row in rows:
        if row.kind == BioDailyStat.Kind.VIEW:
            series[row.day]["views"] += row.count
            totals["views"] += row.count
            totals["unique_visitors"] += row.uniques
        else:
            series[row.day]["clicks"] += row.count
            totals["clicks"] += row.count
            bucket = per_target.setdefault(row.target_id, {"kind": row.kind, "clicks": 0, "unique_clicks": 0})
            bucket["clicks"] += row.count
            bucket["unique_clicks"] += row.uniques
    titles = {link.id: link.title for link in page.links.all()}
    links = [
        {
            "link_id": target,
            "title": titles.get(target, "Post tile" if bucket["kind"] == BioDailyStat.Kind.TILE else "Deleted link"),
            "kind": bucket["kind"],
            "clicks": bucket["clicks"],
            "unique_clicks": bucket["unique_clicks"],
        }
        for target, bucket in per_target.items()
    ]
    for link_id, title in titles.items():
        if link_id not in per_target:
            links.append({"link_id": link_id, "title": title, "kind": "link", "clicks": 0, "unique_clicks": 0})
    links.sort(key=lambda item: (-item["clicks"], item["title"]))
    return {
        "days": days,
        "start": start,
        "end": end,
        "totals": totals,
        "series": [{"day": day, **values} for day, values in series.items()],
        "links": links,
    }
