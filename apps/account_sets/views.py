"""Account sets under workspace settings, plus the JSON the composer uses to apply one.

Managing sets needs ``manage_social_accounts`` (or ``manage_workspace_settings``); using one needs
``create_posts``. Sets are looked up through ``request.workspace``, so an id from another workspace is a 404.
"""

import functools

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.members.decorators import require_permission
from apps.social_accounts.models import SocialAccount

from . import services
from .models import AccountSet


def _manage(view_func):
    @functools.wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not services.can_manage(request.workspace_membership):
            raise PermissionDenied("Permission denied: managing account sets")
        return view_func(request, *args, **kwargs)

    return login_required(_wrapped)


def _set(request, set_id) -> AccountSet:
    obj = services.get_set(request.workspace, set_id)
    if obj is None:
        raise Http404
    return obj


def _accounts(workspace):
    return SocialAccount.objects.for_workspace(workspace.id).order_by("platform", "account_name")


def _shown(value):
    if value is None:
        return ""
    return str(value).lower() if isinstance(value, bool) else value


def _form_rows(workspace, selected_ids, defaults):
    rows = []
    for account in _accounts(workspace):
        saved = defaults.get(str(account.id), {})
        rows.append(
            {
                "account": account,
                "selected": str(account.id) in selected_ids,
                "fields": [
                    {**field, "value": _shown(saved.get(field["key"]))}
                    for field in services.default_fields(account.platform)
                ],
            }
        )
    return rows


@_manage
@require_GET
def set_list(request, workspace_id):
    return render(
        request,
        "account_sets/list.html",
        {"workspace": request.workspace, "settings_active": "sets", "sets": services.sets_for(request.workspace)},
    )


def _save(request, obj):
    post = request.POST
    account_ids = post.getlist("accounts")
    accounts = list(_accounts(request.workspace).filter(id__in=account_ids))
    defaults = services.parse_defaults(post.get, accounts)
    if obj is None:
        return services.create_set(
            request.workspace, name=post.get("name", ""), account_ids=account_ids, defaults=defaults, user=request.user
        )
    return services.update_set(obj, name=post.get("name", ""), account_ids=account_ids, defaults=defaults)


@_manage
@require_http_methods(["GET", "POST"])
def set_create(request, workspace_id):
    selected, defaults = set(request.POST.getlist("accounts")), {}
    if request.method == "POST":
        try:
            obj = _save(request, None)
        except services.SetError as exc:
            messages.error(request, exc.message)
        else:
            messages.success(request, f"Set “{obj.name}” saved.")
            return redirect("account_sets:list", workspace_id=request.workspace.id)
    return render(
        request,
        "account_sets/form.html",
        {
            "workspace": request.workspace,
            "settings_active": "sets",
            "obj": None,
            "name": request.POST.get("name", ""),
            "rows": _form_rows(request.workspace, selected, defaults),
        },
    )


@_manage
@require_http_methods(["GET", "POST"])
def set_edit(request, workspace_id, set_id):
    obj = _set(request, set_id)
    if request.method == "POST":
        try:
            _save(request, obj)
        except services.SetError as exc:
            messages.error(request, exc.message)
        else:
            messages.success(request, "Set saved.")
            return redirect("account_sets:list", workspace_id=request.workspace.id)
        selected, defaults, name = set(request.POST.getlist("accounts")), {}, request.POST.get("name", "")
    else:
        selected = {str(a.id) for a in obj.accounts.all()}
        defaults, name = obj.defaults, obj.name
    return render(
        request,
        "account_sets/form.html",
        {
            "workspace": request.workspace,
            "settings_active": "sets",
            "obj": obj,
            "name": name,
            "rows": _form_rows(request.workspace, selected, defaults),
        },
    )


@_manage
@require_POST
def set_delete(request, workspace_id, set_id):
    obj = _set(request, set_id)
    services.delete_set(obj)
    messages.success(request, "Set deleted.")
    return redirect("account_sets:list", workspace_id=request.workspace.id)


@login_required
@require_permission("create_posts")
@require_GET
def set_picker(request, workspace_id):
    """The workspace's sets with their accounts and defaults, for the composer, CSV import and repurpose pickers."""
    return JsonResponse({"sets": [services.composer_payload(obj) for obj in services.sets_for(request.workspace)]})


@login_required
@require_permission("create_posts")
@require_GET
def set_apply(request, workspace_id, set_id):
    return JsonResponse(services.composer_payload(_set(request, set_id)))
