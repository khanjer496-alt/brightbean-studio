"""Org-level API key management views.

Responsibilities:

1. **List** every ``ApiKey`` in the org with its capabilities, today's usage
   against its budgets, last use and expiry.
2. **Issue** a new key via a two-step HTMX-driven modal: workspace
   dropdown → (cascading) social-account multi-select + permission
   checkboxes scoped to what the issuer can grant in that workspace, plus a
   preset, capabilities, budgets, IP ranges and expiry.
3. **Edit**, **rotate** (a new secret for the same token) and **revoke** a key.

Every mutation re-validates inputs server-side against the same
``services.issue_api_key`` enforcement layer the Phase 1 service tests
exercise — a tampered form post that names a foreign workspace, an
out-of-org account, or an unbacked permission gets the same
``ValueError`` an out-of-process caller would.
"""

from __future__ import annotations

import functools
import re

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import (
    PermissionDenied,
)
from django.core.exceptions import (
    ValidationError as DjangoValidationError,
)
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from apps.api_keys import budgets, services
from apps.api_keys import capabilities as caps
from apps.api_keys.models import ApiKey
from apps.members.models import (
    PERMISSION_KEYS,
    WorkspaceMembership,
    has_org_permission,
)
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

# Permission keys defined in the registry but intentionally hidden from
# API-key issuance until the feature they gate ships. Stored API keys may
# still carry these strings; they just don't appear in the picker.
#
# ``view_analytics`` has now shipped (the ``/api/v1/analytics/*`` REST
# routes and the ``get_*_analytics`` MCP tools gate on it), so it's
# grantable again — the set is empty until the next pre-ship permission
# lands.
_HIDDEN_FROM_ISSUANCE: set[str] = set()

# ---------------------------------------------------------------------------
# Authorization decorator
# ---------------------------------------------------------------------------


def _require_manage_api_keys(view_func):
    """Gate a view on the org-level ``manage_api_keys`` permission.

    Reuses ``request.org_membership`` populated by the existing
    ``apps.members.middleware.RBACMiddleware``, so the URL doesn't have
    to carry an ``org_id`` and the sidebar link can be a plain
    ``{% url 'api_keys:list' %}``. Members lacking the permission get a
    403; users with no org membership at all get the same 403 (so the
    page never accidentally leaks that the URL exists).
    """

    @functools.wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        org_membership = getattr(request, "org_membership", None)
        if not has_org_permission(org_membership, "manage_api_keys"):
            raise PermissionDenied("You need the manage_api_keys org permission to use this page.")
        return view_func(request, *args, **kwargs)

    return login_required(_wrapped)


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@_require_manage_api_keys
def list_keys(request):
    """Render the API-keys list page for the current org.

    Prefetches every relation the list template uses so the page renders
    in a constant number of queries regardless of key count. Status is
    a computed property — keys can be in one of three buckets:
    ``active``, ``revoked``, ``expired`` — so the template can chip them
    consistently.
    """
    org = request.org
    show_all = request.GET.get("show") == "all"
    qs = (
        ApiKey.objects.filter(workspace__organization=org)
        .select_related("workspace", "issued_by")
        .prefetch_related("social_accounts")
        .order_by("-created_at")
    )
    if not show_all:
        qs = qs.filter(revoked_at__isnull=True)
    # Per-request memo (keyed by workspace id) so the Edit modal's grantable
    # permissions + connected accounts are computed once per workspace, not
    # once per key.
    memo: dict = {"grantable": {}, "accounts": {}}
    keys = list(qs)
    usage = budgets.snapshots(k for k in keys if k.revoked_at is None)
    rows = [_row_context(k, user=request.user, memo=memo, usage=usage.get(k.pk)) for k in keys]
    # Surface a "Show N revoked" toggle only when there's actually something
    # behind it — avoids a noisy control on an org that's never revoked a key.
    revoked_count = ApiKey.objects.filter(workspace__organization=org, revoked_at__isnull=False).count()
    # One-time token reveal handed off from ``issue_key`` via the session
    # (Post/Redirect/Get). Pop it so it shows exactly once — a reload of
    # this page finds nothing and the modal stays closed.
    reveal_token = request.session.pop("reveal_token", None)
    reveal_key_name = request.session.pop("reveal_key_name", None)
    context = {
        "settings_active": "api_keys",
        "rows": rows,
        "show_all": show_all,
        "revoked_count": revoked_count,
        # Empty issuance form context — the modal renders inside the
        # same page so we don't need a separate route.
        "issuance": _initial_issuance_context(org, request.user),
        "scope_form": _scope_form_context(None),
        "reveal_token": reveal_token,
        "reveal_key_name": reveal_key_name,
    }
    return render(request, "api_keys/list.html", context)


