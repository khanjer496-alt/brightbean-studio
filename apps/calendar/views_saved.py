"""Saved views on the calendar and Publish page: the dropdown, saving, deleting and the default."""

from django.contrib.auth.decorators import login_required
from django.http import Http404, QueryDict
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from apps.common.htmx import toast_response

from . import saved_views
from .views import _get_workspace


def _menu(request, workspace):
    """The dropdown's list of views, with what the person may do to each."""
    membership = getattr(request, "workspace_membership", None)
    default_id = saved_views.default_view_id(workspace, request.user)
    rows = [
        {
            "view": saved,
            "url": saved_views.view_url(saved),
            "is_default": saved.id == default_id,
            "is_mine": saved.owner_id == request.user.id,
            "can_delete": saved_views.can_delete(saved, request.user, membership),
        }
        for saved in saved_views.visible_views(workspace, request.user)
    ]
    return render(
        request,
        "calendar/partials/_saved_views_menu.html",
        {"workspace": workspace, "rows": rows, "has_default": default_id is not None},
    )


def _own_or_shared_view(request, workspace, view_id):
    saved = saved_views.visible_views(workspace, request.user).filter(id=view_id).first()
    if saved is None:
        raise Http404("View not found.")
    return saved


@login_required
@require_GET
def saved_views_menu(request, workspace_id):
    workspace = _get_workspace(request, workspace_id)
    return _menu(request, workspace)


@login_required
@require_POST
def saved_view_create(request, workspace_id):
    """Save the filters the page is showing (posted as its query string) under a name."""
    workspace = _get_workspace(request, workspace_id)
    filters = saved_views.clean_filters(QueryDict(request.POST.get("query", "")))
    try:
        saved_views.create_view(
            workspace,
            request.user,
            getattr(request, "workspace_membership", None),
            name=request.POST.get("name", ""),
            filters=filters,
            shared=request.POST.get("shared") == "on",
        )
    except ValueError as exc:
        return toast_response(tone="error", title="Couldn't save the view", body=str(exc))
    return _menu(request, workspace)


@login_required
@require_POST
def saved_view_delete(request, workspace_id, view_id):
    workspace = _get_workspace(request, workspace_id)
    saved = _own_or_shared_view(request, workspace, view_id)
    if not saved_views.can_delete(saved, request.user, getattr(request, "workspace_membership", None)):
        return toast_response(tone="error", title="Couldn't delete the view", body="Only its owner can delete it.")
    saved_views.delete_view(saved)
    return _menu(request, workspace)


@login_required
@require_POST
def saved_view_default(request, workspace_id):
    """Make a view the one this person opens by default; no ``view_id`` clears it."""
    workspace = _get_workspace(request, workspace_id)
    view_id = request.POST.get("view_id")
    saved = _own_or_shared_view(request, workspace, view_id) if view_id else None
    saved_views.set_default(workspace, request.user, saved)
    return _menu(request, workspace)
