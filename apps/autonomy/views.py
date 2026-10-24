"""Workspace pages: the Activity trail with its "Going out soon" tray, and the autonomy settings."""

import datetime as dt
import uuid
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.api_keys.models import ApiKey
from apps.approvals.views import _word_diff
from apps.calendar.views import _get_workspace
from apps.members.decorators import require_permission
from apps.members.models import WorkspaceMembership
from apps.social_accounts.models import SocialAccount

from . import configure, release
from .actors import human
from .models import ActionType, ActorType, AgentAction, Decision, Level, RecycleLevel
from .policy import ACTION_EXAMPLES, LEVEL_DESCRIPTIONS, get_override, get_policy

PAGE_SIZE = 50
TRAY_LIMIT = 50

# The pages the dial applies to, in the order the settings page lists them.
DIAL_ROWS = (
    (ActionType.DRAFT_CONTENT, "draft_level", Level),
    (ActionType.PUBLISH_POSTS, "publish_level", Level),
    (ActionType.REPLY, "reply_level", Level),
    (ActionType.RECYCLE_EVERGREEN, "recycle_level", RecycleLevel),
)


def _perms(request) -> dict:
    membership = request.workspace_membership
    return membership.effective_permissions if membership else {}


def _require_view(request) -> None:
    """Team members can read the trail; clients see only the portal's subset."""
    membership = request.workspace_membership
    if membership is None or membership.workspace_role == WorkspaceMembership.WorkspaceRole.CLIENT:
        raise PermissionDenied("You do not have permission to view this workspace's activity.")


def _uuid_or_none(value):
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


def _date_or_none(value):
    try:
        return dt.date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _action_or_404(workspace, action_id):
    return get_object_or_404(
        AgentAction.objects.select_related(
            "social_account",
            "post",
            "platform_post__social_account",
            "reply__inbox_message",
            "reviewed_by",
            "undone_by",
        ),
        id=action_id,
        workspace=workspace,
    )


# ---------------------------------------------------------------------------
# Activity
# ---------------------------------------------------------------------------


def _filtered(request, workspace):
    """The audit trail narrowed by the filters in the query string, and the filter values chosen."""
    chosen = {
        "actor": request.GET.get("actor", ""),
        "decision": request.GET.get("decision", ""),
        "action": request.GET.get("action", ""),
        "account": request.GET.get("account", ""),
        "date_from": request.GET.get("date_from", ""),
        "date_to": request.GET.get("date_to", ""),
        "key": request.GET.get("key", ""),
    }
    qs = AgentAction.objects.filter(workspace=workspace).select_related("social_account")
    # One agent token's trail, linked from the API keys page.
    key_id = _uuid_or_none(chosen["key"])
    if key_id:
        qs = qs.filter(api_key_id=key_id)
    if chosen["actor"] in ActorType.values:
        qs = qs.filter(actor_type=chosen["actor"])
    if chosen["decision"] in Decision.values:
        qs = qs.filter(decision=chosen["decision"])
    if chosen["action"] in ActionType.values:
        qs = qs.filter(action_type=chosen["action"])
    account_id = _uuid_or_none(chosen["account"])
    if account_id:
        qs = qs.filter(social_account_id=account_id)
    date_from, date_to = _date_or_none(chosen["date_from"]), _date_or_none(chosen["date_to"])
    if date_from:
        qs = qs.filter(created_at__date__gte=date_from)
    if date_to:
        qs = qs.filter(created_at__date__lte=date_to)
    return qs, chosen


def _tray_context(workspace, request):
    return {
        "workspace": workspace,
        "tray": list(release.going_out_soon(workspace, limit=TRAY_LIMIT)),
        "can_undo": _perms(request).get("create_posts", False),
        "can_release": _perms(request).get("publish_directly", False),
        "now": timezone.now(),
    }


@login_required
@require_GET
def activity(request, workspace_id):
    workspace = _get_workspace(request, workspace_id)
    _require_view(request)
    qs, chosen = _filtered(request, workspace)
    page = Paginator(qs, PAGE_SIZE).get_page(request.GET.get("page"))
    filter_params = urlencode({k: v for k, v in chosen.items() if v})
    return render(
        request,
        "autonomy/activity.html",
        {
            "workspace": workspace,
            "page": page,
            "chosen": chosen,
            "filter_params": filter_params,
            "accounts": SocialAccount.objects.filter(workspace=workspace).order_by("account_name"),
            "actor_types": ActorType.choices,
            "decisions": Decision.choices,
            "action_types": ActionType.choices,
            "policy": get_policy(workspace),
            "key_filter": ApiKey.objects.filter(id=_uuid_or_none(chosen["key"]), workspace=workspace).first(),
            **_tray_context(workspace, request),
        },
    )


