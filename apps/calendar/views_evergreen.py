"""The Evergreen library: content that is re-posted into queues now and then."""

import json
import uuid

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.composer.models import ContentCategory, PlatformPost
from apps.members.decorators import require_permission
from apps.social_accounts.models import SocialAccount

from . import evergreen
from .models import EvergreenItem
from .views import _get_workspace

STATUS_FILTERS = {"active": "Active", "paused": "Paused", "all": "All"}
LIST_LIMIT = 200


def _uuid_or_none(value):
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


def _item_or_404(workspace, item_id):
    return get_object_or_404(
        EvergreenItem.objects.for_workspace(workspace.id).select_related("social_account", "category", "created_by"),
        id=item_id,
    )


def _can(request, permission):
    return request.workspace_membership.effective_permissions.get(permission, False)


def _rules_from(request):
    """The recycling rules the add and edit forms post, as keyword arguments for the service."""
    return {
        "interval_days": request.POST.get("interval_days", 30),
        "max_repeats": request.POST.get("max_repeats"),
        "vary_with_ai": request.POST.get("wording") == "ai",
        "pre_approved": request.POST.get("pre_approved") == "on",
    }


def _category_from(request, workspace):
    """The category the form chose, or created from its "new category" box."""
    new_name = request.POST.get("new_category", "").strip()
    if new_name:
        return evergreen.get_or_create_category(workspace, new_name)
    category_id = _uuid_or_none(request.POST.get("category_id"))
    if category_id is None:
        return None
    return get_object_or_404(ContentCategory, id=category_id, workspace=workspace)


def _guard_pre_approval(request, rules):
    if rules["pre_approved"] and not _can(request, "approve_posts"):
        raise PermissionDenied("Only people who can approve posts can pre-approve recycling.")


@login_required
def evergreen_list(request, workspace_id):
    """The library: every evergreen item with its category, rules and progress."""
    workspace = _get_workspace(request, workspace_id)
    items = (
        EvergreenItem.objects.for_workspace(workspace.id)
        .select_related("social_account", "category")
        .annotate(history_count=Count("repeats"))
    )
    category_id = _uuid_or_none(request.GET.get("category"))
    account_id = _uuid_or_none(request.GET.get("account"))
    status = request.GET.get("status", "all")
    if category_id:
        items = items.filter(category_id=category_id)
    if account_id:
        items = items.filter(social_account_id=account_id)
    if status == "active":
        items = items.filter(is_paused=False)
    elif status == "paused":
        items = items.filter(is_paused=True)
    items = items.order_by("category__name", "last_posted_at", "created_at")[:LIST_LIMIT]
    return render(
        request,
        "calendar/evergreen.html",
        {
            "workspace": workspace,
            "items": items,
            "categories": ContentCategory.objects.for_workspace(workspace.id),
            "accounts": SocialAccount.objects.for_workspace(workspace.id),
            "status_filters": STATUS_FILTERS,
            "selected_category": str(category_id or ""),
            "selected_account": str(account_id or ""),
            "selected_status": status if status in STATUS_FILTERS else "all",
            "can_edit": _can(request, "create_posts"),
        },
    )


@login_required
def evergreen_detail(request, workspace_id, item_id):
    """One item: its rules, the next time it can go out, and every repeat with its results."""
    workspace = _get_workspace(request, workspace_id)
    item = _item_or_404(workspace, item_id)
    repeats = list(item.repeats.select_related("post", "platform_post", "item__social_account")[:100])
    performance = evergreen.repeat_performance(repeats)
    for repeat in repeats:
        repeat.stats = performance.get(repeat.id)
    return render(
        request,
        "calendar/evergreen_detail.html",
        {
            "workspace": workspace,
            "item": item,
            "repeats": repeats,
            "problem": evergreen.media_problem(item),
            "categories": ContentCategory.objects.for_workspace(workspace.id),
            "can_edit": _can(request, "create_posts"),
            "can_pre_approve": _can(request, "approve_posts"),
        },
    )


