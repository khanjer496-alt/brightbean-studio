"""Short links, UTM tagging and click counting shared by the composer, the redirect, the REST API and MCP.

Links are made when an account's copy of a post is sent for review or *scheduled*, so each network gets its
own ``utm_source`` and its own short link, and reviewers approve the final text (:func:`prepare_platform_post`). Nothing here reads a request except
:func:`record_hit`, which turns the visitor into a throw-away token: address and browser are hashed under a
random salt that exists only in the cache for about two days, used to dedupe and rate-limit there, and never
written to the database or the logs.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import logging
import re
import secrets
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import F, Sum
from django.utils import timezone
from django.utils.text import slugify

from apps.billing.entitlements import check_can_create_short_link
from apps.linkinbio.validators import validate_http_url

from . import shorteners
from .models import LinkDailyStat, LinkSettings, ShortLink

logger = logging.getLogger(__name__)

STATS_DAYS_MAX = 90
MAX_UTM_CUSTOM = 10
UTM_VALUE_MAX = 100
# Per visitor token and link, per minute: counting stops past this, the redirect still happens.
CLICKS_PER_VISITOR_PER_MINUTE = 20
# Past this the visitor is told to slow down instead of being redirected (a script hammering one link).
HARD_LIMIT_PER_VISITOR_PER_MINUTE = 300
EVENTS_PER_LINK_PER_MINUTE = 1200
CODE_ATTEMPTS = 5

_URL_RE = re.compile(r"https?://[^\s<>\"]+", re.IGNORECASE)
_TRAILING = ".,!?:;)]}'’”"
_PARAM_NAME = re.compile(r"[A-Za-z0-9_.-]{1,40}")


class LinkError(Exception):
    """A request that can't be honoured; ``message`` is safe to show."""

    def __init__(self, message: str, field: str = ""):
        super().__init__(message)
        self.message = message
        self.field = field


# ---------------------------------------------------------------------------
# Workspace settings
# ---------------------------------------------------------------------------


def get_settings(workspace) -> LinkSettings:
    obj, _created = LinkSettings.objects.get_or_create(workspace=workspace)
    return obj


def find_settings(workspace_id) -> LinkSettings | None:
    return LinkSettings.objects.filter(workspace_id=workspace_id).first()


def clean_custom_params(raw) -> dict[str, str]:
    """Extra UTM-style parameters: at most ten, plain names, short values."""
    if raw in (None, ""):
        return {}
    if not isinstance(raw, dict):
        raise LinkError("Custom parameters must be name and value pairs.", "utm_custom")
    cleaned: dict[str, str] = {}
    for name, value in raw.items():
        name, value = str(name).strip(), str(value).strip()
        if not name and not value:
            continue
        if not _PARAM_NAME.fullmatch(name):
            raise LinkError(f"'{name}' isn't a usable parameter name. Use letters, numbers, - _ or .", "utm_custom")
        if name.lower() in ("utm_source", "utm_medium", "utm_campaign"):
            raise LinkError(f"{name} is set above; leave it out of the custom parameters.", "utm_custom")
        if len(value) > UTM_VALUE_MAX:
            raise LinkError(f"The value for {name} is too long.", "utm_custom")
        cleaned[name] = value
    if len(cleaned) > MAX_UTM_CUSTOM:
        raise LinkError(f"Use at most {MAX_UTM_CUSTOM} custom parameters.", "utm_custom")
    return cleaned


SETTINGS_FIELDS = (
    "shorten_mode",
    "provider",
    "provider_domain",
    "provider_base_url",
    "custom_short_domain",
    "utm_enabled",
    "utm_medium",
    "utm_campaign_from",
    "utm_campaign",
    "utm_custom",
)


