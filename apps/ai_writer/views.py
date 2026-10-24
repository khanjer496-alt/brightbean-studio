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

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods, require_POST
from django_ratelimit.decorators import ratelimit

from apps.billing.entitlements import PlanLimitError
from apps.common.htmx import trigger_response
from apps.members.decorators import require_org_role, require_permission
from apps.members.models import OrgMembership
from apps.social_accounts.models import SocialAccount

from . import services
from .client import (
    AI_PROVIDERS,
    AIError,
    AIResponseError,
    ai_available,
    check_connection,
    platform_key_for_customers,
    validate_base_url,
)
from .models import OrganizationAIKey
from .services import AIContext

logger = logging.getLogger(__name__)

# Per-user burst cap on the composer actions. The plan's daily allowance is the
# real budget; this only stops a stuck button or script from spending it in
# one go.
AI_ACTION_RATE = "20/m"


def ai_action(permission):
    """Shared guard for the AI actions: login, ``permission``, POST, rate, AI on.

    The wrapped view gets an ``AIContext`` instead of ``workspace_id``, and any
    ``ValueError`` / ``PlanLimitError`` / ``AIError`` it raises becomes a
    plain-text error response the panel shows inline.
    """

    def decorator(view_func):
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
        return login_required(require_permission(permission)(require_POST(guarded)))

    return decorator


_ai_view = ai_action("create_posts")


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


def _provider_options() -> list[dict]:
    """What the settings form needs to know about each provider."""
    options = []
    for name, cls in AI_PROVIDERS.items():
        presets = [
            {
                "key": key,
                "label": label,
                # A local server's address only works where private addresses are allowed.
                "base_url": base_url if not local or settings.AI_ALLOW_PRIVATE_BASE_URLS else "https://",
                "local": local,
            }
            for key, label, base_url, local in cls.presets
        ]
        options.append(
            {
                "name": name,
                "label": cls.label,
                "needs_base_url": cls.needs_base_url,
                "default_model": cls.default_model,
                "key_placeholder": cls.key_placeholder,
                "presets": presets,
            }
        )
    return options


def _connection_from_form(request, org_key):
    """The connection the settings form describes, ready to try or save.

    Returns ``(provider_instance, fields)``; raises ``AIError`` (or ``ValueError``
    with a message for the admin) when the form isn't usable. A blank key keeps
    the stored one when the provider hasn't changed.
    """
    name = request.POST.get("provider", "")
    provider_cls = AI_PROVIDERS.get(name)
    if provider_cls is None:
        raise ValueError("Choose a supported AI provider.")
    api_key = request.POST.get("api_key", "").strip()
    if not api_key and org_key and org_key.provider == name:
        try:
            api_key = OrganizationAIKey.objects.get(pk=org_key.pk).api_key
        except ValueError:
            raise ValueError("The stored key can't be read any more. Paste the key again.") from None
    if len(api_key) < provider_cls.min_key_length or any(ch.isspace() for ch in api_key):
        raise ValueError("That doesn't look like an API key. Paste the full key from your provider.")
    base_url = validate_base_url(request.POST.get("base_url", "")) if provider_cls.needs_base_url else ""
    model = request.POST.get("model", "").strip()
    if len(model) > 100 or any(ch.isspace() for ch in model):
        raise ValueError("That doesn't look like a model name. Copy it exactly from your provider.")
    if provider_cls.needs_base_url and not model:
        raise ValueError("Enter the model name your AI service should use.")
    fields = {"provider": name, "api_key": api_key, "key_hint": api_key[-4:], "base_url": base_url, "model": model}
    return provider_cls(api_key=api_key, model=model or provider_cls.default_model, base_url=base_url), fields


@login_required
@require_org_role(OrgMembership.OrgRole.ADMIN)
@require_POST
def connection_test(request):
    """Try the connection the form describes with one tiny request, without saving it."""
    org_key = OrganizationAIKey.objects.filter(organization=request.org).defer("api_key").first()
    try:
        provider, _fields = _connection_from_form(request, org_key)
        check_connection(provider)
    except (ValueError, AIError) as exc:
        return render(request, "ai_writer/partials/connection_result.html", {"ok": False, "message": _message(exc)})
    return render(
        request,
        "ai_writer/partials/connection_result.html",
        {"ok": True, "message": f"Connected. {provider.label} answered using the model {provider.model}."},
    )


def _message(exc: Exception) -> str:
    return getattr(exc, "message", None) or str(exc)


@login_required
@require_org_role(OrgMembership.OrgRole.ADMIN)
@require_http_methods(["GET", "POST"])
def org_settings(request):
    org = request.org
    # The key itself is never needed here (only its hint), so it isn't loaded
    # or decrypted — the page works even if it can no longer be decrypted.
    org_key = OrganizationAIKey.objects.filter(organization=org).defer("api_key").first()
    form = {
        "provider": org_key.provider if org_key else "anthropic",
        "base_url": org_key.base_url if org_key else "",
        "model": org_key.model if org_key else "",
    }
    save_error = ""

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "remove_key":
            if org_key:
                org_key.delete()
            messages.success(request, "Your organization's AI key was removed.")
            return redirect("ai_writer_org:settings")

        form = {key: request.POST.get(key, "").strip() for key in ("provider", "base_url", "model")}
        try:
            provider, fields = _connection_from_form(request, org_key)
            if not request.POST.get("save_anyway"):
                check_connection(provider)
        except AIResponseError as exc:
            # It answered, just not in a shape the check expected (common with small models):
            # let the admin decide rather than refusing to save.
            save_error = f"{_message(exc)} Tick “Save anyway” to keep these settings, or pick another model."
        except (ValueError, AIError) as exc:
            save_error = f"Not saved. {_message(exc)}"
        else:
            OrganizationAIKey.objects.update_or_create(
                organization=org, defaults={**fields, "updated_by": request.user}
            )
            messages.success(request, "Saved. The AI assistant now uses your organization's connection.")
            return redirect("ai_writer_org:settings")

    return render(
        request,
        "ai_writer/org_settings.html",
        {
            "organization": org,
            "settings_active": "ai",
            "org_key": org_key,
            "form": form,
            "save_error": save_error,
            "provider_options": _provider_options(),
            "chatgpt_signin_enabled": settings.AI_CHATGPT_SIGNIN_ENABLED,
            "platform_key_configured": platform_key_for_customers(),
            "ai_enabled": ai_available(org),
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
