"""Best time to post: the heatmap, "Suggest slots" and the composer's "Best times" chips."""

import re
import uuid

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.utils.dateformat import time_format
from django.views.decorators.http import require_POST

from apps.analytics.best_times import MIN_CELL_POSTS, MIN_POSTS, BestTime, get_best_times, upcoming
from apps.members.decorators import require_permission
from apps.social_accounts.models import SocialAccount

from . import services
from .models import PostingSlot
from .views import _get_workspace, _slots_updated_response

WEEKDAYS = [label for _, label in PostingSlot.DayOfWeek.choices]
CHIP_ACCOUNTS_MAX = 5
CHIPS_PER_ACCOUNT = 3
_SLOT_TOKEN = re.compile(r"^([0-6])-(\d|1\d|2[0-3])$")


def _uuids(values):
    """The valid UUIDs among ``values``; malformed ones are dropped."""
    found = []
    for value in values:
        try:
            found.append(uuid.UUID(str(value)))
        except ValueError:
            continue
    return found


def _account(workspace, account_id):
    ids = _uuids([account_id])
    return get_object_or_404(
        SocialAccount,
        id=ids[0] if ids else None,
        workspace=workspace,
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )


def _count(request):
    try:
        return max(
            1, min(int(request.GET.get("count") or request.POST.get("count") or 0), services.SUGGESTED_SLOTS_MAX)
        )
    except (TypeError, ValueError):
        return services.SUGGESTED_SLOTS_DEFAULT


def _hour_label(hour):
    return f"{hour % 12 or 12}:00 {'AM' if hour < 12 else 'PM'}"


def _cell_tip(cell, is_default):
    """The sentence a heatmap cell reads out on hover, focus and tap, and to screen readers."""
    when = f"{WEEKDAYS[cell['weekday']]} {_hour_label(cell['hour'])}"
    if not cell["level"]:
        return f"{when}: no data"
    if is_default:
        return f"{when}: industry default, score {cell['score']} of 100"
    posts = f"{cell['posts']} post{'s' if cell['posts'] != 1 else ''}"
    if cell["low_confidence"]:
        return f"{when}: score {cell['score']} of 100, low confidence (only {posts})"
    return f"{when}: score {cell['score']} of 100, from {posts}"


def _panel_context(request, workspace, account):
    best = get_best_times(account)
    top = best.top(services.SUGGESTED_SLOTS_DEFAULT)
    rows = best.rows()
    top_keys = {(t.weekday, t.hour) for t in top}
    for row in rows:
        row["name"] = WEEKDAYS[row["weekday"]]
        for cell in row["cells"]:
            cell["tip"] = _cell_tip(cell, best.is_default)
            cell["suggested"] = (cell["weekday"], cell["hour"]) in top_keys
    return {
        "workspace": workspace,
        "workspace_id": workspace.id,
        "account": account,
        "best": best,
        "rows": rows,
        "hours": [
            {"hour": h, "label": f"{h % 12 or 12}{'a' if h < 12 else 'p'}", "show": h % 3 == 0} for h in range(24)
        ],
        "top": [
            {"day": WEEKDAYS[t.weekday], "time": _hour_label(t.hour), "score": round(t.score), "posts": t.posts}
            for t in top
        ],
        "can_edit_slots": request.workspace_membership.effective_permissions.get("manage_social_accounts", False),
        "suggest_default": services.SUGGESTED_SLOTS_DEFAULT,
        "min_posts": MIN_POSTS,
        "min_cell_posts": MIN_CELL_POSTS,
        "has_low_confidence": any(c["low_confidence"] for row in rows for c in row["cells"]),
    }


@login_required
def best_times_panel(request, workspace_id):
    """The account's best-time heatmap with its "Suggest slots" control (HTMX partial)."""
    workspace = _get_workspace(request, workspace_id)
    account = _account(workspace, request.GET.get("social_account_id"))
    return render(request, "calendar/partials/_best_times_panel.html", _panel_context(request, workspace, account))


@login_required
@require_permission("manage_social_accounts")
def suggest_slots_preview(request, workspace_id):
    """Preview what applying the suggested slots would change, before anything is written."""
    workspace = _get_workspace(request, workspace_id)
    account = _account(workspace, request.GET.get("social_account_id"))
    mode = request.GET.get("mode", "replace")
    if mode not in services.SLOT_MODES:
        mode = "replace"
    best, suggestions = services.suggest_slots(account, _count(request))
    to_add, to_remove, kept = services.plan_slot_changes(account, suggestions, mode)
    return render(
        request,
        "calendar/partials/_suggested_slots.html",
        {
            "workspace_id": workspace.id,
            "account": account,
            "best": best,
            "suggestions": suggestions,
            "labels": [f"{WEEKDAYS[s.weekday][:3]} {_hour_label(s.hour)}" for s in suggestions],
            "tokens": [f"{s.weekday}-{s.hour}" for s in suggestions],
            "mode": mode,
            "counts": range(3, services.SUGGESTED_SLOTS_MAX + 1),
            "to_add": [f"{WEEKDAYS[day][:3]} {time_format(at, 'g:i A')}" for day, at in to_add],
            "to_remove": [f"{WEEKDAYS[slot.day_of_week][:3]} {time_format(slot.time, 'g:i A')}" for slot in to_remove],
            "kept_count": len(kept),
            "count": len(suggestions),
        },
    )


def _suggestions_from_tokens(tokens):
    picked = []
    for token in tokens[: services.SUGGESTED_SLOTS_MAX]:
        match = _SLOT_TOKEN.match(token)
        if match:
            picked.append(BestTime(int(match.group(1)), int(match.group(2)), 0, 0))
    return picked


@login_required
@require_POST
@require_permission("manage_social_accounts")
def suggest_slots_apply(request, workspace_id):
    """Write the previewed slots, replacing or adding to the account's posting slots."""
    workspace = _get_workspace(request, workspace_id)
    account = _account(workspace, request.POST.get("social_account_id"))
    mode = request.POST.get("mode", "replace")
    suggestions = _suggestions_from_tokens(request.POST.getlist("slots"))
    try:
        services.apply_suggested_slots(account, suggestions, mode)
    except ValueError as exc:
        return HttpResponse(str(exc), status=400)
    return _slots_updated_response(account.id)


@login_required
def best_time_chips(request, workspace_id):
    """ "Best times" chips for the composer's schedule picker, for the selected accounts."""
    workspace = _get_workspace(request, workspace_id)
    ids = _uuids(request.GET.get("selected_accounts", "").split(","))[:CHIP_ACCOUNTS_MAX]
    accounts = list(
        SocialAccount.objects.filter(
            id__in=ids, workspace=workspace, connection_status=SocialAccount.ConnectionStatus.CONNECTED
        ).select_related("workspace__organization")
    )
    groups = []
    for account in accounts:
        best = get_best_times(account)
        chips = [
            {
                "date": when.strftime("%Y-%m-%d"),
                "time": when.strftime("%H:%M"),
                "label": f"{WEEKDAYS[slot.weekday][:3]} {time_format(when, 'g:i A')}",
                "day": f"{when:%b} {when.day}",
            }
            for when, slot in upcoming(best, CHIPS_PER_ACCOUNT)
        ]
        groups.append({"account": account, "best": best, "chips": chips})
    return render(
        request,
        "calendar/partials/_best_time_chips.html",
        {"groups": groups, "multiple": len(groups) > 1, "workspace": workspace},
    )