def update_settings(workspace, *, api_key: str | None = None, clear_api_key: bool = False, **data) -> LinkSettings:
    """Save a workspace's link preferences. ``api_key`` of None/"" keeps the stored key unless ``clear_api_key``."""
    obj = get_settings(workspace)
    if "utm_custom" in data:
        data["utm_custom"] = clean_custom_params(data["utm_custom"])
    for name in SETTINGS_FIELDS:
        if name in data:
            value = data[name]
            if name in ("custom_short_domain",):
                value = (value or "").strip().lower() or None
            elif isinstance(value, str):
                value = value.strip()
            setattr(obj, name, value)
    if clear_api_key:
        obj.api_key = ""
    elif api_key:
        obj.api_key = api_key.strip()
    if (
        obj.custom_short_domain
        and LinkSettings.objects.filter(custom_short_domain=obj.custom_short_domain).exclude(pk=obj.pk).exists()
    ):
        raise LinkError("That short domain is already used by another workspace.", "custom_short_domain")
    problem = shorteners.settings_problem(obj)
    if problem:
        raise LinkError(problem[1], problem[0])
    try:
        obj.full_clean(exclude=["workspace"])
    except ValidationError as exc:
        field, messages = next(iter(exc.message_dict.items()))
        raise LinkError(" ".join(messages), field if field != "__all__" else "") from exc
    try:
        obj.save()
    except IntegrityError as exc:
        raise LinkError("That short domain is already used by another workspace.", "custom_short_domain") from exc
    return obj


# ---------------------------------------------------------------------------
# Finding and tagging URLs
# ---------------------------------------------------------------------------


def extract_urls(text: str) -> list[str]:
    """Every distinct http(s) address in ``text``, in order, without trailing sentence punctuation."""
    seen: list[str] = []
    for match in _URL_RE.finditer(text or ""):
        url = match.group(0).rstrip(_TRAILING)
        if url and url not in seen:
            seen.append(url)
    return seen


def _replace_urls(text: str, mapping: dict[str, str]) -> str:
    if not mapping:
        return text

    def swap(match):
        url = match.group(0)
        stripped = url.rstrip(_TRAILING)
        tail = url[len(stripped) :]
        return mapping.get(stripped, stripped) + tail

    return _URL_RE.sub(swap, text)


def apply_utm(url: str, params: dict[str, str]) -> str:
    """``url`` with ``params`` added. A parameter the address already carries is left as the author wrote it."""
    parts = urlsplit(url)
    existing = parse_qsl(parts.query, keep_blank_values=True)
    have = {name.lower() for name, _ in existing}
    extra = [(name, value) for name, value in params.items() if value and name.lower() not in have]
    if not extra:
        return url
    query = parts.query + ("&" if parts.query else "") + urlencode(extra)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def campaign_for(link_settings: LinkSettings, post) -> str:
    source = link_settings.utm_campaign_from
    value = ""
    if post is not None:
        if source == LinkSettings.CampaignFrom.POST:
            # The title, else the caption's first words; an address in the caption is no campaign name.
            words = _URL_RE.sub(" ", post.title or post.caption or "").split()
            value = slugify(" ".join(words[:8])[:60])
        elif source == LinkSettings.CampaignFrom.CATEGORY and post.category_id:
            value = slugify(post.category.name)
        elif source == LinkSettings.CampaignFrom.TAG and post.tags:
            value = slugify(str(post.tags[0]))
    return (value or link_settings.utm_campaign or "")[:UTM_VALUE_MAX]


def utm_params(link_settings: LinkSettings, post, social_account) -> dict[str, str]:
    """The UTM set for one account's copy of ``post``: its network is the source."""
    params = {
        "utm_source": social_account.platform if social_account is not None else "",
        "utm_medium": link_settings.utm_medium,
        "utm_campaign": campaign_for(link_settings, post),
    }
    params.update(link_settings.utm_custom or {})
    return {name: value for name, value in params.items() if value}


