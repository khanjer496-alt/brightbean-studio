"""Slack approvals: the signed interactions endpoint and the workspace settings actions."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from apps.members.models import WorkspaceMembership
from apps.workspaces.models import Workspace

from . import digests, slack
from .models import SlackApprovalConnection

_MANAGERS = (WorkspaceMembership.WorkspaceRole.OWNER, WorkspaceMembership.WorkspaceRole.MANAGER)


def _manager_workspace(request, workspace_id):
    workspace = get_object_or_404(Workspace, id=workspace_id)
    membership = WorkspaceMembership.objects.filter(user=request.user, workspace=workspace).first()
    if membership is None or membership.workspace_role not in _MANAGERS:
        raise PermissionDenied("Only owners and managers can change approval settings.")
    return workspace


def _back(workspace):
    return redirect("workspaces:approvals_settings", workspace_id=workspace.id)


@csrf_exempt
@require_POST
def interactions(request):
    """Slack's button and dialog callbacks. Authenticated only by Slack's request signature."""
    status, body = slack.process_request(
        request.body,
        request.headers.get("X-Slack-Request-Timestamp", ""),
        request.headers.get("X-Slack-Signature", ""),
    )
    if status != 200:
        return HttpResponse(status=status)
    return JsonResponse(body) if body is not None else HttpResponse(status=200)


@login_required
@require_POST
def connect(request, workspace_id):
    workspace = _manager_workspace(request, workspace_id)
    if not slack.is_available(workspace):
        raise PermissionDenied("Slack approvals aren't set up on this deployment.")
    return redirect(slack.authorize_url(workspace, request.user))


@login_required
@require_GET
def callback(request):
    state = slack.read_state(request.GET.get("state", ""))
    if state is None or state.get("u") != str(request.user.pk):
        return HttpResponse("This Slack connection request has expired. Please start again.", status=400)
    workspace = _manager_workspace(request, state["w"])
    if request.GET.get("error") or not request.GET.get("code"):
        messages.error(request, "Slack was not connected.")
        return _back(workspace)
    try:
        slack.connect(workspace, request.user, request.GET["code"])
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Slack connected. Pick the channel for approval requests.")
    return _back(workspace)


@login_required
@require_POST
def choose_channel(request, workspace_id):
    workspace = _manager_workspace(request, workspace_id)
    connection = get_object_or_404(SlackApprovalConnection, workspace=workspace)
    try:
        slack.choose_channel(connection, request.POST.get("channel_id", ""))
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Approval requests will go to that channel.")
    return _back(workspace)


@login_required
@require_POST
def disconnect(request, workspace_id):
    workspace = _manager_workspace(request, workspace_id)
    SlackApprovalConnection.objects.filter(workspace=workspace).delete()
    messages.success(request, "Slack approvals disconnected.")
    return _back(workspace)


@login_required
@require_POST
def send_digest(request, workspace_id):
    workspace = _manager_workspace(request, workspace_id)
    sent = digests.send_review_digests(workspace)
    messages.success(request, f"Sent the review digest to {sent} {'person' if sent == 1 else 'people'}.")
    return _back(workspace)