@login_required
@require_GET
def tray(request, workspace_id):
    workspace = _get_workspace(request, workspace_id)
    _require_view(request)
    return render(request, "autonomy/partials/tray.html", _tray_context(workspace, request))


@login_required
@require_GET
def action_detail(request, workspace_id, action_id):
    workspace = _get_workspace(request, workspace_id)
    _require_view(request)
    action = _action_or_404(workspace, action_id)
    perms = _perms(request)
    return render(
        request,
        "autonomy/action_detail.html",
        {
            "workspace": workspace,
            "action": action,
            "diff": _word_diff(action.source_text, action.final_text)
            if action.source_text != action.final_text
            else [],
            "failed_checks": [r for r in action.guardrail_results if not r.get("passed")],
            "passed_checks": [r for r in action.guardrail_results if r.get("passed")],
            "can_undo": perms.get("create_posts", False),
            "can_release": perms.get("publish_directly", False),
            "can_review": perms.get("approve_posts", False),
            "can_delete": perms.get("publish_directly", False) and release.can_delete_from_platform(action),
            "now": timezone.now(),
        },
    )


_ACTIONS = {
    "undo": ("create_posts", release.undo, "Undone. Nothing was sent."),
    "hold": ("create_posts", release.hold, "On hold. It will not go out until you release it."),
    "release": ("publish_directly", release.release_hold, "Released. It will go out shortly."),
    "dismiss": ("approve_posts", release.dismiss_held, "Dismissed."),
}


@login_required
@require_POST
def action_run(request, workspace_id, action_id, verb):
    """Undo, hold, release, approve, dismiss or delete-from-platform one entry."""
    workspace = _get_workspace(request, workspace_id)
    _require_view(request)
    action = _action_or_404(workspace, action_id)
    perms = _perms(request)
    try:
        if verb == "approve":
            if not (perms.get("approve_posts") and perms.get("publish_directly")):
                raise PermissionDenied("Approving a held item needs approval and publishing permission.")
            release.approve_held(action, request.user)
            messages.success(request, "Approved. It is scheduled.")
        elif verb == "delete":
            if not perms.get("publish_directly"):
                raise PermissionDenied("Deleting from the platform needs publishing permission.")
            release.delete_from_platform(action, request.user)
            messages.success(request, "Deleted from the platform.")
        elif verb in _ACTIONS:
            permission, function, success = _ACTIONS[verb]
            if not perms.get(permission):
                raise PermissionDenied(f"Permission denied: {permission}")
            function(action, request.user)
            messages.success(request, success)
        else:
            raise Http404
    except release.ReleaseError as exc:
        messages.error(request, str(exc))
    if request.htmx and request.POST.get("from") == "tray":
        return render(request, "autonomy/partials/tray.html", _tray_context(workspace, request))
    return redirect("autonomy:action_detail", workspace_id=workspace.id, action_id=action.id)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def _settings_context(request, workspace):
    policy = get_policy(workspace)
    accounts = list(SocialAccount.objects.filter(workspace=workspace).order_by("account_name", "id"))
    rows = []
    for action_type, field, choices in DIAL_ROWS:
        rows.append(
            {
                "action": action_type,
                "label": ActionType(action_type).label,
                "field": field,
                "value": getattr(policy, field),
                "example": ACTION_EXAMPLES[action_type],
                "options": [
                    {"value": value, "label": label, "help": LEVEL_DESCRIPTIONS[action_type].get(value, "")}
                    for value, label in choices.choices
                ],
            }
        )
    override_rows = []
    for account in accounts:
        override = get_override(account)
        override_rows.append(
            {
                "account": account,
                "fields": [
                    {
                        "name": f"{account.id}__{field}",
                        "label": ActionType(action_type).label,
                        "value": getattr(override, field, "") if override else "",
                        "options": choices.choices,
                    }
                    for action_type, field, choices in DIAL_ROWS
                ],
                "cap_name": f"{account.id}__max_posts_per_day",
                "cap_value": override.max_posts_per_day if override and override.max_posts_per_day is not None else "",
            }
        )
    perms = _perms(request)
    return {
        "workspace": workspace,
        "policy": policy,
        "settings_active": "autonomy",
        "rows": rows,
        "override_rows": override_rows,
        "blocked_phrases_text": "\n".join(policy.blocked_phrases or []),
        "can_configure": perms.get("manage_workspace_settings", False),
        "can_pause": perms.get("manage_workspace_settings", False) or perms.get("approve_posts", False),
    }