def _row_context(api_key: ApiKey, *, user, memo: dict, usage: dict | None = None) -> dict:
    """Adapt an ``ApiKey`` row into the dict the list template expects.

    ``memo`` is a per-request cache (``{"grantable": {ws_id: ...},
    "accounts": {ws_id: ...}}``) so the Edit modal's grantable-permission and
    connected-account lookups run once per workspace, not once per key.
    """
    if api_key.revoked_at is not None:
        status = "revoked"
    elif api_key.expires_at and api_key.expires_at <= timezone.now():
        status = "expired"
    else:
        status = "active"

    on_key_accounts = list(api_key.social_accounts.all())

    # Edit-modal context — only active keys get an Edit button, so don't pay
    # the grantable/connected queries for revoked or expired rows.
    editable_permissions: list[tuple[str, str, bool]] = []
    locked_permissions: list[str] = []
    editable_accounts: list[tuple] = []
    if status == "active":
        workspace = api_key.workspace
        grantable = memo["grantable"].get(workspace.id)
        if grantable is None:
            # include_hidden=True so the modal's grantable set matches exactly
            # what ``services.update_api_key`` grants over — no silent drop of a
            # held-but-issuance-hidden permission.
            grantable = _grantable_permissions(user, workspace, include_hidden=True)
            memo["grantable"][workspace.id] = grantable
        connected = memo["accounts"].get(workspace.id)
        if connected is None:
            connected = list(
                SocialAccount.objects.filter(
                    workspace=workspace,
                    connection_status=SocialAccount.ConnectionStatus.CONNECTED,
                ).order_by("platform", "account_name")
            )
            memo["accounts"][workspace.id] = connected

        current = set(api_key.permissions or [])
        grantable_keys = {k for k, _ in grantable}
        editable_permissions = [(perm_key, label, perm_key in current) for perm_key, label in grantable]
        # Perms the key holds that this editor can't grant — shown locked so
        # the editor sees the full scope but can't strip what they don't control.
        locked_permissions = sorted(current - grantable_keys)

        # Account checkboxes = connected-in-workspace ∪ accounts already on the
        # key, so an allowlisted-but-now-disconnected account stays pre-checked
        # rather than silently dropping off on save.
        on_key_ids = {a.id for a in on_key_accounts}
        connected_ids = {sa.id for sa in connected}
        editable_accounts = [(sa, sa.id in on_key_ids) for sa in connected]
        for sa in on_key_accounts:
            if sa.id not in connected_ids:
                editable_accounts.append((sa, True))

    return {
        "id": api_key.id,
        "name": api_key.name,
        "workspace_name": api_key.workspace.name,
        "accounts": on_key_accounts,
        "permissions": list(api_key.permissions or []),
        "last_used_at": api_key.last_used_at,
        "issued_by": api_key.issued_by,
        "created_at": api_key.created_at,
        "expires_at": api_key.expires_at,
        "status": status,
        "limited": api_key.capabilities is not None,
        "capability_labels": (
            [caps.label_for(k) for k in caps.token_capabilities(api_key) or []]
            if api_key.capabilities is not None
            else []
        ),
        "preset": caps.preset(api_key.preset),
        "requires_approval": api_key.requires_approval,
        "ip_ranges": list(api_key.allowed_ip_ranges or []),
        "budgets": _budget_rows(usage),
        "activity_url": f"{reverse('autonomy:activity', kwargs={'workspace_id': api_key.workspace_id})}?key={api_key.id}",
        "scope_form": _scope_form_context(api_key) if status == "active" else None,
        # Edit-modal context (empty for non-active rows).
        "editable_permissions": editable_permissions,  # [(key, label, checked)]
        "locked_permissions": locked_permissions,  # [str]
        "editable_accounts": editable_accounts,  # [(SocialAccount, checked)]
    }


_BUDGET_LABELS = (
    ("requests", "Calls today"),
    ("ai", "AI calls today"),
    ("posts", "Posts scheduled today"),
)