def utm_preview(workspace, *, url: str, platform: str = "", title: str = "", category: str = "", tag: str = "") -> str:
    """The address as it would be published to an account on ``platform`` (for the settings page)."""
    link_settings = find_settings(workspace.id) or LinkSettings(workspace=workspace)
    post = SimpleNamespace(
        title=title,
        caption="",
        tags=[tag] if tag else [],
        category_id=1 if category else None,
        category=SimpleNamespace(name=category),
    )
    account = SimpleNamespace(platform=platform or "linkedin")
    return apply_utm(url, utm_params(link_settings, post, account))


# ---------------------------------------------------------------------------
# Making links
# ---------------------------------------------------------------------------


def validate_destination(url: str) -> str:
    url = (url or "").strip()
    try:
        validate_http_url(url)
    except ValidationError as exc:
        raise LinkError(exc.messages[0], "url") from exc
    parts = urlsplit(url)
    from .shorteners import app_base_url

    own = urlsplit(app_base_url())
    if parts.hostname and parts.hostname.lower() == (own.hostname or "").lower() and parts.path.startswith("/s/"):
        raise LinkError("That is already a short link.", "url")
    return url


def create_short_link(
    workspace,
    url: str,
    *,
    user=None,
    post=None,
    platform_post=None,
    social_account=None,
    link_settings: LinkSettings | None = None,
) -> ShortLink:
    """Make a short link to ``url`` with the workspace's chosen service.

    Raises :class:`LinkError` for a bad address or a plan limit, and
    :class:`~apps.links.shorteners.ShortenerError` when an external service fails.
    """
    from apps.billing.entitlements import PlanLimitError

    url = validate_destination(url)
    link_settings = link_settings or get_settings(workspace)
    try:
        check_can_create_short_link(workspace.organization)
    except PlanLimitError as exc:
        raise LinkError(str(exc)) from exc
    shortener = shorteners.shortener_for(link_settings)
    base = {
        "workspace": workspace,
        "destination_url": url,
        "provider": shortener.key,
        "post": post,
        "platform_post": platform_post,
        "social_account": social_account or (platform_post.social_account if platform_post else None),
        "created_by": user,
    }
    for _attempt in range(CODE_ATTEMPTS):
        result = shortener.create(url)
        try:
            with transaction.atomic():
                return ShortLink.objects.create(
                    **base, code=result.code, short_url=result.short_url, external_id=result.external_id
                )
        except IntegrityError:  # a built-in code collided; draw another
            if shortener.key != "builtin":
                raise
    raise LinkError("Couldn't pick a free short address. Try again.")


def set_active(link: ShortLink, active: bool) -> ShortLink:
    link.is_active = active
    link.save(update_fields=["is_active"])
    return link


def is_short_url(workspace_id, url: str, link_settings: LinkSettings | None, urls=None) -> bool:
    """True for an address that is already one of our short links (so it is never shortened twice).

    Our own ``/s/`` addresses, anything on the workspace's chosen service domain, and any short address we
    made earlier for this workspace all count.
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    own = urlsplit(shorteners.app_base_url(link_settings.custom_short_domain if link_settings else None))
    if host and host == (own.hostname or "").lower() and parts.path.startswith("/s/"):
        return True
    if link_settings is not None and link_settings.provider_domain and host == link_settings.provider_domain.lower():
        return True
    return ShortLink.objects.filter(workspace_id=workspace_id, short_url=url).exists()


def effective_shorten(post, link_settings: LinkSettings | None) -> bool:
    """Whether links in ``post`` get shortened: the post's own choice, else the workspace default."""
    if link_settings is None or link_settings.shorten_mode == LinkSettings.ShortenMode.NEVER:
        return False
    choice = getattr(post, "shorten_links", None)
    if choice is None:
        return link_settings.shorten_mode == LinkSettings.ShortenMode.ALWAYS
    return bool(choice)


def expected_short_length(link_settings: LinkSettings | None) -> int:
    return shorteners.shortener_for(link_settings or LinkSettings()).expected_length()


