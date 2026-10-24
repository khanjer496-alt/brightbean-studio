"""Social account connection views.

Handles OAuth flows, account listing, connect/reconnect/disconnect actions.
"""

import logging
import secrets
from datetime import timedelta
from urllib.parse import urlsplit

from csp.decorators import csp_update
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST
from django_ratelimit.decorators import ratelimit

from apps.autonomy import service as autonomy_service
from apps.autonomy.actors import human as human_actor
from apps.billing.entitlements import PlanLimitError, billing_enabled, check_can_add_channel
from apps.calendar import services as calendar_services
from apps.common.validators import is_safe_url as _is_safe_url
from apps.credentials.models import PlatformCredential, configured_platform_credential, derive_is_configured
from apps.members.decorators import require_permission
from apps.webhooks import events as webhook_events
from providers.exceptions import APIError, OAuthError, PublishError, QuotaExceededError

from .error_messages import quota_connect_error
from .forms import ChannelOptionsForm
from .models import MastodonAppRegistration, PlatformVisibility, SocialAccount
from .oauth_aliases import from_url_slug, redirect_uri_from_request, to_url_slug
from .oauth_pkce import issue_pkce_verifier, pkce_kwargs
from .provider_factory import _get_provider_for_platform, apply_analytics_scope_flag
from .webhooks import (
    subscribe_account_webhooks,
    subscribe_account_webhooks_task,
    unsubscribe_account_webhooks,
)

logger = logging.getLogger(__name__)

OAUTH_STATE_MAX_AGE = 600  # 10 minutes
OAUTH_SESSION_KEY = "social_oauth"


def _get_visible_platform_choices():
    """Return PlatformCredential.Platform.choices filtered to visible platforms.

    Platforms without a PlatformVisibility row default to visible.
    """
    return PlatformVisibility.visible_choices()


def _get_configured_platforms(org_id):
    """Return set of platform names that have credentials configured."""
    from providers import PROVIDER_REGISTRY
    from providers.types import AuthType

    configured = set(
        PlatformCredential.objects.for_org(org_id)
        .filter(is_configured=True, workspace__isnull=True)
        .values_list("platform", flat=True)
    )
    env_creds = getattr(settings, "PLATFORM_CREDENTIALS_FROM_ENV", {})
    for platform, creds in env_creds.items():
        # Same completeness rule the credential resolver applies, so the grid can
        # never offer a Connect button that resolution will reject. A truthiness
        # check here would let a half-filled pair (app id set, secret missing)
        # render as connectable, walk the user through the platform's consent
        # screen, and only fail at token exchange with a generic error.
        if derive_is_configured(platform, creds):
            configured.add(platform)

    # Session-auth platforms (e.g. Bluesky) don't need app-level credentials —
    # the user supplies their own handle + app password at connect time.
    # Instance-OAuth platforms (e.g. Mastodon) don't need them either —
    # we register a per-instance OAuth app on first connect and persist it in
    # MastodonAppRegistration.
    for platform, provider_cls in PROVIDER_REGISTRY.items():
        # ``user_supplied_credentials``: the customer brings their own developer
        # app (X) and enters its credentials on the platform's connect form.
        if provider_cls().auth_type in (AuthType.SESSION, AuthType.INSTANCE_OAUTH) or (
            provider_cls.user_supplied_credentials
        ):
            configured.add(platform)

    return configured


def _after_connect_redirect(request, workspace_id):
    """Where to go once a connect flow ends: back into the welcome flow, else the calendar."""
    from apps.onboarding.flow import resume_url_after_connect

    resume_url = resume_url_after_connect(request.user, workspace_id)
    return redirect(resume_url) if resume_url else redirect("calendar:calendar", workspace_id=workspace_id)


def _connect_url_name(platform):
    """The named connect form a provider uses instead of the OAuth redirect, or ""."""
    from providers import PROVIDER_REGISTRY

    provider_cls = PROVIDER_REGISTRY.get(platform)
    return f"social_accounts:{provider_cls.connect_url_name}" if provider_cls and provider_cls.connect_url_name else ""


def _has_destination_picker(platform):
    """Whether one grant on ``platform`` serves several destinations the user picks from."""
    from providers import PROVIDER_REGISTRY

    provider_cls = PROVIDER_REGISTRY.get(platform)
    return bool(provider_cls and provider_cls.has_destination_picker)


def _build_redirect_uri(request, platform):
    """Build the OAuth callback URL.

    Platforms with an entry in ``PLATFORM_TO_URL_ALIAS`` (currently only
    TikTok → ``social1``) use the opaque slug in the URL path so the
    redirect URI doesn't contain the platform brand name. The signed
    OAuth state still carries the real platform identifier.
    """
    from django.urls import reverse

    url_slug = to_url_slug(platform)
    return request.build_absolute_uri(reverse("social_accounts:oauth_callback", kwargs={"platform": url_slug}))


def _sign_state(workspace_id, platform, user_id, nonce):
    """Create a signed OAuth state parameter."""
    return signing.dumps(
        {
            "workspace_id": str(workspace_id),
            "platform": platform,
            "user_id": str(user_id),
            "nonce": nonce,
        },
        salt="social-oauth-state",
    )


def _unsign_state(state_str):
    """Verify and decode the OAuth state parameter."""
    return signing.loads(
        state_str,
        salt="social-oauth-state",
        max_age=OAUTH_STATE_MAX_AGE,
    )


def _normalize_mastodon_instance_url(raw):
    """Normalize user-supplied Mastodon instance input to `scheme://host[:port]`.

    Accepts: bare hosts (`mastodon.social`), URLs with paths (`https://mastodon.social/@user`),
    fediverse handles (`@user@mastodon.social`, `user@mastodon.social`), and values with
    extra whitespace or trailing slashes. Defaults the scheme to https when missing.
    Returns an empty string when the input has no host.
    """
    value = (raw or "").strip()
    if not value:
        return ""

    # Fediverse handle form: `@user@host` or `user@host`. If there's exactly one '@'
    # and no scheme, treat it as a handle and extract the host. Two '@'s means a
    # leading '@' plus user@host.
    if "://" not in value and "@" in value:
        parts = value.lstrip("@").split("@")
        if len(parts) == 2 and parts[1]:
            value = parts[1]

    if "://" not in value:
        value = f"https://{value}"

    parts = urlsplit(value)
    if not parts.netloc:
        return ""
    scheme = parts.scheme or "https"
    return f"{scheme}://{parts.netloc}"


def _resolve_mastodon_extra_creds(session_data):
    """Resolve Mastodon instance-specific credentials from the OAuth session.

    Returns a dict suitable for `_get_provider_for_platform(**extra_creds)`
    containing `instance_url`, and `client_id`/`client_secret` when a matching
    `MastodonAppRegistration` exists. Empty dict when no instance_url is set.
    """
    extra_creds: dict = {}
    instance_url = (session_data or {}).get("instance_url", "")
    if not instance_url:
        return extra_creds

    extra_creds["instance_url"] = instance_url
    try:
        reg = MastodonAppRegistration.objects.get(instance_url=instance_url)
        extra_creds["client_id"] = reg.client_id
        extra_creds["client_secret"] = reg.client_secret
    except MastodonAppRegistration.DoesNotExist:
        pass
    return extra_creds


# ------------------------------------------------------------------
# Account List
# ------------------------------------------------------------------


@login_required
@require_permission("manage_social_accounts")
def account_list(request, workspace_id):
    """List connected social accounts for a workspace."""
    accounts = (
        SocialAccount.objects.for_workspace(workspace_id)
        .prefetch_related("posting_slots")
        .order_by("platform", "account_name")
    )
    configured_platforms = _get_configured_platforms(request.workspace.organization_id)

    return render(
        request,
        "social_accounts/list.html",
        {
            "accounts": accounts,
            "workspace_id": workspace_id,
            "configured_platforms": configured_platforms,
            "platform_choices": PlatformCredential.Platform.choices,
            "settings_active": "social_accounts",
        },
    )


# ------------------------------------------------------------------
# Connect Platform (OAuth redirect)
# ------------------------------------------------------------------