#: Daily budgets: form field, label, and the shorter label the error message uses.
_BUDGET_FIELDS = (
    ("daily_request_budget", "Calls a day"),
    ("daily_ai_budget", "AI calls a day"),
    ("daily_post_budget", "Scheduled posts a day"),
)
_MAX_BUDGET = 1_000_000


def _budget_rows(usage: dict | None) -> list[dict]:
    """Today's spend per budget for the list, with the share used so the bar can warn as a token nears its limit."""
    rows = []
    for kind, label in _BUDGET_LABELS:
        used, limit = (usage or {}).get(kind, (0, None))
        if limit is None and kind != "requests":
            continue
        percent = 100 if limit == 0 else min(100, round(used * 100 / limit)) if limit else 0
        rows.append(
            {
                "label": label,
                "used": used,
                "limit": limit,
                "percent": percent,
                "state": "over" if limit is not None and used >= limit else "near" if percent >= 80 else "ok",
            }
        )
    return rows


def _scope_form_context(api_key: ApiKey | None) -> dict:
    """What the token form shows: presets, capabilities grouped and ticked, budgets, IP ranges, approval mode.

    A new token starts from the Drafting assistant preset, which can only write drafts a person approves.
    A key from before scoped tokens (no capability limit) shows everything ticked, and saving applies the form.
    """
    if api_key is None:
        start = caps.preset("drafting_assistant")
        held = set(start.capabilities)
        selected = start.key
        values = {
            "daily_request_budget": start.daily_requests,
            "daily_ai_budget": start.daily_ai_calls,
            "daily_post_budget": start.daily_posts,
        }
        requires_approval, ip_text, legacy = start.requires_approval, "", False
    else:
        held = set(caps.CAPABILITY_KEYS if api_key.capabilities is None else caps.clean(api_key.capabilities))
        selected = api_key.preset
        values = {name: getattr(api_key, name) for name, _ in _BUDGET_FIELDS}
        requires_approval, ip_text = api_key.requires_approval, "\n".join(api_key.allowed_ip_ranges or [])
        legacy = api_key.capabilities is None
    groups: dict[str, list] = {}
    for cap in caps.CAPABILITIES:
        groups.setdefault(cap.group, []).append({"cap": cap, "checked": cap.key in held})
    return {
        "presets": [
            {
                "preset": p,
                "selected": p.key == selected,
                "capabilities": ",".join(p.capabilities),
                "approval": "1" if p.requires_approval else "0",
            }
            for p in caps.PRESETS
        ],
        "custom_selected": not selected,
        "groups": list(groups.items()),
        "budget_fields": [(name, label, values.get(name)) for name, label in _BUDGET_FIELDS],
        "requires_approval": requires_approval,
        "ip_ranges_text": ip_text,
        "legacy": legacy,
    }


def _scope_from_post(post) -> tuple[dict, list[str]]:
    """The tool-level scope posted by the token form, as ``(service kwargs, errors)``.

    A post without the form's ``scoped`` marker is an older client posting only permissions, and gets no scope
    arguments, so the key stays as it was. The preset is stored only when the capabilities still match it.
    """
    if not post.get("scoped"):
        return {}, []
    errors: list[str] = []
    chosen = caps.clean(post.getlist("capabilities"))
    if not chosen:
        errors.append("Choose at least one capability.")
    preset = caps.preset(post.get("preset", ""))
    kwargs: dict = {
        "capabilities": chosen,
        "preset": preset.key if preset and set(preset.capabilities) == set(chosen) else "",
        "requires_approval": post.get("requires_approval") == "on",
    }
    for name, label in _BUDGET_FIELDS:
        raw = (post.get(name) or "").strip()
        if not raw:
            kwargs[name] = None
        elif not raw.isdigit() or int(raw) > _MAX_BUDGET:
            errors.append(f"{label} must be a whole number up to {_MAX_BUDGET:,}, or empty for no limit.")
        else:
            kwargs[name] = int(raw)
    kwargs["allowed_ip_ranges"] = [part for part in re.split(r"[\s,]+", post.get("allowed_ip_ranges", "")) if part]
    return kwargs, errors


def _permissions_for_scope(posted: list[str], scope: dict, user, workspace) -> list[str]:
    """Posted permissions plus what the chosen capabilities need, limited to what the issuer holds."""
    if "capabilities" not in scope:
        return posted
    grantable = {k for k, _ in _grantable_permissions(user, workspace, include_hidden=True)}
    return sorted(set(posted) | (set(caps.permissions_for(scope["capabilities"])) & grantable))