def _form_bool(request, name) -> bool:
    return request.POST.get(name) == "on"


@login_required
@require_http_methods(["GET", "POST"])
def autonomy_settings(request, workspace_id):
    workspace = _get_workspace(request, workspace_id)
    context = _settings_context(request, workspace)
    if request.method == "POST":
        section = request.POST.get("section", "")
        actor = human(request.user)
        try:
            if section == "crisis":
                if not context["can_pause"]:
                    raise PermissionDenied("You do not have permission to change crisis pause.")
                pause = request.POST.get("crisis_paused") == "1"
                configure.update_policy(workspace, actor, crisis_paused=pause)
                messages.success(
                    request,
                    "Crisis pause is on. Every autonomous action is stopped and queued ones are held."
                    if pause
                    else "Crisis pause is off. Autonomous actions and queued items resume.",
                )
            else:
                if not context["can_configure"]:
                    raise PermissionDenied("You do not have permission to change autonomy settings.")
                if section == "levels":
                    configure.update_policy(
                        workspace,
                        actor,
                        **{field: request.POST.get(field, "") for _, field, _ in DIAL_ROWS},
                        undo_delay_minutes=request.POST.get("undo_delay_minutes", ""),
                    )
                elif section == "overrides":
                    for account in SocialAccount.objects.filter(workspace=workspace):
                        configure.set_account_override(
                            workspace,
                            account,
                            actor,
                            **{field: request.POST.get(f"{account.id}__{field}", "") for _, field, _ in DIAL_ROWS},
                            max_posts_per_day=request.POST.get(f"{account.id}__max_posts_per_day", ""),
                        )
                elif section == "guardrails":
                    configure.update_policy(
                        workspace,
                        actor,
                        blocked_phrases=request.POST.get("blocked_phrases", ""),
                        review_links=_form_bool(request, "review_links"),
                        review_mentions=_form_bool(request, "review_mentions"),
                        review_prices=_form_bool(request, "review_prices"),
                        review_sensitive=_form_bool(request, "review_sensitive"),
                        max_posts_per_day=request.POST.get("max_posts_per_day", ""),
                        max_replies_per_day=request.POST.get("max_replies_per_day", ""),
                        quiet_hours_enabled=_form_bool(request, "quiet_hours_enabled"),
                        quiet_start=request.POST.get("quiet_start", ""),
                        quiet_end=request.POST.get("quiet_end", ""),
                        ai_risk_check=_form_bool(request, "ai_risk_check"),
                        ai_risk_threshold=request.POST.get("ai_risk_threshold", ""),
                        ai_voice_check=_form_bool(request, "ai_voice_check"),
                        ai_voice_threshold=request.POST.get("ai_voice_threshold", ""),
                    )
                elif section == "clients":
                    configure.update_policy(workspace, actor, client_visible=_form_bool(request, "client_visible"))
                else:
                    raise Http404
                messages.success(request, "Autonomy settings saved.")
        except ValueError as exc:
            messages.error(request, str(exc))
        return redirect("autonomy:settings", workspace_id=workspace.id)
    return render(request, "autonomy/settings.html", context)


@login_required
@require_permission("approve_posts")
@require_POST
def crisis_toggle(request, workspace_id):
    """One-click crisis pause from the Activity page header."""
    workspace = _get_workspace(request, workspace_id)
    pause = request.POST.get("crisis_paused") == "1"
    configure.update_policy(workspace, human(request.user), crisis_paused=pause)
    messages.success(request, "Crisis pause is on." if pause else "Crisis pause is off.")
    return redirect("autonomy:activity", workspace_id=workspace.id)
