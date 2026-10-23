"""Grid preview pages: ``/workspace/<id>/calendar/grid/…``.

Anyone in the workspace can look at a grid. Changing it follows the calendar's
rules for moving a post in time (see :func:`apps.calendar.services.reschedule_denial`),
checked again on the server for every swap. Swaps and moves answer an HTMX
request with the refreshed grid and a message, and a plain form post with a
redirect, so the keyboard buttons work without script.
"""

from __future__ import annotations

import uuid
import zoneinfo

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from apps.social_accounts.models import SocialAccount

from . import grid
from .views import _get_workspace


def _account(workspace, account_id) -> SocialAccount:
    account = get_object_or_404(SocialAccount, id=account_id, workspace=workspace)
    if not grid.supports_grid(account):
        raise Http404("This account has no grid preview.")
    return account


def _membership(request, workspace):
    # Resolved for this workspace by the RBAC middleware from the URL.
    return request.workspace_membership


def _context(request, workspace, account, *, notice="", error="", focus_id=None) -> dict:
    view = grid.build_grid(account, request.user, _membership(request, workspace))
    return {
        "workspace": workspace,
        "account": account,
        "grid": view,
        "accounts": list(grid.grid_accounts(workspace)),
        "timezone_name": workspace.effective_timezone or "UTC",
        "notice": notice,
        "error": error,
        "focus_id": focus_id,
        "swap_url": reverse("calendar:grid_swap", kwargs={"workspace_id": workspace.id, "account_id": account.id}),
        "profile_name": account.account_name or account.account_handle,
    }


@login_required
@require_GET
def grid_index(request, workspace_id):
    workspace = _get_workspace(request, workspace_id)
    accounts = list(grid.grid_accounts(workspace))
    if len(accounts) == 1:
        return redirect("calendar:account_grid", workspace_id=workspace.id, account_id=accounts[0].id)
    return render(request, "calendar/grid_index.html", {"workspace": workspace, "accounts": accounts})


@login_required
@require_GET
def account_grid(request, workspace_id, account_id):
    workspace = _get_workspace(request, workspace_id)
    account = _account(workspace, account_id)
    return render(request, "calendar/grid.html", _context(request, workspace, account))


def _answer(request, workspace, account, *, notice="", error="", focus_id=None):
    if getattr(request, "htmx", False):
        context = _context(request, workspace, account, notice=notice, error=error, focus_id=focus_id)
        return render(request, "calendar/partials/_grid.html", context)
    (messages.error if error else messages.success)(request, error or notice)
    return redirect("calendar:account_grid", workspace_id=workspace.id, account_id=account.id)


def _describe(pp, workspace) -> str:
    tz = zoneinfo.ZoneInfo(workspace.effective_timezone or "UTC")
    label = (pp.post.caption or pp.post.title or "Untitled post").strip()[:40]
    return f"{label} ({pp.scheduled_at.astimezone(tz):%b %-d, %H:%M})"


def _swapped(request, workspace, account, first, second, focus_id):
    notice = f"Swapped times: {_describe(first, workspace)} and {_describe(second, workspace)}."
    return _answer(request, workspace, account, notice=notice, focus_id=focus_id)


def _parse(value):
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


@login_required
@require_POST
def grid_swap(request, workspace_id, account_id):
    workspace = _get_workspace(request, workspace_id)
    account = _account(workspace, account_id)
    first, second = _parse(request.POST.get("first")), _parse(request.POST.get("second"))
    if first is None or second is None:
        return _answer(request, workspace, account, error="Pick two posts to swap.")
    try:
        a, b = grid.swap_times(account, first, second, request.user, _membership(request, workspace))
    except grid.GridError as exc:
        return _answer(request, workspace, account, error=exc.message)
    return _swapped(request, workspace, account, a, b, focus_id=first)


@login_required
@require_POST
def grid_move(request, workspace_id, account_id, platform_post_id):
    workspace = _get_workspace(request, workspace_id)
    account = _account(workspace, account_id)
    try:
        a, b = grid.move(
            account, platform_post_id, request.POST.get("direction", ""), request.user, _membership(request, workspace)
        )
    except grid.GridError as exc:
        return _answer(request, workspace, account, error=exc.message, focus_id=platform_post_id)
    return _swapped(request, workspace, account, a, b, focus_id=platform_post_id)