@login_required
@require_permission("manage_social_accounts")
@ratelimit(key="user", rate="20/m", method="POST", block=True)
def connect_platform(request, workspace_id):
    """GET: show platform grid. POST: initiate OAuth flow."""
    configured_platforms = _get_configured_platforms(request.workspace.organization_id)
    visible_platform_choices = _get_visible_platform_choices()

    if request.method == "GET":
        return render(
            request,
            "social_accounts/connect.html",
            {
                "workspace_id": workspace_id,
                "platform_choices": visible_platform_choices,
                "configured_platforms": configured_platforms,
            },
        )

    # POST: initiate OAuth
    platform = request.POST.get("platform", "").strip()
    if platform not in dict(visible_platform_choices):
        messages.error(request, "This platform is not available.")
        return redirect("social_accounts:connect", workspace_id=workspace_id)

    if platform not in configured_platforms:
        messages.error(
            request,
            f"Platform credentials for {platform} are not configured. Please contact your administrator.",
        )
        return redirect("social_accounts:connect", workspace_id=workspace_id)

    # Refuse before the platform round-trip rather than after it: a user at
    # their plan's channel limit shouldn't authorize an app only to be told no.
    try:
        check_can_add_channel(request.workspace.organization)
    except PlanLimitError as exc:
        messages.error(request, exc.message)
        return redirect("social_accounts:connect", workspace_id=workspace_id)

    # Special auth flows
    if platform == PlatformCredential.Platform.BLUESKY:
        return redirect("social_accounts:connect_bluesky", workspace_id=workspace_id)
    if platform == PlatformCredential.Platform.MASTODON:
        return redirect("social_accounts:connect_mastodon", workspace_id=workspace_id)
    if platform == PlatformCredential.Platform.DEVTO:
        return redirect("social_accounts:connect_devto", workspace_id=workspace_id)
    connect_url_name = _connect_url_name(platform)
    if connect_url_name:
        return redirect(connect_url_name, workspace_id=workspace_id)

    return _start_oauth(request, workspace_id, platform)


def _start_oauth(request, workspace_id, platform):
    """Send the user to ``platform``'s authorization page (state, nonce and PKCE verifier set up)."""
    # The workspace's organization, as the callback resolves it: ``request.org`` is the
    # user's first membership, which for a guest in another organization's workspace is not it.
    provider = _get_provider_for_platform(platform, request.workspace.organization_id, workspace_id=workspace_id)
    apply_analytics_scope_flag(provider, platform)
    nonce = secrets.token_urlsafe(32)
    state = _sign_state(workspace_id, platform, request.user.id, nonce)

    # PKCE verifier (e.g. TikTok, X); round-trips via the session alongside the nonce.
    code_verifier = issue_pkce_verifier(provider)

    # Store nonce in session to prevent replay
    request.session[OAUTH_SESSION_KEY] = {
        "nonce": nonce,
        "workspace_id": str(workspace_id),
        "platform": platform,
        "code_verifier": code_verifier,
    }

    redirect_uri = _build_redirect_uri(request, platform)
    auth_url = provider.get_auth_url(redirect_uri, state, **pkce_kwargs(code_verifier))
    return redirect(auth_url)


# ------------------------------------------------------------------
# OAuth Callback
# ------------------------------------------------------------------


@login_required
@ratelimit(key="user", rate="20/m", block=True)
@require_GET
def oauth_callback(request, platform):
    """Handle OAuth callback from the platform.

    ``platform`` arrives as the URL slug, which may be an alias (e.g.
    ``social1`` for TikTok). Normalise it before any platform-keyed lookup
    or comparison against the signed state.
    """
    platform = from_url_slug(platform)
    error = request.GET.get("error")
    if error:
        error_desc = request.GET.get("error_description", error)
        messages.error(request, f"OAuth error: {error_desc}")
        session_data = request.session.pop(OAUTH_SESSION_KEY, {})
        workspace_id = session_data.get("workspace_id")
        if workspace_id:
            return redirect("calendar:calendar", workspace_id=workspace_id)
        return redirect("dashboard")

    code = request.GET.get("code")
    state_str = request.GET.get("state")

    if not code or not state_str:
        messages.error(request, "Missing authorization code or state parameter.")
        return redirect("dashboard")

    # Validate state
    try:
        state_data = _unsign_state(state_str)
    except signing.BadSignature:
        messages.error(request, "Invalid or expired OAuth state. Please try again.")
        return redirect("dashboard")

    # Validate nonce from session
    session_data = request.session.pop(OAUTH_SESSION_KEY, {})
    if not session_data or session_data.get("nonce") != state_data.get("nonce"):
        messages.error(request, "OAuth session mismatch. Please try again.")
        return redirect("dashboard")

    # Validate platform matches
    if state_data.get("platform") != platform:
        messages.error(request, "Platform mismatch in OAuth callback.")
        return redirect("dashboard")

    # Validate user
    if str(request.user.id) != state_data.get("user_id"):
        raise PermissionDenied("OAuth state does not match current user.")

    workspace_id = state_data["workspace_id"]

    # Re-check workspace membership - user may have lost access during OAuth
    from apps.members.models import WorkspaceMembership

    ws_membership = WorkspaceMembership.objects.filter(user=request.user, workspace_id=workspace_id).first()
    if not ws_membership:
        raise PermissionDenied("You no longer have access to this workspace.")
    perms = ws_membership.effective_permissions
    if not perms.get("manage_social_accounts", False):
        raise PermissionDenied("You no longer have permission to manage social accounts.")

    try:
        # For Mastodon, we need instance-specific credentials from session + registration
        extra_creds: dict = {}
        if platform == PlatformCredential.Platform.MASTODON:
            extra_creds = _resolve_mastodon_extra_creds(session_data)

        # The signed state's workspace and its organization, not ``request.org``: this
        # URL names no workspace, so the middleware cannot say which of the user's
        # organizations the connection is for, and the app credentials that started
        # this flow (an org's own app, or a workspace's own X app) belong to one of them.
        provider = _get_provider_for_platform(
            platform, ws_membership.workspace.organization_id, workspace_id=workspace_id, **extra_creds
        )
        redirect_uri = redirect_uri_from_request(request)
        tokens = provider.exchange_code(code, redirect_uri, **pkce_kwargs(session_data.get("code_verifier")))

        # Facebook/Instagram/LinkedIn Company: connect Pages, not personal profiles
        if platform in (
            PlatformCredential.Platform.FACEBOOK,
            PlatformCredential.Platform.INSTAGRAM,
            PlatformCredential.Platform.LINKEDIN_COMPANY,
        ) and hasattr(provider, "get_user_pages"):
            tokens, promoted = promote_meta_user_token(provider, platform, tokens)
            pages = provider.get_user_pages(tokens.access_token)
            meta_user_id = ""
            if platform != PlatformCredential.Platform.LINKEDIN_COMPANY:
                # Meta's data deletion callback names the user, not the Page.
                try:
                    meta_user_id = str(provider.get_profile(tokens.access_token).platform_id or "")
                except Exception:
                    logger.warning("Could not read the Meta user id for %s", platform)
            if pages:
                # Filtered by workspace only: that set is bounded by what this
                # workspace has connected, while the page list is bounded only
                # by how many Pages the login administers — passing every page
                # id back as an IN clause scales with the wrong number.
                existing_ids = set(
                    SocialAccount.objects.filter(
                        workspace_id=workspace_id,
                        platform=platform,
                    ).values_list("account_platform_id", flat=True)
                )
                for page in pages:
                    page["already_connected"] = page["id"] in existing_ids
                # Store in session for account selection
                request.session["oauth_page_select"] = {
                    "workspace_id": workspace_id,
                    "platform": platform,
                    "user_tokens": {
                        "access_token": tokens.access_token,
                        "refresh_token": tokens.refresh_token,
                        "meta_user_id": meta_user_id,
                        # None once promoted: a Page token derived from a
                        # long-lived user token does not expire. On the fallback
                        # path it is the short-lived expiry, which the account
                        # must carry to be seen as expiring at all.
                        "expires_in": None if promoted else tokens.expires_in,
                        "user_expires_at": (
                            (timezone.now() + timedelta(seconds=tokens.expires_in)).isoformat()
                            if tokens.expires_in
                            else None
                        ),
                    },
                    "pages": pages,
                }
                return redirect("social_accounts:select_account")
            else:
                if platform == PlatformCredential.Platform.LINKEDIN_COMPANY:
                    warning = (
                        "No LinkedIn Company Pages were found for your account. "
                        "Only Company Pages you administer can be connected — "
                        "personal profiles connect via the LinkedIn (Personal) option. "
                        "If you expected to see a Page, ask the page owner to grant "
                        "you Admin access in LinkedIn \u2192 Admin tools \u2192 "
                        "Manage admins, then reconnect."
                    )
                else:
                    if platform == PlatformCredential.Platform.INSTAGRAM:
                        warning = (
                            "No Instagram Business accounts were found for your account. "
                            "Only Instagram Business or Creator accounts linked to a Facebook Page "
                            "can be connected through this Instagram option. If you expected to "
                            "see an account, make sure it is linked to a Page you manage, then reconnect."
                        )
                    else:
                        warning = (
                            "No Facebook Pages were found for your account. "
                            "Only Pages can be connected — personal profiles are not "
                            "supported by the Facebook API. "
                            "If you expected to see a Page, make sure you have admin "
                            "access and try removing the app in Facebook Settings \u2192 "
                            "Business Integrations, then reconnect."
                        )
                messages.warning(request, warning)
                return redirect("social_accounts:list", workspace_id=workspace_id)

        # One grant, several destinations (Discord and Slack channels): let the
        # user pick, through the same picker the Page flows use.
        if _has_destination_picker(platform):
            destinations = provider.list_destinations(tokens)
            if not destinations:
                messages.warning(
                    request,
                    f"No {provider.platform_name} channels were found that {settings.BRAND_NAME} can post to. "
                    + (provider.no_destinations_hint or "Check the app has access to a channel, then reconnect."),
                )
                return redirect("social_accounts:list", workspace_id=workspace_id)
            existing_ids = set(
                SocialAccount.objects.filter(workspace_id=workspace_id, platform=platform).values_list(
                    "account_platform_id", flat=True
                )
            )
            for destination in destinations:
                destination["already_connected"] = destination["id"] in existing_ids
            request.session["oauth_page_select"] = {
                "workspace_id": workspace_id,
                "platform": platform,
                # The grant is only the means of listing the destinations; each
                # account carries the token it posts with (``access_token``).
                "user_tokens": {"access_token": "", "refresh_token": None, "expires_in": None},
                "pages": destinations,
            }
            return redirect("social_accounts:select_account")

        # Standard single-account flow (non-Facebook/Instagram platforms)
        profile = provider.get_profile(tokens.access_token)
        _create_or_update_account(
            workspace_id=workspace_id,
            platform=platform,
            profile=profile,
            authorizing_user=request.user,
            access_token=tokens.access_token,
            refresh_token=tokens.refresh_token,
            expires_in=tokens.expires_in,
            instance_url=extra_creds.get("instance_url", ""),
            platform_data=(profile.extra or {}).get("account_data"),
        )
        messages.success(request, f"Connected {profile.name} successfully.")

    except (signing.BadSignature, PermissionDenied):
        raise
    except PlanLimitError as exc:
        messages.error(request, exc.message)
    except QuotaExceededError as exc:
        # Before the blanket clause below, whose "Please try again" is the one
        # piece of advice that cannot work here: the grant is fine, the
        # platform's daily budget is spent, and every retry until the window
        # rolls over fails identically. This is what a user connecting a
        # YouTube channel at 03:00 UTC was being told to keep doing.
        logger.warning("OAuth callback hit a spent %s quota: %s", platform, exc)
        messages.error(request, quota_connect_error(exc))
    except Exception:
        logger.exception("OAuth callback failed for %s", platform)
        messages.error(
            request,
            "Failed to connect account. Please try again.",
        )

    return _after_connect_redirect(request, workspace_id)


