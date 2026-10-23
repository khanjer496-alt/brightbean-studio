"""Views for the AI writing assistant.

Composer actions (HTMX POSTs from the composer's AI panel) render small
result partials into the panel; the panel applies them to the caption or the
per-account overrides client-side, so nothing is saved until the user saves
the post. Failures answer with a 4xx/5xx whose plain-text body is the
user-facing message — the panel's ``data-inline-error`` shows it in place.

The organization settings page stores the organization's own provider key.
"""

from __future__ import annotations

import functools
import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods, require_POST
from django_ratelimit.decorators import ratelimit

from apps.billing.entitlements import PlanLimitError, billing_enabled, get_entitlements
from apps.common.htmx import trigger_response
from apps.members.decorators import require_org_role, require_permission
from apps.members.models import OrgMembership
from apps.social_accounts.models import SocialAccount

from . import services
from .client import AI_PROVIDERS, PROVIDER_CHOICES, AIError, ai_available, platform_key_configured
from .models import OrganizationAIKey
from .services import AIContext

logger = logging.getLogger(__name__)

# Per-user burst cap on the composer actions. The plan's daily allowance is the
# real budget; this only stops a stuck button or script from spending it in
# one go.
AI_ACTION_RATE = "20/m"


def _ai_view(view_func):
    """Shared guard for the composer actions: login, permission, POST, rate, AI on.

    The wrapped view gets an ``AIContext`` instead of ``workspace_id``, and any
    ``ValueError`` / ``PlanLimitError`` / ``AIError`` it raises becomes a
    plain-text error response the panel shows inline.
    """

    @functools.wraps(view_func)
    def _wrapped(request, workspace_id, *args, **kwargs):
        if not ai_available(request.workspace.organization):
            raise Http404
        if getattr(request, "limited", False):
            return HttpResponse(
                "That's a lot of AI requests in a row. Wait a minute and try again.",
                status=429,
                content_type="text/plain",
            )
        try:
            return view_func(request, AIContext(workspace=request.workspace, user=request.user), *args, **kwargs)
        except (ValueError, PlanLimitError, AIError) as exc:
            status = getattr(exc, "status_code", 402 if isinstance(exc, PlanLimitError) else 400)
            return HttpResponse(getattr(exc, "message", str(exc)), status=status, content_type="text/plain")

    guarded = ratelimit(key="user", rate=AI_ACTION_RATE, method="POST", block=False)(_wrapped)
    return login_required(require_permission("create_posts")(require_POST(guarded)))