def counting_text(text: str, link_settings: LinkSettings | None, *, shorten: bool) -> str:
    """``text`` as it will be counted once its links are short: each address swapped for one of the short length."""
    if not shorten or not text:
        return text
    length = expected_short_length(link_settings)
    placeholder = "https://" + "x" * max(length - len("https://"), 1)
    return _replace_urls(text, {url: placeholder for url in extract_urls(text)})


def link_counting(workspace, choice="", *, post=None):
    """A function that rewrites a text to the length it will count once its links are short, or None.

    ``choice`` is the composer's "1" / "0" / "" (undecided); ``post`` supplies the saved choice instead.
    """
    link_settings = find_settings(workspace.id)
    if link_settings is None:
        return None
    if post is not None:
        shorten = effective_shorten(post, link_settings)
    else:
        shorten = effective_shorten(SimpleNamespace(shorten_links={"1": True, "0": False}.get(choice)), link_settings)
    if not shorten:
        return None
    return lambda text: counting_text(text, link_settings, shorten=True)


def composer_prefs(workspace, post=None) -> dict:
    """What the composer needs: the mode, the short length and prefix, and this post's saved choice."""
    link_settings = find_settings(workspace.id)
    choice = getattr(post, "shorten_links", None)
    return {
        "mode": link_settings.shorten_mode if link_settings else LinkSettings.ShortenMode.NEVER,
        "short_length": expected_short_length(link_settings),
        "short_prefix": shorteners.app_base_url(link_settings.custom_short_domain if link_settings else None) + "/s/",
        "choice": "" if choice is None else ("1" if choice else "0"),
    }


# ---------------------------------------------------------------------------
# Schedule time
# ---------------------------------------------------------------------------

# A copy's links are prepared when it is submitted for review (so reviewers and the approval fingerprint see the
# final addresses) and when it is scheduled.
PREPARE_STATUSES = ("pending_review", "scheduled")


def _short_for(target, ctx) -> str:
    """The short address for ``target`` in this copy: reused when one exists, else made now.

    An external service that fails falls back to a built-in link (still counted, still short); a plan limit or
    a refused address leaves the tagged address as it is. Either way ``ctx["problems"]`` says why.
    """
    existing = ctx["existing"].get(target)
    if existing is not None:
        return existing
    made = {
        "platform_post": ctx["pp"],
        "post": ctx["pp"].post,
        "social_account": ctx["pp"].social_account,
    }
    try:
        existing = create_short_link(ctx["workspace"], target, link_settings=ctx["settings"], **made).short_url
    except shorteners.ShortenerError as exc:
        logger.warning("Link service failed for platform post %s; using a built-in link: %s", ctx["pp"].pk, exc)
        builtin = LinkSettings(
            workspace=ctx["workspace"],
            provider=LinkSettings.Provider.BUILTIN,
            custom_short_domain=ctx["settings"].custom_short_domain,
        )
        try:
            existing = create_short_link(ctx["workspace"], target, link_settings=builtin, **made).short_url
            ctx["problems"].append(f"{exc} A built-in short link was used instead.")
        except LinkError as inner:
            ctx["problems"].append(f"{exc} The link was left as typed.")
            logger.warning("Link not shortened for platform post %s: %s", ctx["pp"].pk, inner)
            existing = target
    except LinkError as exc:
        logger.warning("Link not shortened for platform post %s: %s", ctx["pp"].pk, exc)
        ctx["problems"].append(f"{exc.message} The link was left as typed.")
        existing = target
    ctx["existing"][target] = existing
    return existing


def _rewrite(text, ctx) -> str:
    urls = extract_urls(text)
    if not urls:
        return text
    mapping: dict[str, str] = {}
    for url in urls:
        if is_short_url(ctx["workspace"].id, url, ctx["settings"]):
            continue
        target = apply_utm(url, ctx["utm"]) if ctx["utm"] else url
        if ctx["shorten"]:
            target = _short_for(target, ctx)
        if target != url:
            mapping[url] = target
    return _replace_urls(text, mapping)