# ------------------------------------------------------------------
# Account Selection (Facebook multi-page)
# ------------------------------------------------------------------


@login_required
def select_account(request):
    """Show page/account selection after multi-page OAuth."""
    page_data = request.session.get("oauth_page_select")
    if not page_data:
        messages.error(request, "No accounts to select. Please start over.")
        return redirect("dashboard")

    workspace_id = page_data["workspace_id"]

    def _render_picker():
        # Resolve publishability here rather than in the template: Django
        # resolves a missing key to string_if_invalid, so `can_publish is False`
        # in markup is a different predicate from the Python one and would drift
        # from it. The template gets a plain bool it can trust.
        rows = [{**page, "can_publish": page_is_publishable(page)} for page in page_data["pages"]]
        return render(
            request,
            "social_accounts/account_select.html",
            {
                "pages": rows,
                "platform": page_data["platform"],
                "workspace_id": workspace_id,
            },
        )

    if request.method == "GET":
        return _render_picker()

    # POST: create accounts for selected pages
    selected_ids = request.POST.getlist("selected_pages")
    if not selected_ids:
        messages.error(request, "Please select at least one account.")
        return _render_picker()

    from providers.types import AccountProfile

    platform = page_data["platform"]
    user_tokens = page_data["user_tokens"]
    connected = []

    for page in page_data["pages"]:
        if page["id"] in selected_ids:
            if not page_is_publishable(page):
                # Meta reported the Page's task list and it lacks CREATE_CONTENT.
                # The template already disables these rows; this guard is what
                # stops a hand-built POST from connecting an account that would
                # fail every publish.
                messages.error(
                    request,
                    f"Could not connect {page['name']}: your Facebook access cannot create content for this Page.",
                )
                continue
            access_token = resolve_page_account_token(page, platform, user_tokens.get("access_token", ""))
            if not access_token:
                messages.error(
                    request,
                    f"Could not connect {page['name']}: the platform did not provide an account token.",
                )
                continue

            profile = AccountProfile(
                platform_id=page["id"],
                name=page["name"],
                handle=page.get("handle"),
                avatar_url=page.get("picture", ""),
                follower_count=page.get("followers_count", 0),
            )
            try:
                _create_or_update_account(
                    workspace_id=workspace_id,
                    platform=platform,
                    profile=profile,
                    access_token=access_token,
                    # A destination may carry the grant's refresh token and expiry itself (Tumblr blogs).
                    refresh_token=page.get("refresh_token") or user_tokens.get("refresh_token"),
                    expires_in=page.get("expires_in") or user_tokens.get("expires_in"),
                    facebook_user_access_token=user_tokens.get("access_token", "") if platform == "instagram" else None,
                    facebook_user_token_expires_at=user_tokens.get("user_expires_at"),
                    meta_user_id=user_tokens.get("meta_user_id", ""),
                    # Instagram-via-Facebook receives its webhooks through the
                    # linked Page, so remember which Page to subscribe.
                    webhook_target_id=page.get("page_id", ""),
                    messaging_access=False
                    if platform == "instagram" and not page.get("access_token")
                    else page.get("can_message"),
                )
            except PlanLimitError as exc:
                messages.error(request, exc.message)
                break
            connected.append(page["name"])

    request.session.pop("oauth_page_select", None)

    if connected:
        names = ", ".join(connected)
        messages.success(request, f"Connected: {names}")

    return _after_connect_redirect(request, workspace_id)


# ------------------------------------------------------------------
# Bluesky Connect (session-based, no OAuth)
# ------------------------------------------------------------------