def _selected_accounts(request):
    """The connected accounts of this workspace named in ``selected_accounts``, in that order."""
    from apps.composer.views import _parse_selected_account_ids

    ids = _parse_selected_account_ids(request.POST.get("selected_accounts", ""))
    accounts = {
        str(a.id): a
        for a in SocialAccount.objects.for_workspace(request.workspace.id).filter(
            id__in=ids,
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
    }
    return [accounts[i] for i in ids if i in accounts]


def _result(request, text: str, *, heading: str):
    return render(request, "ai_writer/partials/caption_result.html", {"text": text, "heading": heading})


@_ai_view
def write(request, ctx):
    """Write a caption from a short prompt."""
    text = services.write_caption(ctx, request.POST.get("ai_prompt", ""), _selected_accounts(request))
    return _result(request, text, heading="Suggested caption")


@_ai_view
def improve(request, ctx):
    """Rewrite the current caption one way (shorter, punchier, translate…)."""
    mode = request.POST.get("ai_mode", "")
    text = services.improve_caption(
        ctx,
        request.POST.get("caption", ""),
        mode,
        language=request.POST.get("ai_language", ""),
        accounts=_selected_accounts(request),
    )
    label = services.IMPROVE_MODES.get(mode, ("Rewritten", ""))[0]
    return _result(request, text, heading=f"{label} version")


@_ai_view
def adapt(request, ctx):
    """One caption per selected account, for its per-account override."""
    accounts = _selected_accounts(request)
    adapted = services.adapt_for_networks(ctx, request.POST.get("caption", ""), accounts)
    rows = [
        {
            "account": account,
            "text": adapted[str(account.id)],
            "length": account.caption_wire_length(adapted[str(account.id)]),
        }
        for account in accounts
    ]
    return render(request, "ai_writer/partials/adapt_result.html", {"rows": rows, "adapted": adapted})


@_ai_view
def hashtags(request, ctx):
    """Hashtags for the current caption, as chips to add."""
    tags = services.suggest_hashtags(ctx, request.POST.get("caption", ""))
    return render(request, "ai_writer/partials/hashtags_result.html", {"tags": tags})


@_ai_view
def link(request, ctx):
    """Turn a link into a few post drafts."""
    try:
        count = int(request.POST.get("ai_count", 3))
    except (TypeError, ValueError):
        count = 3
    drafts = services.link_to_posts(
        ctx, request.POST.get("ai_url", ""), count=count, accounts=_selected_accounts(request)
    )
    return render(
        request,
        "ai_writer/partials/link_result.html",
        {"drafts": drafts, "workspace": request.workspace},
    )


@_ai_view
def link_save(request, ctx):
    """Save the drafts from ``link`` to the idea board, or as draft posts."""
    drafts = [d for d in request.POST.getlist("ai_draft") if d.strip()][: services.MAX_LINK_POSTS]
    if not drafts:
        raise ValueError("There are no drafts to save.")
    if request.POST.get("ai_save_as") == "drafts":
        posts = services.save_posts_as_drafts(ctx.workspace, request.user, drafts, _selected_accounts(request))
        body = f"{len(posts)} draft post{'s' if len(posts) != 1 else ''} saved. Find them under Drafts."
    else:
        ideas = services.save_posts_as_ideas(ctx.workspace, request.user, drafts)
        body = f"{len(ideas)} idea{'s' if len(ideas) != 1 else ''} added to your idea board."
    return trigger_response({"showToast": {"tone": "success", "title": "Saved", "body": body}, "ideaChanged": True})


@_ai_view
def thread(request, ctx):
    """Split long text into numbered parts within one account's limit.

    Returns JSON ``{"parts": [...]}`` for the thread composer to lay out.
    """
    from apps.composer.views import _is_valid_uuid

    account_id = request.POST.get("social_account_id", "")
    account = (
        SocialAccount.objects.for_workspace(request.workspace.id).filter(id=account_id).first()
        if _is_valid_uuid(account_id)
        else None
    )
    if account is None:
        raise ValueError("Choose the account the thread is for.")
    parts = services.split_into_thread(ctx, request.POST.get("text", ""), account)
    return JsonResponse({"parts": parts, "char_limit": account.char_limit})


# ---------------------------------------------------------------------------
# Organization settings: bring your own key
# ---------------------------------------------------------------------------


@login_required
@require_org_role(OrgMembership.OrgRole.ADMIN)
@require_http_methods(["GET", "POST"])
def org_settings(request):
    org = request.org
    # The key itself is never needed here (only its hint), so it isn't loaded
    # or decrypted — the page works even if it can no longer be decrypted.
    org_key = OrganizationAIKey.objects.filter(organization=org).defer("api_key").first()

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "remove_key":
            if org_key:
                org_key.delete()
            messages.success(request, "Your organization's AI key was removed.")
            return redirect("ai_writer_org:settings")

        provider = request.POST.get("provider", "anthropic")
        api_key = request.POST.get("api_key", "").strip()
        if provider not in AI_PROVIDERS:
            messages.error(request, "Choose a supported AI provider.")
        elif len(api_key) < 20 or any(ch.isspace() for ch in api_key):
            messages.error(request, "That doesn't look like an API key. Paste the full key from your provider.")
        else:
            OrganizationAIKey.objects.update_or_create(
                organization=org,
                defaults={
                    "provider": provider,
                    "api_key": api_key,
                    "key_hint": api_key[-4:],
                    "updated_by": request.user,
                },
            )
            messages.success(request, "Saved. The AI assistant now uses your organization's key.")
        return redirect("ai_writer_org:settings")

    ent = get_entitlements(org)
    return render(
        request,
        "ai_writer/org_settings.html",
        {
            "organization": org,
            "settings_active": "ai",
            "org_key": org_key,
            "provider_choices": PROVIDER_CHOICES,
            "platform_key_configured": platform_key_configured(),
            "ai_enabled": ai_available(org),
            "daily_limit": ent.max_ai_requests_per_day if billing_enabled() else None,
        },
    )


# ---------------------------------------------------------------------------
# Composer context
# ---------------------------------------------------------------------------

COMPOSER_TABS = [
    ("write", "Write"),
    ("improve", "Improve"),
    ("adapt", "Adapt"),
    ("hashtags", "Hashtags"),
    ("link", "From a link"),
]


def composer_context(organization) -> dict:
    """What the composer template needs to render the AI panel, or hide it."""
    if not ai_available(organization):
        return {"ai_enabled": False}
    return {
        "ai_enabled": True,
        "ai_tabs": COMPOSER_TABS,
        "ai_improve_modes": [(key, label) for key, (label, _instruction) in services.IMPROVE_MODES.items()],
    }
