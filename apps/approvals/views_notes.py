"""Team-only notes on a post: the panel in the composer sidebar."""

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET, require_POST

from apps.composer.models import Post

from . import notes as note_service
from .views import _get_workspace


def _note_post(request, workspace_id, post_id):
    """The workspace and post for a notes request, refusing anyone who may not read notes."""
    workspace = _get_workspace(request, workspace_id)
    if note_service.membership_for(request.user, workspace) is None:
        raise PermissionDenied("Internal notes are for the team.")
    return workspace, get_object_or_404(Post, id=post_id, workspace=workspace)


def _panel(request, workspace, post):
    return render(
        request,
        "approvals/partials/notes_panel.html",
        {"workspace": workspace, "post": post, "notes": note_service.notes_for_post(post)},
    )


@login_required
@require_GET
def notes_panel(request, workspace_id, post_id):
    """The post's notes thread and the form to add one."""
    workspace, post = _note_post(request, workspace_id, post_id)
    return _panel(request, workspace, post)


@login_required
@require_POST
def add_note(request, workspace_id, post_id):
    """Add a note, then redraw the thread."""
    workspace, post = _note_post(request, workspace_id, post_id)
    try:
        note_service.create_note(post, request.user, request.POST.get("body", ""))
    except ValueError as exc:
        return HttpResponse(str(exc), status=400)
    return _panel(request, workspace, post)


@login_required
@require_POST
def delete_note(request, workspace_id, post_id, note_id):
    """Remove one of the thread's notes (its author, or an editor of others' posts)."""
    workspace, post = _note_post(request, workspace_id, post_id)
    try:
        note_service.delete_note(note_id, request.user, workspace)
    except ValueError as exc:
        return HttpResponse(str(exc), status=400)
    except PermissionError as exc:
        return HttpResponse(str(exc), status=403)
    return _panel(request, workspace, post)