@login_required
@require_permission("manage_social_accounts")
def connect_bluesky(request, workspace_id):
    """Connect a Bluesky account via handle + app password."""
    if request.method == "GET":
        return render(
            request,
            "social_accounts/bluesky_connect.html",
            {"workspace_id": workspace_id},
        )

    handle = request.POST.get("handle", "").strip().lstrip("@")
    app_password = request.POST.get("app_password", "").strip()

    if not handle or not app_password:
        messages.error(request, "Handle and app password are required.")
        return render(
            request,
            "social_accounts/bluesky_connect.html",
            {"workspace_id": workspace_id},
        )

    try:
        provider = _get_provider_for_platform(
            PlatformCredential.Platform.BLUESKY, request.workspace.organization_id, workspace_id=workspace_id
        )
        tokens = provider.create_session(handle, app_password)
        profile = provider.get_profile(tokens.access_token)

        _create_or_update_account(
            workspace_id=workspace_id,
            platform=PlatformCredential.Platform.BLUESKY,
            profile=profile,
            access_token=tokens.access_token,
            refresh_token=tokens.refresh_token,
            expires_in=tokens.expires_in,
            instance_url=provider.pds_url,
        )
        messages.success(request, f"Connected {profile.name} on Bluesky.")

    except PlanLimitError as exc:
        messages.error(request, exc.message)
        return redirect("social_accounts:list", workspace_id=workspace_id)
    except Exception:
        logger.exception("Bluesky connection failed")
        messages.error(
            request,
            "Failed to connect Bluesky account. Check your handle and app password.",
        )
        return render(
            request,
            "social_accounts/bluesky_connect.html",
            {"workspace_id": workspace_id},
        )

    return _after_connect_redirect(request, workspace_id)


# ------------------------------------------------------------------
# DEV.to Connect (API-key based, no OAuth)
# ------------------------------------------------------------------


@login_required
@require_permission("manage_social_accounts")
def connect_devto(request, workspace_id):
    """Connect a DEV.to account via a personal API key."""
    if request.method == "GET":
        return render(
            request,
            "social_accounts/devto_connect.html",
            {"workspace_id": workspace_id},
        )

    api_key = request.POST.get("api_key", "").strip()
    if not api_key:
        messages.error(request, "A DEV.to API key is required.")
        return render(
            request,
            "social_accounts/devto_connect.html",
            {"workspace_id": workspace_id},
        )

    try:
        provider = _get_provider_for_platform(
            PlatformCredential.Platform.DEVTO, request.workspace.organization_id, workspace_id=workspace_id
        )
        profile = provider.get_profile(api_key)
        _create_or_update_account(
            workspace_id=workspace_id,
            platform=PlatformCredential.Platform.DEVTO,
            profile=profile,
            access_token=api_key,
        )
        messages.success(request, f"Connected {profile.name} on DEV.to.")
    except PlanLimitError as exc:
        messages.error(request, exc.message)
        return redirect("social_accounts:list", workspace_id=workspace_id)
    except Exception:
        logger.exception("DEV.to connection failed")
        messages.error(request, "Failed to connect DEV.to account. Check your API key.")
        return render(request, "social_accounts/devto_connect.html", {"workspace_id": workspace_id})

    return _after_connect_redirect(request, workspace_id)


# ------------------------------------------------------------------
# Pasted-credentials Connect (Medium, WordPress, Hashnode, Listmonk: a token or password, no OAuth)
# ------------------------------------------------------------------


def _connect_with_credentials(request, workspace_id, platform):
    """Render and handle the connect form a provider declares in ``connect_fields``.

    A password input is never sent back to the page. The provider checks the credentials against the
    platform and returns the profile and the secret stored (encrypted) as the account's access token.
    """
    from providers import PROVIDER_REGISTRY

    provider_cls = PROVIDER_REGISTRY[platform]
    label = provider_cls().platform_name

    def render_form(values=None):
        return render(
            request,
            "social_accounts/credentials_connect.html",
            {
                "workspace_id": workspace_id,
                "platform": platform,
                "platform_label": label,
                "intro": provider_cls.connect_intro,
                "notice": provider_cls.connect_notice,
                "connect_url": f"social_accounts:{provider_cls.connect_url_name}",
                "fields": [
                    {"field": field, "value": "" if field.kind == "password" else (values or {}).get(field.key, "")}
                    for field in provider_cls.connect_fields
                ],
            },
        )

    if request.method == "GET":
        return render_form()

    values = {field.key: request.POST.get(field.key, "").strip() for field in provider_cls.connect_fields}
    if not all(values.values()):
        messages.error(request, f"Fill in every {label} field.")
        return render_form(values)

    instance_url = ""
    url_field = provider_cls.connect_url_field
    if url_field:
        # Validate against SSRF - an https address that resolves to a public host only.
        instance_url = provider_cls.normalize_site_url(values[url_field])
        if not instance_url or not _is_safe_url(instance_url):
            messages.error(request, "Enter a public https address. Private or reserved addresses are not allowed.")
            return render_form(values)
        values[url_field] = instance_url

    try:
        provider = _get_provider_for_platform(platform, request.workspace.organization_id, workspace_id=workspace_id)
        profile, secret = provider.connect_with_form(values)
        _create_or_update_account(
            workspace_id=workspace_id,
            platform=platform,
            profile=profile,
            access_token=secret,
            instance_url=instance_url,
            platform_data=(profile.extra or {}).get("account_data"),
        )
        messages.success(request, f"Connected {profile.name} on {label}.")
    except PlanLimitError as exc:
        messages.error(request, exc.message)
        return redirect("social_accounts:list", workspace_id=workspace_id)
    except PublishError as exc:
        messages.error(request, str(exc))
        return render_form(values)
    except Exception:
        logger.exception("%s connection failed", label)
        messages.error(request, f"Failed to connect {label}. Check the details and try again.")
        return render_form(values)

    return _after_connect_redirect(request, workspace_id)


@login_required
@require_permission("manage_social_accounts")
@ratelimit(key="user", rate="20/m", method="POST", block=True)
def connect_medium(request, workspace_id):
    """Connect Medium with an existing integration token."""
    return _connect_with_credentials(request, workspace_id, PlatformCredential.Platform.MEDIUM)


@login_required
@require_permission("manage_social_accounts")
@ratelimit(key="user", rate="20/m", method="POST", block=True)
def connect_wordpress(request, workspace_id):
    """Connect a self-hosted WordPress site with an Application Password."""
    return _connect_with_credentials(request, workspace_id, PlatformCredential.Platform.WORDPRESS)


@login_required
@require_permission("manage_social_accounts")
@ratelimit(key="user", rate="20/m", method="POST", block=True)
def connect_hashnode(request, workspace_id):
    """Connect Hashnode with a personal access token."""
    return _connect_with_credentials(request, workspace_id, PlatformCredential.Platform.HASHNODE)


@login_required
@require_permission("manage_social_accounts")
@ratelimit(key="user", rate="20/m", method="POST", block=True)
def connect_listmonk(request, workspace_id):
    """Connect a self-hosted Listmonk instance with an API user."""
    return _connect_with_credentials(request, workspace_id, PlatformCredential.Platform.LISTMONK)


# ------------------------------------------------------------------
# Telegram Connect (platform bot added to a channel or group, no OAuth)
# ------------------------------------------------------------------


TELEGRAM_CODE_SESSION_KEY = "telegram_connect_codes"


def _telegram_verification_code(request, workspace_id, *, issue=True):
    """The one-time code this user must put in the chat's description to prove they run it.

    The bot is shared by every customer, so the bot being an administrator of a
    chat proves nothing about who is connecting it: anyone could name a channel
    another customer added the bot to. Editing the chat's description takes an
    administrator of that chat, and ``getChat`` reads it back without needing
    the bot's update stream (which a webhook or another poller may own).
    """
    codes = request.session.get(TELEGRAM_CODE_SESSION_KEY) or {}
    code = codes.get(str(workspace_id), "")
    if not code and issue:
        code = f"verify-{secrets.token_hex(5)}"
        request.session[TELEGRAM_CODE_SESSION_KEY] = {**codes, str(workspace_id): code}
    return code