def _approval_locked(pp) -> bool:
    """True when ``pp`` carries final-approval proof for exactly its current content.

    Such a copy is published as approved: its links are never rewritten afterwards, even if the workspace's link
    settings changed since, because the publisher would refuse content that no longer matches the approval.
    """
    if not pp.approval_fingerprint:
        return False
    from apps.approvals.policy import publish_fingerprint

    return pp.approval_fingerprint == publish_fingerprint(pp)


def prepare_platform_post(pp) -> bool:
    """Tag and shorten the links in one account's copy of a post. Returns True when any text changed.

    The result is saved as the copy's caption (and first comment) override, so what reviewers see, what the
    approval fingerprint covers and what is published are the same text. Safe to call again: an address that
    already carries its UTM parameters is left alone, one that is already short is skipped and a link made
    earlier for this copy is reused, so re-saving never makes a second link. An override we wrote from the
    post's own caption is re-derived from that caption, so editing the post later isn't hidden behind it.
    """
    from apps.composer.models import PlatformPost, PlatformPostPart

    from .models import PreparedCopy

    if pp.status not in PREPARE_STATUSES:
        return False
    # Under review the old proof is about to be cleared; once scheduled, approved content is final.
    if pp.status == "scheduled" and _approval_locked(pp):
        return False
    post = pp.post
    record = PreparedCopy.objects.filter(platform_post=pp).first()
    link_settings = find_settings(post.workspace_id)
    shorten = effective_shorten(post, link_settings)
    active = link_settings is not None and (shorten or link_settings.utm_enabled)
    if record is None:
        texts = [pp.effective_caption or "", pp.effective_first_comment or ""]
        if not active or (not any("http" in t for t in texts) and not post.thread_parts.exists()):
            return False
    ctx = {
        "pp": pp,
        "workspace": post.workspace,
        "settings": link_settings,
        "shorten": shorten,
        "utm": utm_params(link_settings, post, pp.social_account) if active and link_settings.utm_enabled else {},
        "existing": {
            link.destination_url: link.short_url for link in ShortLink.objects.filter(platform_post=pp, is_active=True)
        },
        "problems": [],
    }
    updates: dict = {}
    derived: dict = {}
    for field, base in (("caption", post.caption), ("first_comment", post.first_comment)):
        override_name = f"platform_specific_{field}"
        override = getattr(pp, override_name)
        ours = record is not None and getattr(record, field) is not None and override == getattr(record, field)
        # Our own earlier rewrite stands for the post's text: start again from that text as it is now.
        source = base if (ours or override is None) else override
        rewritten = _rewrite(source or "", ctx) if active else (source or "")
        if ours or override is None:
            value = rewritten if rewritten != (base or "") else None
            derived[field] = value
        else:
            value = rewritten
            derived[field] = None
        if value != override:
            updates[override_name] = value
    changed = bool(updates)
    if updates:
        PlatformPost.objects.filter(pk=pp.pk).update(**updates)
        for name, value in updates.items():
            setattr(pp, name, value)
    if active:
        for part in post.thread_parts.all():
            state = PlatformPostPart.objects.filter(platform_post=pp, part=part).first()
            if state is not None and state.status != PlatformPostPart.Status.PENDING:
                continue
            current = state.effective_caption if state is not None else part.caption
            rewritten = _rewrite(current or "", ctx)
            if rewritten != (current or ""):
                if state is None:
                    PlatformPostPart.objects.create(platform_post=pp, part=part, platform_specific_caption=rewritten)
                else:
                    PlatformPostPart.objects.filter(pk=state.pk).update(platform_specific_caption=rewritten)
                changed = True
    if ctx["existing"]:
        _drop_unused_links(pp)
    problem = " ".join(dict.fromkeys(ctx["problems"]))[:300]
    if any(derived.values()) or problem or record is not None:
        PreparedCopy.objects.update_or_create(
            platform_post=pp,
            defaults={
                "caption": derived.get("caption"),
                "first_comment": derived.get("first_comment"),
                "problem": problem,
            },
        )
    return changed