def _initial_issuance_context(org, user) -> dict:
    """Build the initial state for the issuance modal.

    The workspace dropdown is rendered server-side from the org's
    workspaces; everything downstream (accounts, permissions) loads via
    the HTMX partial on workspace change.
    """
    # The example workspace (apps.onboarding.sample) never gets a key.
    workspaces = Workspace.objects.filter(organization=org, is_archived=False, sample_marker__isnull=True).order_by(
        "name"
    )
    return {"workspaces": workspaces}


# ---------------------------------------------------------------------------
# HTMX partial — workspace → accounts + permissions
# ---------------------------------------------------------------------------


@_require_manage_api_keys
def workspace_options_partial(request):
    """Return a partial with the social-account checkboxes and grantable
    permission catalog for the selected workspace.

    Triggered by ``hx-trigger="change"`` on the workspace ``<select>``.
    The permission set is intersected with what the issuer (the logged-in
    user) actually holds in this workspace — an admin without
    ``publish_directly`` in workspace X cannot tick the
    ``publish_directly`` checkbox for a key in workspace X, even via a
    tampered form post (which ``services.issue_api_key`` re-validates).
    """
    workspace_id = request.GET.get("workspace_id")
    if not workspace_id:
        return HttpResponse("")
    try:
        workspace = Workspace.objects.get(id=workspace_id, organization=request.org, sample_marker__isnull=True)
    except (Workspace.DoesNotExist, ValueError, DjangoValidationError):
        # Django's UUIDField raises ``django.core.exceptions.ValidationError``
        # (not ``ValueError``) when the input doesn't parse as a UUID,
        # so a tampered HTMX trigger like ``workspace_id=not-a-uuid``
        # used to escape this handler and 500. Catch all three exception
        # types and don't differentiate a bad UUID from a foreign-org
        # workspace — the empty response is identical to "no workspace
        # picked", which is what the cascade UI expects.
        return HttpResponse("")

    # Show all connected accounts; the issue endpoint re-checks the
    # account → workspace mapping before persisting.
    accounts = SocialAccount.objects.filter(
        workspace=workspace,
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    ).order_by("platform", "account_name")

    # Permission catalog scoped to what THIS issuer can grant in THIS
    # workspace — see ``_grantable_permissions`` for the rule.
    grantable = _grantable_permissions(request.user, workspace)

    context = {
        "workspace": workspace,
        "accounts": accounts,
        "grantable_permissions": grantable,
    }
    return render(request, "api_keys/_workspace_options.html", context)


def _grantable_permissions(user, workspace, *, include_hidden: bool = False) -> list[tuple[str, str]]:
    """Return ``[(perm_key, label), ...]`` of permissions the user can
    grant in ``workspace``.

    ``include_hidden`` keeps issuance-hidden permissions in the list. The
    issuance picker leaves it ``False`` (don't offer not-yet-shipped
    features when minting a key); the *edit* modal sets it ``True`` so the
    modal's grantable set matches exactly what ``services.update_api_key``
    grants over (``held ∩ PERMISSION_KEYS``) — otherwise a held permission
    that's hidden-from-issuance could be silently stripped on save.

    The rule mirrors what ``services.issue_api_key`` will enforce
    server-side: an issuer can only grant a permission they themselves
    hold via their workspace membership. This way the UI doesn't offer
    permissions the user can't actually issue.

    Labels are derived from the permission key — ``PERMISSION_KEYS`` is
    a flat list of slugs in ``apps.members.models``, with no
    human-readable label dict alongside it. Titlecasing the underscored
    slug ("create_posts" → "Create posts") gives a friendly-enough
    label for the modal without us having to maintain a parallel dict.
    """
    try:
        membership = WorkspaceMembership.objects.select_related("custom_role").get(user=user, workspace=workspace)
    except WorkspaceMembership.DoesNotExist:
        return []
    held = {k for k, v in membership.effective_permissions.items() if v}
    return [
        (k, k.replace("_", " ").capitalize()) for k in PERMISSION_KEYS if k in held and k not in _HIDDEN_FROM_ISSUANCE
    ]


# ---------------------------------------------------------------------------
# Issue
# ---------------------------------------------------------------------------


