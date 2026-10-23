"""Context processor for the onboarding checklist widget and the sample banner."""

from django.utils.functional import SimpleLazyObject

from apps.onboarding.models import OnboardingChecklist

_COMPOSER_VIEWS = {"composer:compose", "composer:compose_edit"}


def _build(request):
    from apps.members.models import WorkspaceMembership
    from apps.onboarding.checklist import get_checklist_items
    from apps.onboarding.models import OnboardingFlow
    from apps.onboarding.sample import is_sample_workspace

    context = {"checklist_dismissed": True, "checklist_items": [], "is_sample_workspace": False}
    workspace = getattr(request, "workspace", None)
    membership = getattr(request, "workspace_membership", None)
    if workspace is None or membership is None:
        return context

    context["is_sample_workspace"] = is_sample_workspace(workspace.id)
    if context["is_sample_workspace"]:
        return context

    # The checklist is the owner's: invited members have their own jobs.
    if membership.workspace_role != WorkspaceMembership.WorkspaceRole.OWNER:
        return context

    if OnboardingChecklist.objects.filter(user=request.user, workspace=workspace, is_dismissed=True).exists():
        return context

    items = get_checklist_items(workspace, request.user)
    completed = sum(1 for item in items if item["completed"])
    # Nothing left to do: stop showing it.
    if completed == len(items):
        return context

    match = getattr(request, "resolver_match", None)
    # A flow still in progress or skipped can be picked up again from here.
    flow = OnboardingFlow.objects.filter(
        user=request.user,
        workspace=workspace,
        status__in=[OnboardingFlow.Status.ACTIVE, OnboardingFlow.Status.SKIPPED],
    ).first()
    return {
        **context,
        "checklist_dismissed": False,
        "checklist_items": items,
        "checklist_completed_count": completed,
        "checklist_total_count": len(items),
        # Phones keep the composer clear: its bottom sheets own the screen.
        "checklist_hidden_on_phone": bool(match and match.view_name in _COMPOSER_VIEWS),
        "checklist_flow": flow,
    }


def onboarding_checklist(request):
    """Expose ``onboarding`` to templates, computed only if a template reads it."""
    if not hasattr(request, "user") or not request.user.is_authenticated:
        return {}
    return {"onboarding": SimpleLazyObject(lambda: _build(request))}