def _drop_unused_links(pp) -> None:
    """Delete links made for this copy that its text no longer uses and nobody ever opened.

    The copy hasn't gone out, so such a link was never public: it came from text that was edited since (an
    autosave caught a half-typed address, a link was removed). Keeping it would only use up the plan allowance.
    """
    from apps.composer.models import PlatformPostPart

    texts = [pp.effective_caption or "", pp.effective_first_comment or ""]
    texts += [
        state.effective_caption or ""
        for state in PlatformPostPart.objects.filter(platform_post=pp).select_related("part")
    ]
    texts += [part.caption or "" for part in pp.post.thread_parts.all()]
    blob = "\n".join(texts)
    unused = [
        link.pk
        for link in ShortLink.objects.filter(platform_post=pp, stats__isnull=True).distinct()
        if link.short_url not in blob
    ]
    if unused:
        ShortLink.objects.filter(pk__in=unused).delete()


def prepare_post(post) -> None:
    """:func:`prepare_platform_post` for each copy of ``post`` under review or scheduled.

    Called when the post's own text changes (and once thread parts exist), so the copies follow the edit.
    """
    for pp in post.platform_posts.select_related("social_account", "post").filter(status__in=PREPARE_STATUSES):
        prepare_platform_post(pp)


def link_problems(workspace) -> list:
    """Copies still waiting to go out whose links couldn't all be shortened as chosen, newest first."""
    from .models import PreparedCopy

    return list(
        PreparedCopy.objects.filter(
            platform_post__post__workspace=workspace, platform_post__status__in=PREPARE_STATUSES
        )
        .exclude(problem="")
        .select_related("platform_post__post", "platform_post__social_account")
        .order_by("-updated_at")[:20]
    )


# ---------------------------------------------------------------------------
# Counting: no cookies, no addresses stored
# ---------------------------------------------------------------------------

# Link-preview fetchers: platforms open the link to build a card when a post goes out. They are not people.
_PREVIEW_MARKERS = (
    "facebookexternalhit",
    "facebot",
    "twitterbot",
    "linkedinbot",
    "slackbot",
    "slack-imgproxy",
    "whatsapp",
    "telegrambot",
    "discordbot",
    "pinterestbot",
    "pinterest/",
    "skypeuripreview",
    "embedly",
    "redditbot",
    "mastodon",
    "bluesky cardyb",
    "cardyb",
    "threads",
    "meta-externalagent",
    "meta-externalfetcher",
    "iframely",
    "vkshare",
    "tumblr",
    "bitlybot",
    "quora link preview",
    "snapchat",
)
_BOT_MARKERS = (
    "bot",
    "crawl",
    "spider",
    "slurp",
    "headless",
    "curl/",
    "wget/",
    "python-requests",
    "python-urllib",
    "httpclient",
    "go-http-client",
    "java/",
    "libwww",
    "scrapy",
    "monitor",
    "uptime",
    "pingdom",
    "lighthouse",
    "yandex",
    "baidu",
    "ahrefs",
    "semrush",
    "mj12",
    "phantomjs",
)

CLICK, PREVIEW, IGNORED = "click", "preview", "ignored"


def classify_agent(request) -> str:
    """``preview`` for a platform's card fetcher, ``ignored`` for crawlers and scripts, else ``click``."""
    agent = (request.META.get("HTTP_USER_AGENT") or "").lower()
    if not agent:
        return IGNORED
    if any(marker in agent for marker in _PREVIEW_MARKERS):
        return PREVIEW
    if any(marker in agent for marker in _BOT_MARKERS):
        return IGNORED
    return CLICK


def _today() -> dt.date:
    return timezone.now().astimezone(dt.UTC).date()