def _parse_expires_at(expires_at_str: str):
    """Parse the optional ``expires_at`` form value.

    Accepts either a full ISO timestamp or a date-only value from
    ``<input type="date">`` (treated as end-of-day in the current timezone).
    Returns ``(datetime | None, error | None)`` — an empty string is "no
    expiry" (``(None, None)``), an unparseable value yields an error string.
    Shared by ``issue_key`` and ``edit_key`` so the two endpoints agree.
    """
    if not expires_at_str:
        return None, None

    from datetime import datetime, time

    from django.utils.dateparse import parse_date, parse_datetime

    try:
        dt = parse_datetime(expires_at_str)
        date_only = parse_date(expires_at_str)
    except ValueError:
        return None, "Could not parse expires_at."

    if dt is None and date_only is None:
        return None, "Could not parse expires_at."

    if date_only is not None:
        # Date-only value (e.g. ``<input type="date">``) → end of that day, so
        # a key set to expire on a date stays valid through it. ``parse_datetime``
        # would otherwise hand back a naive *midnight*, expiring it a day early.
        dt = datetime.combine(date_only, time.max)

    assert dt is not None
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt, None


def _resolve_account_allowlist(account_ids, workspace) -> tuple[list, str | None]:
    """Resolve posted ``social_account_ids`` to SocialAccounts in ``workspace``.

    Returns ``(accounts, error | None)``. A tampered POST can carry a non-UUID
    ``social_account_ids`` value; the ``UUIDField`` coercion then raises
    ``ValueError``/``ValidationError`` when the queryset is evaluated — the same
    failure the ``workspace_id`` path guards against (see
    ``workspace_options_partial``). Catch it and treat it identically to
    "accounts not in this workspace" so a malformed post yields a clean
    validation error + redirect rather than a 500.

    Shared by ``issue_key`` and ``edit_key`` so the two endpoints agree.
    """
    try:
        accounts = list(SocialAccount.objects.filter(id__in=account_ids, workspace=workspace))
    except (ValueError, DjangoValidationError):
        return [], "Some selected accounts do not belong to that workspace."
    if len(accounts) != len(set(account_ids)):
        return accounts, "Some selected accounts do not belong to that workspace."
    return accounts, None


@_require_manage_api_keys
@require_http_methods(["POST"])
def issue_key(request):
    """Issue a new key, then render the one-time-reveal modal.

    The plaintext token is shown to the user **once** in this response;
    we never store it. Subsequent requests can see the key in the list,
    but never see the token.
    """
    name = (request.POST.get("name") or "").strip()
    workspace_id = request.POST.get("workspace_id") or ""
    account_ids = request.POST.getlist("social_account_ids")
    permission_keys = request.POST.getlist("permissions")
    expires_at_str = (request.POST.get("expires_at") or "").strip()
    scope, scope_errors = _scope_from_post(request.POST)

    errors: list[str] = list(scope_errors)
    if not name:
        errors.append("Name is required.")
    if not workspace_id:
        errors.append("Workspace is required.")
    if not account_ids:
        errors.append("Select at least one connected account.")

    workspace = None
    if workspace_id and not errors:
        try:
            workspace = Workspace.objects.get(id=workspace_id, organization=request.org, sample_marker__isnull=True)
        except (Workspace.DoesNotExist, ValueError, DjangoValidationError):
            # ``ValidationError`` covers the malformed-UUID path; see the
            # corresponding catch in ``workspace_options_partial`` for
            # the full rationale.
            errors.append("Selected workspace is not in this organisation.")

    accounts: list[SocialAccount] = []
    if workspace is not None:
        accounts, acct_err = _resolve_account_allowlist(account_ids, workspace)
        if acct_err:
            errors.append(acct_err)

    expires_at, exp_err = _parse_expires_at(expires_at_str)
    if exp_err:
        errors.append(exp_err)

    if errors:
        for e in errors:
            messages.error(request, e)
        return redirect("api_keys:list")

    try:
        issued = services.issue_api_key(
            workspace=workspace,
            social_accounts=accounts,
            issued_by=request.user,
            name=name,
            permissions=_permissions_for_scope(permission_keys, scope, request.user, workspace),
            expires_at=expires_at,
            **scope,
        )
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect("api_keys:list")

    # Post/Redirect/Get. Stash the one-time token in the session and
    # redirect to the list, which pops it and renders the reveal modal on
    # the way through. Rendering the list *directly* from this POST (the
    # previous behaviour) left the browser sitting on the ``/issue/``
    # endpoint, so a refresh re-submitted the form and minted a brand-new
    # key — and re-popped the reveal modal — every time.
    #
    # The token never rides the URL/Location header: sessions here are
    # DB-backed (``SESSION_ENGINE = ...sessions.backends.db``), so only the
    # opaque session id is in the cookie. ``list_keys`` pops the value, so
    # it's still shown exactly once and a later reload sees nothing.
    request.session["reveal_token"] = issued.plaintext_token
    request.session["reveal_key_name"] = issued.api_key.name
    return redirect("api_keys:list")