@login_required
@require_permission("manage_social_accounts")
def connect_telegram(request, workspace_id):
    """Connect a Telegram channel or group the platform bot administers and the user controls."""
    platform = PlatformCredential.Platform.TELEGRAM
    provider = _get_provider_for_platform(platform, request.workspace.organization_id, workspace_id=workspace_id)

    def render_form(chat=""):
        try:
            bot_username = provider.get_bot_username()
        except Exception:
            logger.warning("Could not read the Telegram bot's username", exc_info=True)
            bot_username = ""
        return render(
            request,
            "social_accounts/telegram_connect.html",
            {
                "workspace_id": workspace_id,
                "bot_username": bot_username,
                "verification_code": _telegram_verification_code(request, workspace_id),
                "chat": chat,
            },
        )

    if request.method == "GET":
        return render_form()

    raw_chat = request.POST.get("chat", "")
    chat = provider.normalize_chat_reference(raw_chat)
    if not chat:
        messages.error(request, "Enter the channel as @username, a t.me link, or its numeric chat id.")
        return render_form(raw_chat)

    # Read from the session, never the form: the code is what the user was shown.
    code = _telegram_verification_code(request, workspace_id, issue=False)
    if not code:
        messages.error(request, "Your verification code expired. Add the new code below to the chat, then connect.")
        return render_form(raw_chat)

    try:
        profile = provider.get_profile(chat)
        if code not in (profile.extra or {}).get("description", ""):
            messages.error(
                request,
                f"The code {code} isn't in {profile.name}'s description yet. Add it, save, then connect again.",
            )
            return render_form(raw_chat)
        # The chat id is the account's stored credential; the bot token stays in the environment.
        _create_or_update_account(
            workspace_id=workspace_id,
            platform=platform,
            profile=profile,
            access_token=profile.platform_id,
        )
        messages.success(
            request, f"Connected {profile.name} on Telegram. You can remove the code from its description."
        )
    except PlanLimitError as exc:
        messages.error(request, exc.message)
        return redirect("social_accounts:list", workspace_id=workspace_id)
    except PublishError as exc:
        messages.error(request, str(exc))
        return render_form(raw_chat)
    except APIError as exc:
        logger.warning("Telegram connection failed: %s", exc)
        messages.error(
            request,
            "Telegram couldn't find that chat. Check the name, and make sure the bot was added to it as an administrator.",
        )
        return render_form(raw_chat)
    except Exception:
        logger.exception("Telegram connection failed")
        messages.error(request, "Failed to connect Telegram. Please try again.")
        return render_form(raw_chat)

    # One use: the next connection proves control with a fresh code.
    codes = dict(request.session.get(TELEGRAM_CODE_SESSION_KEY) or {})
    codes.pop(str(workspace_id), None)
    request.session[TELEGRAM_CODE_SESSION_KEY] = codes
    return _after_connect_redirect(request, workspace_id)


# ------------------------------------------------------------------
# X Connect (the customer's own developer app)
# ------------------------------------------------------------------


@csp_update(FORM_ACTION="'self' https:")
@login_required
@require_permission("manage_social_accounts")
@ratelimit(key="user", rate="20/m", method="POST", block=True)
def connect_x(request, workspace_id):
    """Collect the customer's own X app credentials, then start OAuth 2.0 + PKCE against that app.

    X bills API use to the owner of the developer app, so there are no platform-wide
    credentials. What is entered here is stored as this workspace's encrypted
    ``PlatformCredential``: an agency's clients each bring (and pay for) their own app, and
    a workspace manager can only ever change the app of their own workspace. A workspace
    without one uses the organization's app when an administrator set one up. The secret
    is never sent back to the page; leaving it blank keeps the saved one.
    """
    platform = PlatformCredential.Platform.X
    workspace = request.workspace
    org_id = workspace.organization_id
    own = PlatformCredential.objects.for_org(org_id).filter(platform=platform, workspace_id=workspace_id).first()
    current = own or configured_platform_credential(platform, org_id, workspace_id)
    current_credentials = dict(current.credentials) if current else {}

    def render_form(client_id=""):
        return render(
            request,
            "social_accounts/x_connect.html",
            {
                "workspace_id": workspace_id,
                "callback_url": _build_redirect_uri(request, platform),
                "client_id": client_id or current_credentials.get("client_id", ""),
                "has_secret": bool(current_credentials.get("client_secret")),
                "uses_org_app": current is not None and current.workspace_id is None,
            },
        )

    if request.method == "GET":
        return render_form()

    client_id = request.POST.get("client_id", "").strip()
    client_secret = request.POST.get("client_secret", "").strip()
    if not client_secret and client_id == current_credentials.get("client_id"):
        # A blank secret keeps the saved one, but only for the app it belongs to.
        client_secret = current_credentials.get("client_secret", "")
    if not client_id or not client_secret:
        messages.error(request, "Enter the Client ID and Client Secret of your X app.")
        return render_form(client_id)

    try:
        check_can_add_channel(workspace.organization)
    except PlanLimitError as exc:
        messages.error(request, exc.message)
        return render_form(client_id)

    credentials = {"client_id": client_id, "client_secret": client_secret}
    if (
        current_credentials.get("client_id") not in (None, client_id)
        and SocialAccount.objects.filter(workspace_id=workspace_id, platform=platform).exists()
    ):
        # Tokens belong to the app that issued them: switching apps would break every connected X account.
        messages.error(
            request,
            "X accounts in this workspace are already connected through your current X app. "
            "Disconnect them before using another app.",
        )
        return render_form(client_id)
    if own is None and current_credentials != credentials:
        PlatformCredential.objects.create(
            organization_id=org_id, workspace_id=workspace_id, platform=platform, credentials=credentials
        )
    elif own is not None and current_credentials != credentials:
        own.credentials = credentials
        own.save()
    return _start_oauth(request, workspace_id, platform)


# ------------------------------------------------------------------
# Mastodon Connect (instance-based OAuth)
# ------------------------------------------------------------------


@csp_update(FORM_ACTION="'self' https:")
@login_required
@require_permission("manage_social_accounts")
def connect_mastodon(request, workspace_id):
    """Connect a Mastodon account via instance URL + OAuth."""
    if request.method == "GET":
        return render(
            request,
            "social_accounts/mastodon_connect.html",
            {"workspace_id": workspace_id},
        )

    instance_url = _normalize_mastodon_instance_url(request.POST.get("instance_url", ""))
    if not instance_url:
        messages.error(request, "Instance URL is required.")
        return render(
            request,
            "social_accounts/mastodon_connect.html",
            {"workspace_id": workspace_id},
        )

    # Validate against SSRF - reject private/reserved IP ranges
    if not _is_safe_url(instance_url):
        messages.error(request, "Invalid instance URL. Private or reserved addresses are not allowed.")
        return render(
            request,
            "social_accounts/mastodon_connect.html",
            {"workspace_id": workspace_id},
        )

    # Check for existing app registration or create one
    try:
        registration = MastodonAppRegistration.objects.get(instance_url=instance_url)
        client_id = registration.client_id
        client_secret = registration.client_secret
    except MastodonAppRegistration.DoesNotExist:
        # Register app on this instance
        try:
            provider = _get_provider_for_platform(
                PlatformCredential.Platform.MASTODON,
                request.workspace.organization_id,
                workspace_id=workspace_id,
                instance_url=instance_url,
            )
            redirect_uri = _build_redirect_uri(request, PlatformCredential.Platform.MASTODON)
            app_data = provider.register_app(instance_url, redirect_uri)
            registration = MastodonAppRegistration.objects.create(
                instance_url=instance_url,
                client_id=app_data["client_id"],
                client_secret=app_data["client_secret"],
            )
            client_id = app_data["client_id"]
            client_secret = app_data["client_secret"]
        except Exception:
            logger.exception("Mastodon app registration failed for %s", instance_url)
            messages.error(
                request,
                f"Failed to register with {instance_url}. Check the URL.",
            )
            return render(
                request,
                "social_accounts/mastodon_connect.html",
                {"workspace_id": workspace_id},
            )

    # Initiate OAuth
    provider = _get_provider_for_platform(
        PlatformCredential.Platform.MASTODON,
        request.workspace.organization_id,
        workspace_id=workspace_id,
        instance_url=instance_url,
        client_id=client_id,
        client_secret=client_secret,
    )

    nonce = secrets.token_urlsafe(32)
    state = _sign_state(
        workspace_id,
        PlatformCredential.Platform.MASTODON,
        request.user.id,
        nonce,
    )

    request.session[OAUTH_SESSION_KEY] = {
        "nonce": nonce,
        "workspace_id": str(workspace_id),
        "platform": PlatformCredential.Platform.MASTODON,
        "instance_url": instance_url,
    }

    redirect_uri = _build_redirect_uri(request, PlatformCredential.Platform.MASTODON)
    auth_url = provider.get_auth_url(redirect_uri, state)
    return redirect(auth_url)