_SALT_SECONDS = 60 * 60 * 48


def _daily_salt(day: dt.date) -> bytes:
    """A random secret for ``day`` that exists only in the cache, so a token can't be recomputed from an address later."""
    key = f"links:salt:{day.isoformat()}"
    cache.add(key, secrets.token_hex(32), _SALT_SECONDS)
    salt = cache.get(key)
    if salt is None:
        salt = secrets.token_hex(32)
    return salt.encode()


def _visitor_token(request, day: dt.date) -> str:
    from apps.api.limits import _client_ip

    material = f"{_client_ip(request) or ''}|{request.META.get('HTTP_USER_AGENT', '')}"
    return hmac.new(_daily_salt(day), material.encode(), hashlib.sha256).hexdigest()[:32]


def _within(key: str, limit: int, window: int = 60) -> bool:
    if cache.add(key, 1, window):
        return limit >= 1
    try:
        return cache.incr(key) <= limit
    except ValueError:
        cache.set(key, 1, window)
        return True


def record_hit(link: ShortLink, request) -> str:
    """Count a visit to ``link``. Returns ``click``, ``preview``, ``ignored`` or ``limited`` (stop; answer 429).

    Only ``limited`` should change the response; every other outcome still redirects.
    """
    kind = classify_agent(request)
    day = _today()
    token = _visitor_token(request, day)
    if not _within(f"links:rl:hard:{token}", HARD_LIMIT_PER_VISITOR_PER_MINUTE):
        return "limited"
    if kind == IGNORED or request.method != "GET":
        return IGNORED
    if not _within(f"links:rl:visitor:{link.pk}:{token}", CLICKS_PER_VISITOR_PER_MINUTE):
        return IGNORED
    if not _within(f"links:rl:link:{link.pk}", EVENTS_PER_LINK_PER_MINUTE):
        return IGNORED
    first = bool(cache.add(f"links:seen:{link.pk}:{day}:{kind}:{token}", 1, 60 * 60 * 30))
    row, _created = LinkDailyStat.objects.get_or_create(link=link, day=day, kind=kind)
    LinkDailyStat.objects.filter(pk=row.pk).update(count=F("count") + 1, uniques=F("uniques") + int(first))
    return kind


# ---------------------------------------------------------------------------
# Reading the counters
# ---------------------------------------------------------------------------


def _range(days: int) -> tuple[dt.date, dt.date]:
    days = max(1, min(int(days), STATS_DAYS_MAX))
    end = _today()
    return end - dt.timedelta(days=days - 1), end