# ---------------------------------------------------------------------------
# Revoke
# ---------------------------------------------------------------------------


@_require_manage_api_keys
@require_http_methods(["POST"])
def revoke_key(request, key_id):
    """Soft-revoke a key.

    The actual delete is deferred to the background sweep (or a future
    admin action) — we only set ``revoked_at``. ``verify_token`` reads the
    row fresh on every request, so every worker rejects the key from its
    very next call.
    """
    key = get_object_or_404(
        ApiKey.objects.select_related("workspace", "workspace__organization"),
        id=key_id,
    )
    # Defence in depth — never revoke a key outside the current org.
    if key.workspace.organization_id != request.org.id:
        raise Http404()
    if key.revoked_at is None:
        services.revoke_api_key(key)
        messages.success(request, f"Revoked key “{key.name}”.")
    else:
        messages.info(request, f"Key “{key.name}” was already revoked.")
    return redirect("api_keys:list")


# ---------------------------------------------------------------------------
# Edit
# ---------------------------------------------------------------------------


@_require_manage_api_keys
@require_http_methods(["POST"])
def edit_key(request, key_id):
    """Update an existing key's permissions, account allowlist, and expiry.

    A plain POST + redirect (PRG), like ``issue_key``/``revoke_key`` — not
    HTMX — so we sidestep the teleport/HTMX interplay and reuse the flash
    messages. Org-scoping mirrors ``revoke_key`` (defence in depth), and the
    heavy lifting + re-validation lives in ``services.update_api_key``.
    """
    key = get_object_or_404(
        ApiKey.objects.select_related("workspace", "workspace__organization"),
        id=key_id,
    )
    # Defence in depth — never edit a key outside the current org.
    if key.workspace.organization_id != request.org.id:
        raise Http404()

    # Only active keys are editable (mirror the row's Edit button gating).
    if not key.is_active:
        messages.info(request, f"Key “{key.name}” is not active; it can't be edited.")
        return redirect("api_keys:list")

    account_ids = request.POST.getlist("social_account_ids")
    permission_keys = request.POST.getlist("permissions")
    expires_at, exp_err = _parse_expires_at((request.POST.get("expires_at") or "").strip())
    scope, scope_errors = _scope_from_post(request.POST)

    errors: list[str] = list(scope_errors)
    if not account_ids:
        errors.append("Select at least one connected account.")
    accounts, acct_err = _resolve_account_allowlist(account_ids, key.workspace)
    if acct_err:
        errors.append(acct_err)
    if exp_err:
        errors.append(exp_err)

    if errors:
        for e in errors:
            messages.error(request, e)
        return redirect("api_keys:list")

    try:
        services.update_api_key(
            key,
            editor=request.user,
            permissions=_permissions_for_scope(permission_keys, scope, request.user, key.workspace),
            social_accounts=accounts,
            expires_at=expires_at,
            **scope,
        )
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect("api_keys:list")

    messages.success(request, f"Updated “{key.name}”.")
    return redirect("api_keys:list")


# ---------------------------------------------------------------------------
# Rotate
# ---------------------------------------------------------------------------


@_require_manage_api_keys
@require_http_methods(["POST"])
def rotate_key(request, key_id):
    """Give a key a new secret and show it once; the old secret stops working on the next request.

    Scope, budgets and history stay with the key, so an agent only needs its new token pasted in.
    """
    key = get_object_or_404(
        ApiKey.objects.select_related("workspace", "workspace__organization"),
        id=key_id,
    )
    if key.workspace.organization_id != request.org.id:
        raise Http404()
    try:
        issued = services.rotate_api_key(key, editor=request.user)
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect("api_keys:list")
    request.session["reveal_token"] = issued.plaintext_token
    request.session["reveal_key_name"] = key.name
    return redirect("api_keys:list")