# ------------------------------------------------------------------
# Lemmy Connect (instance login, only the JWT is kept)
# ------------------------------------------------------------------


@login_required
@require_permission("manage_social_accounts")
@ratelimit(key="user", rate="10/m", method="POST", block=True)
def connect_lemmy(request, workspace_id):
    """Connect a Lemmy account: instance address, username and password. The password is never stored."""
    from providers.lemmy import normalize_instance_url

    def render_form(instance_url=""):
        return render(
            request,
            "social_accounts/lemmy_connect.html",
            {"workspace_id": workspace_id, "instance_url": instance_url},
        )

    if request.method == "GET":
        return render_form()

    instance_url = normalize_instance_url(request.POST.get("instance_url", ""))
    username = request.POST.get("username", "").strip()
    password = request.POST.get("password", "")
    totp_token = request.POST.get("totp_token", "").strip()
    if not instance_url or not username or not password:
        messages.error(request, "Instance, username and password are required.")
        return render_form(instance_url)
    if not _is_safe_url(instance_url):
        messages.error(request, "Invalid instance URL. Private or reserved addresses are not allowed.")
        return render_form(instance_url)

    try:
        provider = _get_provider_for_platform(
            PlatformCredential.Platform.LEMMY,
            request.workspace.organization_id,
            workspace_id=workspace_id,
            instance_url=instance_url,
        )
        tokens = provider.login(username, password, totp_token)
        profile = provider.get_profile(tokens.access_token)
        _create_or_update_account(
            workspace_id=workspace_id,
            platform=PlatformCredential.Platform.LEMMY,
            profile=profile,
            access_token=tokens.access_token,
            instance_url=instance_url,
        )
        messages.success(request, f"Connected {profile.name} on Lemmy.")
    except PlanLimitError as exc:
        messages.error(request, exc.message)
        return redirect("social_accounts:list", workspace_id=workspace_id)
    except Exception:
        logger.exception("Lemmy connection failed")
        messages.error(
            request,
            "Failed to connect Lemmy account. Check the instance, username and password"
            " (and the two-factor code, if the account has one).",
        )
        return render_form(instance_url)

    return _after_connect_redirect(request, workspace_id)


# ------------------------------------------------------------------
# Nostr Connect (private key + relays)
# ------------------------------------------------------------------


@login_required
@require_permission("manage_social_accounts")
@ratelimit(key="user", rate="10/m", method="POST", block=True)
def connect_nostr(request, workspace_id):
    """Connect a Nostr identity: its private key (nsec or hex) and the relays to publish to."""
    from providers.nostr import DEFAULT_RELAYS, normalize_relays

    def render_form(relays=""):
        return render(
            request,
            "social_accounts/nostr_connect.html",
            {"workspace_id": workspace_id, "relays": relays or "\n".join(DEFAULT_RELAYS)},
        )

    if request.method == "GET":
        return render_form()

    secret_key = request.POST.get("secret_key", "").strip()
    relays_text = request.POST.get("relays", "")
    relays = normalize_relays(relays_text)
    if not secret_key:
        messages.error(request, "A private key is required.")
        return render_form(relays_text)
    if not relays:
        messages.error(request, "Add at least one relay address starting with wss://.")
        return render_form(relays_text)

    try:
        provider = _get_provider_for_platform(
            PlatformCredential.Platform.NOSTR,
            request.workspace.organization_id,
            workspace_id=workspace_id,
            relays=relays,
        )
        tokens, profile = provider.connect(secret_key)
        _create_or_update_account(
            workspace_id=workspace_id,
            platform=PlatformCredential.Platform.NOSTR,
            profile=profile,
            access_token=tokens.access_token,
            platform_data=(profile.extra or {}).get("account_data"),
        )
        messages.success(request, f"Connected {profile.name} on Nostr.")
    except PlanLimitError as exc:
        messages.error(request, exc.message)
        return redirect("social_accounts:list", workspace_id=workspace_id)
    except ValueError as exc:
        # The key itself is not echoed anywhere; the message only says what format is expected.
        messages.error(request, str(exc))
        return render_form(relays_text)
    except Exception:
        logger.exception("Nostr connection failed")
        messages.error(request, "Failed to connect Nostr account. Check the private key and relays.")
        return render_form(relays_text)

    return _after_connect_redirect(request, workspace_id)


# ------------------------------------------------------------------
# Farcaster Connect (the customer's own Neynar API key + approved signer)
# ------------------------------------------------------------------


@login_required
@require_permission("manage_social_accounts")
@ratelimit(key="user", rate="10/m", method="POST", block=True)
def connect_farcaster(request, workspace_id):
    """Connect Farcaster through the customer's own Neynar key and an approved signer.

    Neynar bills API use to the key's owner, so the key is the workspace's own encrypted
    ``PlatformCredential`` (as with X). A signer only works with the key it was created
    with, so a workspace with Farcaster accounts cannot switch to another key. The key is
    never sent back to the page; leaving it blank keeps the saved one.
    """
    platform = PlatformCredential.Platform.FARCASTER
    workspace = request.workspace
    org_id = workspace.organization_id
    own = PlatformCredential.objects.for_org(org_id).filter(platform=platform, workspace_id=workspace_id).first()
    current = own or configured_platform_credential(platform, org_id, workspace_id)
    current_credentials = dict(current.credentials) if current else {}

    def render_form():
        return render(
            request,
            "social_accounts/farcaster_connect.html",
            {
                "workspace_id": workspace_id,
                "has_api_key": bool(current_credentials.get("api_key")),
                "uses_org_key": current is not None and current.workspace_id is None,
            },
        )

    if request.method == "GET":
        return render_form()

    api_key = request.POST.get("api_key", "").strip() or current_credentials.get("api_key", "")
    signer_uuid = request.POST.get("signer_uuid", "").strip()
    if not api_key or not signer_uuid:
        messages.error(request, "Enter your Neynar API key and the UUID of an approved signer.")
        return render_form()
    if (
        current_credentials.get("api_key") not in (None, api_key)
        and SocialAccount.objects.filter(workspace_id=workspace_id, platform=platform).exists()
    ):
        # A signer belongs to the key that created it: switching keys would break every connected account.
        messages.error(
            request,
            "Farcaster accounts in this workspace are already connected through your current Neynar key. "
            "Disconnect them before using another key.",
        )
        return render_form()

    try:
        check_can_add_channel(workspace.organization)
        provider = _get_provider_for_platform(platform, org_id, workspace_id=workspace_id, api_key=api_key)
        tokens, profile = provider.connect(signer_uuid)
        _create_or_update_account(
            workspace_id=workspace_id, platform=platform, profile=profile, access_token=tokens.access_token
        )
        credentials = {"api_key": api_key}
        if own is None and current_credentials != credentials:
            PlatformCredential.objects.create(
                organization_id=org_id, workspace_id=workspace_id, platform=platform, credentials=credentials
            )
        elif own is not None and current_credentials != credentials:
            own.credentials = credentials
            own.save()
        messages.success(request, f"Connected {profile.name} on Farcaster.")
    except PlanLimitError as exc:
        messages.error(request, exc.message)
        return render_form()
    except OAuthError as exc:
        # Our own wording about the signer or the missing key, safe to show.
        messages.error(request, str(exc))
        return render_form()
    except Exception:
        logger.exception("Farcaster connection failed")
        messages.error(
            request,
            "Failed to connect Farcaster. Check the Neynar API key and that the signer UUID was created with it.",
        )
        return render_form()

    return _after_connect_redirect(request, workspace_id)


# ------------------------------------------------------------------
# Reconnect
# ------------------------------------------------------------------