def clicks_summary(
    workspace,
    *,
    start: dt.date,
    end: dt.date,
    account_ids=None,
    post_ids=None,
    link_ids=None,
) -> dict:
    """Clicks, unique visitors (an estimate) and previews between two UTC days, with a split per link, post and account."""
    rows = LinkDailyStat.objects.filter(link__workspace=workspace, day__gte=start, day__lte=end)
    if account_ids is not None:
        rows = rows.filter(link__social_account_id__in=list(account_ids))
    if post_ids is not None:
        rows = rows.filter(link__post_id__in=list(post_ids))
    if link_ids is not None:
        rows = rows.filter(link_id__in=list(link_ids))
    per_link: dict = {}
    series: dict[dt.date, int] = {}
    totals = {"clicks": 0, "unique_visitors": 0, "previews": 0}
    for row in rows.values("link_id", "day", "kind").annotate(count=Sum("count"), uniques=Sum("uniques")):
        bucket = per_link.setdefault(row["link_id"], {"clicks": 0, "unique_clicks": 0, "previews": 0})
        if row["kind"] == LinkDailyStat.Kind.CLICK:
            bucket["clicks"] += row["count"]
            bucket["unique_clicks"] += row["uniques"]
            totals["clicks"] += row["count"]
            totals["unique_visitors"] += row["uniques"]
            series[row["day"]] = series.get(row["day"], 0) + row["count"]
        else:
            bucket["previews"] += row["count"]
            totals["previews"] += row["count"]
    links = {
        link.pk: link
        for link in ShortLink.objects.filter(pk__in=per_link.keys()).select_related("social_account", "post")
    }
    by_link, by_post, by_account = [], {}, {}
    for link_id, bucket in per_link.items():
        link = links.get(link_id)
        if link is None:
            continue
        by_link.append(
            {
                "link_id": link.pk,
                "short_url": link.short_url,
                "destination_url": link.destination_url,
                "post_id": link.post_id,
                "social_account_id": link.social_account_id,
                "is_active": link.is_active,
                **bucket,
            }
        )
        if link.post_id:
            agg = by_post.setdefault(link.post_id, {"post_id": link.post_id, "clicks": 0, "unique_clicks": 0})
            agg["clicks"] += bucket["clicks"]
            agg["unique_clicks"] += bucket["unique_clicks"]
        if link.social_account_id:
            agg = by_account.setdefault(
                link.social_account_id,
                {
                    "social_account_id": link.social_account_id,
                    "platform": link.social_account.platform,
                    "name": link.social_account.account_name or link.social_account.account_handle,
                    "clicks": 0,
                    "unique_clicks": 0,
                },
            )
            agg["clicks"] += bucket["clicks"]
            agg["unique_clicks"] += bucket["unique_clicks"]
    by_link.sort(key=lambda item: (-item["clicks"], item["short_url"]))
    return {
        "start": start,
        "end": end,
        "totals": totals,
        "series": [{"day": day, "clicks": series.get(day, 0)} for day in _days(start, end)],
        "links": by_link,
        "posts": sorted(by_post.values(), key=lambda item: -item["clicks"]),
        "accounts": sorted(by_account.values(), key=lambda item: -item["clicks"]),
    }


def _days(start: dt.date, end: dt.date):
    day = start
    while day <= end:
        yield day
        day += dt.timedelta(days=1)


def stats_summary(workspace, days: int = 30, **filters) -> dict:
    start, end = _range(days)
    return {"days": (end - start).days + 1, **clicks_summary(workspace, start=start, end=end, **filters)}


def link_listing(workspace, *, post_id=None, account_id=None):
    qs = ShortLink.objects.filter(workspace=workspace).select_related("social_account")
    if post_id:
        qs = qs.filter(post_id=post_id)
    if account_id:
        qs = qs.filter(social_account_id=account_id)
    return qs


def overview_section(workspace, rng) -> dict | None:
    """Link clicks for the analytics overview and reports over ``rng`` (days are UTC), or None when no link was ever made."""
    if not ShortLink.objects.filter(workspace=workspace).exists():
        return None
    data = clicks_summary(workspace, start=rng.start, end=rng.end)
    data["top_links"] = data["links"][:5]
    return data


def platform_post_clicks(platform_post) -> dict | None:
    """All-time clicks on the short links made for one account's copy of a post, or None when it has none."""
    links = list(ShortLink.objects.filter(platform_post=platform_post))
    if not links:
        return None
    totals = {"clicks": 0, "unique_clicks": 0, "previews": 0}
    per_link = {
        link.pk: {"link_id": link.pk, "short_url": link.short_url, "destination_url": link.destination_url, "clicks": 0}
        for link in links
    }
    for row in (
        LinkDailyStat.objects.filter(link__in=links)
        .values("link_id", "kind")
        .annotate(count=Sum("count"), uniques=Sum("uniques"))
    ):
        if row["kind"] == LinkDailyStat.Kind.CLICK:
            totals["clicks"] += row["count"]
            totals["unique_clicks"] += row["uniques"]
            per_link[row["link_id"]]["clicks"] += row["count"]
        else:
            totals["previews"] += row["count"]
    return {**totals, "links": sorted(per_link.values(), key=lambda item: -item["clicks"])}