@login_required
@require_permission("create_posts")
def evergreen_add_form(request, workspace_id):
    """The "Add to evergreen" dialog for one published post (HTMX partial)."""
    workspace = _get_workspace(request, workspace_id)
    pp = get_object_or_404(
        PlatformPost.objects.select_related("post", "social_account"),
        id=_uuid_or_none(request.GET.get("platform_post_id")),
        post__workspace=workspace,
        status=PlatformPost.Status.PUBLISHED,
    )
    return render(
        request,
        "calendar/partials/_evergreen_add_form.html",
        {
            "workspace": workspace,
            "pp": pp,
            "categories": ContentCategory.objects.for_workspace(workspace.id),
            "can_pre_approve": _can(request, "approve_posts"),
            "defaults": {"interval_days": 30},
        },
    )


@login_required
@require_POST
@require_permission("create_posts")
def evergreen_add(request, workspace_id):
    """Add a published post to the library."""
    workspace = _get_workspace(request, workspace_id)
    pp = get_object_or_404(
        PlatformPost.objects.select_related("post", "social_account"),
        id=_uuid_or_none(request.POST.get("platform_post_id")),
        post__workspace=workspace,
    )
    rules = _rules_from(request)
    _guard_pre_approval(request, rules)
    context = {
        "workspace": workspace,
        "pp": pp,
        "categories": ContentCategory.objects.for_workspace(workspace.id),
        "can_pre_approve": _can(request, "approve_posts"),
        "defaults": {"interval_days": rules["interval_days"], "max_repeats": rules["max_repeats"]},
    }
    try:
        item = evergreen.add_from_platform_post(
            pp, user=request.user, category=_category_from(request, workspace), **rules
        )
    except evergreen.EvergreenError as exc:
        return render(request, "calendar/partials/_evergreen_add_form.html", {**context, "error": str(exc)})
    return render(request, "calendar/partials/_evergreen_added.html", {**context, "item": item})


@login_required
@require_POST
@require_permission("create_posts")
def evergreen_update(request, workspace_id, item_id):
    """Change an item's category and recycling rules."""
    workspace = _get_workspace(request, workspace_id)
    item = _item_or_404(workspace, item_id)
    rules = _rules_from(request)
    _guard_pre_approval(request, rules)
    try:
        interval_days, max_repeats = evergreen.clean_rules(rules["interval_days"], rules["max_repeats"])
        category = _category_from(request, workspace)
    except evergreen.EvergreenError as exc:
        return HttpResponse(str(exc), status=400)
    item.interval_days, item.max_repeats = interval_days, max_repeats
    item.vary_with_ai = rules["vary_with_ai"]
    item.category = category
    if rules["pre_approved"] != item.pre_approved:
        item.pre_approved = rules["pre_approved"]
        item.pre_approved_by = request.user if rules["pre_approved"] else None
    item.save()
    return redirect("calendar:evergreen_detail", workspace_id=workspace.id, item_id=item.id)


@login_required
@require_POST
@require_permission("create_posts")
def evergreen_pause(request, workspace_id, item_id):
    """Pause (default) or resume (``paused=0``) an item."""
    workspace = _get_workspace(request, workspace_id)
    item = _item_or_404(workspace, item_id)
    evergreen.set_paused(item, request.POST.get("paused", "1") != "0")
    if request.htmx:
        return HttpResponse(status=204, headers={"HX-Trigger": json.dumps({"evergreenChanged": str(item.id)})})
    if request.POST.get("return") == "detail":
        return redirect("calendar:evergreen_detail", workspace_id=workspace.id, item_id=item.id)
    return redirect("calendar:evergreen_list", workspace_id=workspace.id)


@login_required
@require_POST
@require_permission("create_posts")
def evergreen_remove(request, workspace_id, item_id):
    """Take an item out of the library. Posts already made from it stay."""
    workspace = _get_workspace(request, workspace_id)
    _item_or_404(workspace, item_id).delete()
    if request.htmx:
        return HttpResponse(status=204, headers={"HX-Trigger": "evergreenChanged"})
    return redirect("calendar:evergreen_list", workspace_id=workspace.id)