@login_required
@require_permission("manage_social_accounts")
@require_POST
def reconnect(request, workspace_id, account_id):
    """Re-initiate OAuth for an existing account."""
    account = get_object_or_404(SocialAccount.objects.for_workspace(workspace_id), id=account_id)
    platform = account.platform

    if platform == PlatformCredential.Platform.BLUESKY:
        return redirect("social_accounts:connect_bluesky", workspace_id=workspace_id)
    if platform == PlatformCredential.Platform.MASTODON:
        return redirect("social_accounts:connect_mastodon", workspace_id=workspace_id)
    if platform == PlatformCredential.Platform.DEVTO:
        return redirect("social_accounts:connect_devto", workspace_id=workspace_id)
    connect_url_name = _connect_url_name(platform)
    if connect_url_name:
        return redirect(connect_url_name, workspace_id=workspace_id)

    # Standard OAuth reconnect
    provider = _get_provider_for_platform(platform, request.workspace.organization_id, workspace_id=workspace_id)
    apply_analytics_scope_flag(provider, platform)
    nonce = secrets.token_urlsafe(32)
    state = _sign_state(workspace_id, platform, request.user.id, nonce)
    code_verifier = issue_pkce_verifier(provider)

    request.session[OAUTH_SESSION_KEY] = {
        "nonce": nonce,
        "workspace_id": str(workspace_id),
        "platform": platform,
        "code_verifier": code_verifier,
    }

    redirect_uri = _build_redirect_uri(request, platform)
    auth_url = provider.get_auth_url(redirect_uri, state, **pkce_kwargs(code_verifier))
    return redirect(auth_url)


@login_required
@require_permission("manage_social_accounts")
@require_POST
@ratelimit(key="user", rate="10/m", method="POST", block=True)
def retry_webhooks(request, workspace_id, account_id):
    """Re-run a failed webhook subscription without a new OAuth grant.

    A subscription can fail for reasons that have nothing to do with the token —
    a transient Graph error, or a bug in what we asked for — and sending the
    user through the full OAuth dance to retry one API call is theatre. Runs
    inline rather than through ``subscribe_account_webhooks_task``: that task
    exists so connecting *several* Pages at once doesn't stack a round trip per
    Page inside the redirect the user is waiting on, which a single deliberate
    retry doesn't. Rate limited because each press is a live round trip to the
    platform on a request thread.
    """
    account = get_object_or_404(SocialAccount.objects.for_workspace(workspace_id), id=account_id)

    # A dead connection cannot carry a subscription: the call would spend a
    # round trip to be rejected, then overwrite the warning with one about
    # real-time delivery when the real problem is the connection itself. Gated
    # on the same ``needs_reconnect`` the card uses to hide the retry button, so
    # this can only be reached by a stale card or a direct POST — and answers
    # both with the reconnect state rather than an unchanged card.
    if account.needs_reconnect:
        if request.headers.get("HX-Request"):
            return _render_account_card(request, account, workspace_id)
        messages.error(request, f"Reconnect {account.display_label} first — its connection isn't healthy.")
        return redirect("social_accounts:list", workspace_id=workspace_id)

    # An explicit press is a fresh mandate: clear the automatic retry budget so
    # a user can always get one more attempt out of a capped-out account.
    SocialAccount.objects.filter(pk=account.pk).update(webhook_retry_count=0)
    account.webhook_retry_count = 0

    subscribed = subscribe_account_webhooks(account)
    account.refresh_from_db()

    if request.headers.get("HX-Request"):
        return _render_account_card(request, account, workspace_id)

    if subscribed:
        messages.success(request, f"Real-time updates are back on for {account.display_label}.")
    else:
        messages.error(request, account.webhook_error or "Couldn't set up real-time updates. Please try again.")
    return redirect("social_accounts:list", workspace_id=workspace_id)


def _render_account_card(request, account, workspace_id, **context):
    """Render one account card for an htmx ``outerHTML`` swap of ``#account-<id>``."""
    return render(
        request,
        "social_accounts/partials/_account_card.html",
        {"account": account, "workspace_id": workspace_id, **context},
    )


# ------------------------------------------------------------------
# Channel options: nickname, picture, profile address, pause
# ------------------------------------------------------------------


@login_required
@require_permission("manage_social_accounts")
@require_POST
def update_options(request, workspace_id, account_id):
    """Save the account's display overrides (nickname, picture, profile address). Display only."""
    account = get_object_or_404(SocialAccount.objects.for_workspace(workspace_id), id=account_id)
    form = ChannelOptionsForm(request.POST)
    if form.is_valid():
        for field, value in form.cleaned_data.items():
            setattr(account, field, value)
        account.save(update_fields=[*form.cleaned_data, "updated_at"])
        form = None
    if request.headers.get("HX-Request"):
        # Errors re-render the card with the dialog still open, so the person can fix the field.
        return _render_account_card(request, account, workspace_id, options_form=form)
    if form is None:
        messages.success(request, f"Saved the options for {account.display_name}.")
    else:
        messages.error(request, "; ".join(error for errors in form.errors.values() for error in errors))
    return redirect("social_accounts:list", workspace_id=workspace_id)


@login_required
@require_permission("manage_social_accounts")
@require_POST
def set_paused(request, workspace_id, account_id):
    """Pause or resume publishing to the account (``paused=1`` / ``paused=0``).

    Posts scheduled to a paused account stay scheduled and are held by the publisher. Resuming takes
    ``held_posts`` (requeue, the default, / publish_now / skip): what happens to those whose time passed
    meanwhile. The card asks first when there are any.
    """
    account = get_object_or_404(SocialAccount.objects.for_workspace(workspace_id), id=account_id)
    pause = request.POST.get("paused") == "1"
    held_posts = request.POST.get("held_posts") or calendar_services.DEFAULT_HELD_POSTS
    if not pause and held_posts not in calendar_services.HELD_POSTS_CHOICES:
        return HttpResponseBadRequest("Unknown choice for the held posts.")
    was_paused = account.is_paused
    if pause:
        if account.set_paused(True, by=request.user):
            autonomy_service.record_account_pause(account.workspace, human_actor(request.user), account, paused=True)
    else:
        outcome = calendar_services.resume_publishing(account, held_posts, by=request.user)
        if was_paused:
            autonomy_service.record_account_pause(
                account.workspace,
                human_actor(request.user),
                account,
                paused=False,
                held_posts=held_posts,
                outcome=outcome,
            )
    if request.headers.get("HX-Request"):
        return _render_account_card(request, account, workspace_id)
    messages.success(
        request,
        f"Publishing to {account.display_name} is paused." if pause else f"Publishing to {account.display_name} is on.",
    )
    return redirect("social_accounts:list", workspace_id=workspace_id)


# ------------------------------------------------------------------
# Disconnect
# ------------------------------------------------------------------


def _emit_removed(account):
    """``account.disconnected`` for a member removing the account, queued in the deleting transaction.

    An account that had already lost its connection was reported then.
    """
    if not account.needs_reconnect:
        webhook_events.account_disconnected(account, reason="Removed by a workspace member.", removed=True)


