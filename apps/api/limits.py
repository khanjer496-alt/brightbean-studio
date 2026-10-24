"""Rate-limit primitives for the Agent API.

Three orthogonal concerns live here:

1. ``PLATFORM_DAILY_POST_LIMIT`` — channel-aligned caps on how many
   scheduled-or-published PlatformPost rows an API key may create per
   ``SocialAccount`` per rolling 24h window. Numbers come from each
   platform's own developer docs (May 2026); see ``docs/agent-api.md``
   for the source links. The publisher's own ``RateLimitState`` tracks
   the *outgoing* upstream platform quota separately — these layers
   compose; neither replaces the other.

2. Per-key / per-workspace / per-IP HTTP throttles via ``django-ratelimit``,
   exposed as small wrapper helpers so each router stays declarative.

3. Per-key hourly and daily call caps, counted from ``ApiKeyUsageHourly``,
   and (with ``BILLING_ENABLED``) the plan's per-organization daily cap.
   The per-minute tiers in (2) stop bursts but not a client polling just
   under them all day long, which is what one key did at ~113 calls a
   minute (165k a day). These caps are the backstop for that.

The 429 body shape is uniform across all tiers so agents can self-throttle
on ``tier`` without parsing free text.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import logging

from django.conf import settings
from django.core.cache import cache as _cache
from django.http import HttpRequest
from django.urls import reverse
from django.utils import timezone
from django_ratelimit.core import is_ratelimited
from ninja.errors import HttpError

from apps.api.usage import actor_for, day_start, hour_start, org_usage_in_day, usage_in_windows
from apps.api_keys import budgets
from apps.api_keys import capabilities as caps
from apps.api_keys.models import ApiKey
from apps.billing.entitlements import get_entitlements
from apps.composer.models import PlatformPost
from apps.social_accounts.models import SocialAccount

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tier 2 — channel-aligned per-(SocialAccount, 24h) creation caps
# ---------------------------------------------------------------------------

#: Default platform cap when ``daily_post_limit_override`` on the
#: ``SocialAccount`` row isn't set. Numbers chosen at or just below the
#: platform's own published cap; see the plan file for justification and
#: source links. Lower bound (``_DEFAULT_FALLBACK``) covers unknown future
#: platforms safely.
#:
#: Keys MUST match the actual ``SocialAccount.platform`` values, which
#: come from ``apps.credentials.models.PlatformCredential.Platform`` —
#: not invented variants. Codex review flagged the previous
#: ``"facebook_page"`` key as a no-op (the real platform code is
#: ``"facebook"``), which silently dropped every Facebook account into
#: ``_DEFAULT_FALLBACK=50/day`` instead of the intended 200/day cap.
PLATFORM_DAILY_POST_LIMIT: dict[str, int] = {
    "linkedin_personal": 100,
    "linkedin_company": 100,
    "facebook": 200,
    "instagram": 25,
    "instagram_login": 25,
    "tiktok": 15,
    "youtube": 50,
    "pinterest": 100,
    "threads": 250,
    "mastodon": 200,
    "bluesky": 200,
    "google_business": 50,
    # A bot may send ~20 messages a minute to one group; the cap is about volume
    # a channel's subscribers will tolerate rather than the platform's limit.
    "telegram": 200,
    # Messages to a community channel; Discord's own limits are per route.
    "discord": 200,
    # Roughly one message a second per channel is Slack's own ceiling.
    "slack": 200,
    # Reddit throttles new accounts and each subreddit hard; keep well under it.
    "reddit": 25,
    # POST /2/tweets allows 100 per 15 minutes, but each post is billed to the customer's own app.
    "x": 100,
    # Tumblr allows 250 new posts a day per user.
    "tumblr": 250,
    # Lemmy instances default to 6 posts per 10 minutes per user; the cap is about community etiquette.
    "lemmy": 50,
    # Relays set their own limits; free public relays tolerate a modest rate.
    "nostr": 100,
    # Casts are billed to the customer's own Neynar plan.
    "farcaster": 100,
    # Chat messages: the cap is about what a channel's viewers will tolerate.
    "twitch": 100,
    "kick": 100,
    # Shots are curated work; Dribbble throttles uploads per account.
    "dribbble": 10,
    # Long-form platforms: a few articles or newsletters a day is already a lot.
    "medium": 25,
    "wordpress": 50,
    "hashnode": 25,
    # A campaign emails a whole list, so the cap protects its subscribers rather than the API.
    "listmonk": 10,
}

_DEFAULT_FALLBACK = 50


def resolve_platform_limit(social_account: SocialAccount) -> int:
    """Effective per-24h cap for one ``SocialAccount``.

    Order: per-account override → platform default → fallback.

    Note the explicit ``is not None`` check: ``daily_post_limit_override``
    is a ``PositiveIntegerField(null=True)``, so an admin can legitimately
    set it to ``0`` to lock the account out of any creation. A naked
    ``if override:`` would treat 0 as "no override" and fall through to
    the platform default (e.g. LinkedIn 100/day), silently defeating the
    lockout.
    """
    override = getattr(social_account, "daily_post_limit_override", None)
    if override is not None:
        return int(override)
    return PLATFORM_DAILY_POST_LIMIT.get(social_account.platform, _DEFAULT_FALLBACK)


#: Statuses that *consume* the upstream platform's posting budget.
#:
#: A row only pressures the platform from the moment it leaves "draft" —
#: scheduled rows are queued for the publisher, publishing rows are
#: mid-flight on a platform API call, published rows already spent a
#: slot, and failed rows almost certainly did too (the platform 4xx'd
#: AFTER we made the call). Drafts are local DB state only; they have
#: not touched the platform yet, so counting them would block scheduling
#: for an agent who built a 25-post Instagram draft queue.
QUOTA_CONSUMING_STATUSES = frozenset({"scheduled", "publishing", "published", "failed"})


def count_recent_creations(social_account: SocialAccount, *, window_hours: int = 24) -> int:
    """Count PlatformPost rows in the platform-budget-consuming states.

    Filtered to ``QUOTA_CONSUMING_STATUSES`` and to rows whose
    ``updated_at`` falls inside the window. Codex review flagged that
    the previous ``created_at`` filter let an agent bypass the cap by
    creating drafts (which don't count) and then scheduling them more
    than 24 h later — the newly quota-consuming rows had a stale
    ``created_at`` outside the window and slipped through.

    ``updated_at`` is bumped by ``transition_platform_post`` whenever
    the status changes (including the moment a draft enters scheduled),
    so it is a faithful approximation of "when did this row consume a
    platform slot". Edits that touch other fields also bump it; the
    over-counting is conservative — agents hit the cap slightly earlier
    than the platform's own count, which is the safe direction.
    """
    cutoff = timezone.now() - dt.timedelta(hours=window_hours)
    return PlatformPost.objects.filter(
        social_account=social_account,
        updated_at__gte=cutoff,
        status__in=QUOTA_CONSUMING_STATUSES,
    ).count()


def check_platform_quota(social_account: SocialAccount) -> None:
    """Raise an ``HttpError(429, ...)`` if the per-account cap is reached.

    Call this immediately before creating a ``PlatformPost`` row in any
    write endpoint. The 24h-rolling check is a single indexed count, so
    it's cheap enough to run on every write.
    """
    limit = resolve_platform_limit(social_account)
    used = count_recent_creations(social_account)
    if used >= limit:
        # Compute when the oldest quota-consuming row ages out, so the
        # client gets an honest Retry-After rather than guessing. Match
        # the same filter as count_recent_creations to keep the two
        # numbers internally consistent.
        # Match the same filter as ``count_recent_creations`` so the
        # two numbers stay internally consistent; computing the oldest
        # quota-consuming ``updated_at`` lets ``retry_after`` reflect
        # when the bucket will next free up.
        oldest = (
            PlatformPost.objects.filter(
                social_account=social_account,
                updated_at__gte=timezone.now() - dt.timedelta(hours=24),
                status__in=QUOTA_CONSUMING_STATUSES,
            )
            .order_by("updated_at")
            .values_list("updated_at", flat=True)
            .first()
        )
        retry_after_seconds = int((oldest + dt.timedelta(hours=24) - timezone.now()).total_seconds()) if oldest else 60
        # 1s floor so the client doesn't hammer us at the boundary.
        retry_after_seconds = max(retry_after_seconds, 1)
        raise HttpError(
            429,
            _format_quota_message(
                tier=f"platform_quota:{social_account.platform}",
                limit=limit,
                remaining=0,
                retry_after=retry_after_seconds,
            ),
        )


def _format_quota_message(*, tier: str, limit: int, remaining: int, retry_after: int, upgrade_url: str = "") -> str:
    """Plain-text body for the HttpError so Ninja's default 429 page renders.

    The router-level error handler in ``api.py`` rewraps this into the
    uniform JSON shape with a ``Retry-After`` header.
    """
    msg = f"rate_limited tier={tier} limit={limit} remaining={remaining} retry_after={retry_after}"
    return f"{msg} upgrade_url={upgrade_url}" if upgrade_url else msg


# ---------------------------------------------------------------------------
# Tier 1 — HTTP DoS protection via django-ratelimit
# ---------------------------------------------------------------------------

#: Defaults — overridable per-key via ``ApiKey.rate_override_*`` columns
#: so an admin can loosen a single key without bumping the global default.
DEFAULT_WRITE_RATE = "120/m"
DEFAULT_READ_RATE = "300/m"
WORKSPACE_AGG_WRITE_RATE = "1000/m"
IP_FAILED_AUTH_RATE = "10/m"

#: Calls per key per UTC hour / UTC day, overridable via
#: ``ApiKey.rate_override_hourly`` / ``rate_override_daily``. In the 30 days
#: before these existed the busiest legitimate key made 1,444 calls in an hour
#: and 7,783 in a day; the hourly cap is the one that binds a steady poller,
#: and at it the daily budget takes 20 hours to spend.
DEFAULT_HOURLY_CAP = 1000
DEFAULT_DAILY_CAP = 20000

# ``django-ratelimit`` keys are computed by callable accessors; these
# helpers consolidate the convention so individual routes stay clean.


def _ratelimit_key_apikey_writes(_group: str, request: HttpRequest) -> str:
    api_key: ApiKey = request.auth  # type: ignore[attr-defined]  # set by ApiKeyAuth
    return f"apikey:{api_key.id}:w"


def _ratelimit_key_apikey_reads(_group: str, request: HttpRequest) -> str:
    api_key: ApiKey = request.auth  # type: ignore[attr-defined]
    return f"apikey:{api_key.id}:r"


def _ratelimit_key_workspace_writes(_group: str, request: HttpRequest) -> str:
    api_key: ApiKey = request.auth  # type: ignore[attr-defined]
    return f"ws:{api_key.workspace_id}:w"


def _override_or(api_key: ApiKey, attr: str, default: str) -> str:
    """Pick the per-key override rate or fall back to the default.

    ``is not None`` (rather than ``if override:``) so that an explicit
    ``0`` value is honoured as "0 requests per minute" — admins use
    that to freeze a misbehaving key without revoking it.
    """
    override = getattr(api_key, attr, None)
    if override is not None:
        return f"{int(override)}/m"
    return default


def enforce_http_rate_limits(request: HttpRequest, *, is_write: bool) -> None:
    """Stack the HTTP-level tiers and raise 429 if any trip.

    Centralized here so routers don't each re-import django-ratelimit
    helpers. Per-tier rates honour per-key overrides; the workspace
    aggregate is global.
    """
    api_key: ApiKey = request.auth  # type: ignore[attr-defined]
    tier_rate = (
        _override_or(api_key, "rate_override_writes", DEFAULT_WRITE_RATE)
        if is_write
        else _override_or(api_key, "rate_override_reads", DEFAULT_READ_RATE)
    )
    tier_key = _ratelimit_key_apikey_writes if is_write else _ratelimit_key_apikey_reads
    if is_ratelimited(
        request=request,
        group=f"agent_api:apikey:{'w' if is_write else 'r'}",
        key=tier_key,
        rate=tier_rate,
        increment=True,
    ):
        raise HttpError(
            429,
            _format_quota_message(
                tier="per_key_writes" if is_write else "per_key_reads",
                limit=_parse_rate_num(tier_rate),
                remaining=0,
                retry_after=60,
            ),
        )
    if is_write and is_ratelimited(
        request=request,
        group="agent_api:workspace:w",
        key=_ratelimit_key_workspace_writes,
        rate=WORKSPACE_AGG_WRITE_RATE,
        increment=True,
    ):
        raise HttpError(
            429,
            _format_quota_message(
                tier="per_workspace_writes",
                limit=_parse_rate_num(WORKSPACE_AGG_WRITE_RATE),
                remaining=0,
                retry_after=60,
            ),
        )
    enforce_usage_caps(request)
    enforce_plan_daily_cap(request)
    enforce_token_budgets(request)
    # Global instance cap — optional, env-driven.
    global_cap = getattr(settings, "BB_API_LIMIT", None)
    if global_cap and is_ratelimited(
        request=request,
        group="agent_api:global",
        key=lambda _g, _r: "global",
        rate=f"{int(global_cap)}/m",
        increment=True,
    ):
        raise HttpError(
            429,
            _format_quota_message(
                tier="global",
                limit=int(global_cap),
                remaining=0,
                retry_after=60,
            ),
        )


def _parse_rate_num(rate: str) -> int:
    return int(rate.split("/", 1)[0])


def _override_cap(api_key: ApiKey, attr: str, default: int) -> int:
    """Per-key cap override, with the same explicit-``0``-freezes rule as ``_override_or``."""
    override = getattr(api_key, attr, None)
    return int(override) if override is not None else default


def enforce_usage_caps(request: HttpRequest) -> None:
    """Raise 429 once the caller has used its hourly or daily call budget.

    Windows are fixed UTC hours and days, so ``retry_after`` is exact: the
    seconds until the window that tripped rolls over. Gated on
    ``RATELIMIT_ENABLE`` like the django-ratelimit tiers, so it switches off
    with them in development.
    """
    if not getattr(settings, "RATELIMIT_ENABLE", True):
        return
    api_key: ApiKey = request.auth  # type: ignore[attr-defined]
    hourly_cap = _override_cap(api_key, "rate_override_hourly", DEFAULT_HOURLY_CAP)
    daily_cap = _override_cap(api_key, "rate_override_daily", DEFAULT_DAILY_CAP)
    now = timezone.now()
    used_hour, used_day = usage_in_windows(actor_for(api_key), now)

    # Daily first: when both are spent, the honest Retry-After is midnight.
    if used_day >= daily_cap:
        next_day = day_start(now) + dt.timedelta(days=1)
        raise HttpError(
            429,
            _format_quota_message(
                tier="per_key_daily",
                limit=daily_cap,
                remaining=0,
                retry_after=max(int((next_day - now).total_seconds()), 1),
            ),
        )
    if used_hour >= hourly_cap:
        next_hour = hour_start(now) + dt.timedelta(hours=1)
        raise HttpError(
            429,
            _format_quota_message(
                tier="per_key_hourly",
                limit=hourly_cap,
                remaining=0,
                retry_after=max(int((next_hour - now).total_seconds()), 1),
            ),
        )


def _seconds_to_next_day(now: dt.datetime) -> int:
    return max(int(((day_start(now) + dt.timedelta(days=1)) - now).total_seconds()), 1)


def token_budget_error(tier: str, limit: int) -> HttpError:
    """The 429 for a spent per-token budget; ``retry_after`` is the time until the UTC day rolls over."""
    return HttpError(
        429,
        _format_quota_message(tier=tier, limit=limit, remaining=0, retry_after=_seconds_to_next_day(timezone.now())),
    )


def enforce_token_budgets(request: HttpRequest) -> None:
    """Raise 429 once a token has spent its own daily request or AI budget.

    The owner sets these on the token, so they are not switched off with ``RATELIMIT_ENABLE``. They only
    narrow the caps above: the token is held to the lower of the two. MCP tools that call the AI assistant
    spend their AI budget in ``apps.mcp.transport``, where the tool is known.
    """
    api_key = request.auth  # type: ignore[attr-defined]
    if api_key is None:
        return
    request_budget = getattr(api_key, "daily_request_budget", None)
    if request_budget is not None:
        if isinstance(api_key, ApiKey):
            _, used_day = usage_in_windows(actor_for(api_key), timezone.now())
            spent = used_day >= request_budget
        else:
            spent = not budgets.reserve_request(api_key)
        if spent:
            raise token_budget_error("token_daily_requests", request_budget)
    if caps.route_uses_ai(request.method or "", request.path) and not budgets.reserve_ai_call(api_key):
        raise token_budget_error("token_daily_ai", api_key.daily_ai_budget or 0)


def enforce_plan_daily_cap(request: HttpRequest) -> None:
    """Raise 429 once the organization has used its plan's daily calls.

    Every plan includes the Agent API and MCP; plans differ in how many calls
    a day they allow, counted across all of the organization's keys and OAuth
    callers. A no-op (and no query) unless ``BILLING_ENABLED``. Not gated on
    ``RATELIMIT_ENABLE``: this is a plan limit, not abuse protection.
    """
    api_key: ApiKey = request.auth  # type: ignore[attr-defined]
    organization = api_key.workspace.organization
    cap = get_entitlements(organization).max_api_calls_per_day
    if cap is None:
        return
    now = timezone.now()
    if org_usage_in_day(organization.pk, now) < cap:
        return
    next_day = day_start(now) + dt.timedelta(days=1)
    raise HttpError(
        429,
        _format_quota_message(
            tier="plan_daily",
            limit=cap,
            remaining=0,
            retry_after=max(int((next_day - now).total_seconds()), 1),
            upgrade_url=f"{settings.APP_URL.rstrip('/')}{reverse('billing:overview')}",
        ),
    )


# ---------------------------------------------------------------------------
# Failed-auth IP throttle — special-case, no api_key on request
# ---------------------------------------------------------------------------
#
# We deliberately *don't* use django-ratelimit here. Its semantics are
# "ratelimited iff usage > limit" — which means a rate of "10/m" lets
# 10 requests in AND THEN serves an 11th before short-circuiting on
# the 12th. For a brute-force defense that's an off-by-one we don't
# want: the budget is "10 failed attempts," period.
#
# Instead we drive Django's cache directly: ``cache.add`` seeds the
# bucket with a TTL on the first failure, ``cache.incr`` bumps it on
# subsequent failures (preserving the TTL), and the threshold check
# is the tighter ``count >= limit``. Same response shape on the
# blocked path (401, not 429) so an attacker can't detect the throttle.

_AUTH_FAIL_LIMIT = 10
_AUTH_FAIL_WINDOW_SECONDS = 60


def _auth_fail_cache_key(request: HttpRequest) -> str:
    return f"agent_api:auth_fail:{_client_ip(request) or 'anon'}"


def is_failed_auth_ip_blocked(request: HttpRequest) -> bool:
    """True iff the IP has accumulated ``>= _AUTH_FAIL_LIMIT`` failures
    in the current rolling window. Read-only — no increment.
    """
    return _cache.get(_auth_fail_cache_key(request), 0) >= _AUTH_FAIL_LIMIT


def record_failed_auth(request: HttpRequest) -> None:
    """Increment the failed-auth counter for this IP.

    On the first failure, ``cache.add`` seeds the bucket with the
    window TTL. On subsequent failures, ``cache.incr`` bumps the
    counter without touching the TTL — so the window stays anchored
    at the *first* failure, not the most recent one.
    """
    key = _auth_fail_cache_key(request)
    if not _cache.add(key, 1, _AUTH_FAIL_WINDOW_SECONDS):
        try:
            _cache.incr(key)
        except ValueError:
            # Race: the key's TTL expired between our ``add`` (which
            # returned False because the key existed) and the incr
            # (which now finds nothing). Re-seed.
            _cache.set(key, 1, _AUTH_FAIL_WINDOW_SECONDS)


def _client_ip(request: HttpRequest) -> str | None:
    """Return the originating client IP, honouring proxies safely.

    Codex review flagged: the previous version unconditionally trusted
    the leftmost ``X-Forwarded-For`` value, which a remote client can
    set to any string. That defeats the failed-auth IP throttle (rotate
    XFF per request to escape the per-IP bucket) and lets the attacker
    pin audit-log rows to a victim's IP.

    Hardening: only honour ``X-Forwarded-For`` when the direct
    ``REMOTE_ADDR`` is in ``settings.BB_TRUSTED_PROXIES``. On platforms
    that terminate TLS at a proxy you actually run (Cloudflare, ALB,
    nginx, …), set that list in env config. Otherwise fall back to the
    socket peer — which is the only IP we can vouch for ourselves.

    Also used by ``apps.accounts.middleware.AuthRateLimitMiddleware``.

    Entries may be single addresses or CIDR ranges. Behind Cloudflare and
    the bundled Caddy, the app's peer is Caddy on a Docker network whose
    address changes between restarts, and the hop before it is one of
    Cloudflare's edge addresses — neither can be listed as fixed IPs, so
    list the Docker network range and Cloudflare's published ranges.
    """
    trusted = _trusted_proxy_networks()
    remote = request.META.get("REMOTE_ADDR")
    if trusted and _is_trusted_proxy(remote, trusted):
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
        if forwarded:
            # Each proxy appends the peer it received the request from, so
            # the header reads client, proxy1, proxy2 — but a client can
            # send its own header first, and proxies append to it rather
            # than replace it. Only the values our trusted proxies appended
            # are reliable: walk from the right and take the first hop that
            # is not one of them. The leftmost hop is whatever the client
            # chose to write.
            hops = [h.strip() for h in forwarded.split(",") if h.strip()]
            for hop in reversed(hops):
                if not _is_trusted_proxy(hop, trusted):
                    return hop
            # Every hop was a trusted proxy — fall back to remote.
    return remote


def _trusted_proxy_networks() -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parse ``BB_TRUSTED_PROXIES`` into networks, skipping malformed entries.

    Parsed per call rather than cached so ``override_settings`` in tests
    takes effect; the list is short and this runs once per request.
    """
    networks = []
    for entry in getattr(settings, "BB_TRUSTED_PROXIES", ()) or ():
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            logger.warning("Ignoring malformed BB_TRUSTED_PROXIES entry %r", entry)
    return tuple(networks)


def _is_trusted_proxy(value: str | None, networks) -> bool:
    if not value:
        return False
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return any(address in network for network in networks)