@login_required
@require_permission("manage_social_accounts")
@require_POST
def disconnect(request, workspace_id, account_id):
    """Disconnect a social account."""
    account = get_object_or_404(SocialAccount.objects.for_workspace(workspace_id), id=account_id)
    if account.platform == "youtube":
        from .youtube_compliance import purge_connection

        name = account.account_name or account.account_handle
        with transaction.atomic():
            _emit_removed(account)
            purge_connection(account)
        messages.success(request, f"Disconnected {name}. YouTube data was deleted immediately.")
        if request.headers.get("HX-Request"):
            return render(request, "social_accounts/partials/_empty.html")
        return redirect("social_accounts:list", workspace_id=workspace_id)

    # Stop the platform pushing us this account's activity before we drop the
    # token that would let us unsubscribe.
    if account.oauth_access_token:
        unsubscribe_account_webhooks(account)

    # Try to revoke token. The account's own credentials, not just the org's:
    # a Mastodon grant lives on the account's instance, under that instance's
    # app registration, and revoking without them calls an empty base URL.
    try:
        from apps.publisher.engine import _resolve_publish_credentials
        from providers import get_provider

        provider = get_provider(account.platform, _resolve_publish_credentials(account))
        if account.revocation_token:
            provider.revoke_token(account.revocation_token)
    except Exception:
        logger.warning(
            "Failed to revoke token for %s, proceeding with disconnect",
            account,
        )

    # Delete posts that ONLY target this account (will be fully orphaned).
    # Multi-platform posts keep their other PlatformPost targets via cascade.
    from django.db.models import Count

    from apps.composer.models import PlatformPost, Post

    orphan_post_ids = list(
        PlatformPost.objects.filter(social_account=account)
        .values("post_id")
        .annotate(total_platforms=Count("post__platform_posts"))
        .filter(total_platforms=1)
        .values_list("post_id", flat=True)
    )
    if orphan_post_ids:
        Post.objects.filter(id__in=orphan_post_ids).delete()

    account_name = account.account_name or account.account_handle
    with transaction.atomic():
        _emit_removed(account)
        account.delete()

    messages.success(request, f"Disconnected {account_name}.")

    # HTMX partial response
    if request.headers.get("HX-Request"):
        return render(request, "social_accounts/partials/_empty.html")

    return redirect("social_accounts:list", workspace_id=workspace_id)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def promote_meta_user_token(provider, platform, tokens) -> tuple[object, bool]:
    """Trade a Meta authorization-code token for a long-lived one, if we can.

    Returns ``(tokens, promoted)``. ``promoted`` is what callers must key the
    stored expiry off: a Page token derived from a *long-lived* user token does
    not expire and should be stored with no expiry at all, while one derived
    from the short-lived token we fell back to expires within the hour and has
    to carry that expiry or the health and publish checks will never look at it.

    Meta's code exchange returns a *short-lived* user token, and every Page
    token derived from it inherits that lifetime — which is how an agency
    onboarding fifty Pages ends up with fifty credentials that expire before
    the first scheduled post runs. Exchanging first yields durable Page tokens.

    Best-effort on purpose. The exchange can fail for reasons that say nothing
    about whether the grant is usable (a transient Graph 5xx, an app secret the
    deployment has rotated), and the callback's error handling turns any raise
    into "Failed to connect account". A short-lived token still connects and
    still publishes today, so a failure here degrades the connection's lifetime
    rather than blocking it.
    """
    if platform not in (
        PlatformCredential.Platform.FACEBOOK,
        PlatformCredential.Platform.INSTAGRAM,
    ):
        return tokens, False
    try:
        return provider.refresh_token(tokens.access_token), True
    except Exception:
        # Deliberately broad. SocialProvider._request only converts HTTP status
        # codes into ProviderError — httpx transport failures (connect, read
        # timeout) propagate unwrapped, and a timeout is the likeliest
        # transient of all. Catching only ProviderError would let exactly the
        # case this fallback exists for reach the callback's error handler and
        # fail a connect that the short-lived token would have completed.
        logger.warning(
            "Could not exchange the %s user token for a long-lived one; continuing with the short-lived token.",
            platform,
            exc_info=True,
        )
        return tokens, False


def page_is_publishable(page: dict) -> bool:
    """Whether a Page dict from ``get_user_pages`` may be connected.

    The providers compute ``can_publish`` from Meta's per-Page task list; this
    is the single reading of that flag every caller must share. Absent means a
    provider that does not report publishability at all (LinkedIn Company), not
    "no".

    Kept beside ``resolve_page_account_token`` for the same reason that one
    exists: the interactive picker and the connection-link flow had each written
    their own copy of a page-eligibility rule once already, and diverged.
    """
    return page.get("can_publish", True) is not False


def resolve_page_account_token(page: dict, platform: str, user_access_token: str) -> str:
    """Pick the token a Page-backed account must be driven by.

    A Facebook Page needs its *own* Page token: a user token publishes under
    the wrong identity, and — because the only revoke endpoint that accepts it
    revokes the app for the whole person — makes a per-account disconnect able
    to sever every other connection they have.

    Legacy Instagram publishing/comments can fall back to the user token when
    Meta omits the Page token. Such a connection must keep DMs disabled.

    Returns "" when no usable token exists, which callers must treat as "cannot
    connect this account" rather than substituting one.
    """
    token = page.get("access_token")
    if not token and platform == PlatformCredential.Platform.INSTAGRAM:
        token = user_access_token
    return token or ""


@transaction.atomic
def _create_or_update_account(
    *,
    workspace_id,
    platform,
    profile,
    access_token,
    refresh_token=None,
    expires_in=None,
    instance_url="",
    webhook_target_id="",
    facebook_user_access_token=None,
    facebook_user_token_expires_at=None,
    messaging_access=None,
    authorizing_user=None,
    platform_data=None,
    meta_user_id="",
):
    """Create or update a SocialAccount from OAuth results."""
    from apps.onboarding.sample import SAMPLE_CONNECT_REFUSAL, is_sample_workspace

    # The example workspace is for looking around: real channels go in a real workspace.
    if is_sample_workspace(workspace_id):
        raise PermissionDenied(SAMPLE_CONNECT_REFUSAL)
    token_expires_at = None
    if expires_in:
        token_expires_at = timezone.now() + timedelta(seconds=expires_in)

    analytics_defaults = {}
    if platform_data is not None:
        analytics_defaults["platform_data"] = platform_data
    if platform == "youtube":
        from apps.organizations.models import Organization

        org = Organization.objects.select_for_update().get(workspaces__pk=workspace_id)
        if org.is_deletion_pending:
            raise PermissionDenied("Cancel organization deletion before reconnecting YouTube.")
        analytics_defaults = {
            "youtube_authorization_checked_at": timezone.now(),
            "youtube_profile_refreshed_at": timezone.now(),
        }
        if authorizing_user is not None:
            analytics_defaults["youtube_authorized_by"] = authorizing_user
    if platform == "instagram" and facebook_user_access_token is not None:
        from django.utils.dateparse import parse_datetime

        if isinstance(facebook_user_token_expires_at, str):
            facebook_user_token_expires_at = parse_datetime(facebook_user_token_expires_at)
        analytics_defaults = {
            "oauth_facebook_user_access_token": facebook_user_access_token,
            "facebook_user_token_expires_at": facebook_user_token_expires_at,
            "analytics_status": SocialAccount.AnalyticsStatus.CHECKING,
        }

    # Reconnecting an account it already has never counts against the plan.
    if (
        billing_enabled()
        and not SocialAccount.objects.filter(
            workspace_id=workspace_id, platform=platform, account_platform_id=profile.platform_id
        ).exists()
    ):
        from apps.workspaces.models import Workspace

        check_can_add_channel(Workspace.objects.select_related("organization").get(pk=workspace_id).organization)

    if meta_user_id:
        analytics_defaults = {**analytics_defaults, "meta_user_id": meta_user_id}

    account, created = SocialAccount.objects.update_or_create(
        workspace_id=workspace_id,
        platform=platform,
        account_platform_id=profile.platform_id,
        defaults={
            **analytics_defaults,
            "account_name": profile.name,
            "account_handle": profile.handle or "",
            "avatar_url": profile.avatar_url or "",
            "follower_count": profile.follower_count,
            "oauth_access_token": access_token,
            "oauth_refresh_token": refresh_token or "",
            "token_expires_at": token_expires_at,
            "instance_url": instance_url,
            "webhook_target_id": webhook_target_id or "",
            "messaging_access": messaging_access,
            "connection_status": SocialAccount.ConnectionStatus.CONNECTED,
            "last_error": "",
            # Fresh OAuth grant invalidates any prior analytics-scope failure.
            "analytics_needs_reconnect": False,
            # Likewise the webhook verdict: the subscription is about to be
            # retried below, and subscribe_account_webhooks returns early
            # without recording when the provider has no webhooks at all — so
            # without this reset a stale failure would outlive the reconnect
            # that was supposed to clear it.
            "webhooks_active": None,
            "webhook_error": "",
            "webhook_needs_reconnect": False,
            "webhook_error_detail": "",
            "webhook_retry_count": 0,
        },
    )

    if created:
        from apps.calendar.services import create_default_queue_and_slots

        create_default_queue_and_slots(account)

    if platform == "youtube":
        from django.db.models import F

        SocialAccount.objects.filter(pk=account.pk).update(youtube_data_generation=F("youtube_data_generation") + 1)
        account.refresh_from_db()

    subscribe_account_webhooks_task(str(account.id))

    return account
