from django import template

from apps.members.models import WorkspaceMembership
from apps.onboarding import empty_states, hints
from apps.onboarding.sample import is_sample_workspace

register = template.Library()


@register.inclusion_tag("onboarding/partials/_empty_state.html", takes_context=True)
def empty_state(context, key, workspace, has_channels=None, show_action=True):
    """Render the shared empty state for a page: ``{% empty_state "inbox" workspace %}``.

    ``show_action=False`` leaves the button out for people who may not use it.
    """
    membership = getattr(context.get("request"), "workspace_membership", None)
    is_owner = bool(membership and membership.workspace_role == WorkspaceMembership.WorkspaceRole.OWNER)
    offer_sample = is_owner and not is_sample_workspace(workspace.id)
    state = empty_states.empty_state(key, workspace, has_channels=has_channels, offer_sample=offer_sample)
    if not show_action:
        state["action_url"] = state["action_click"] = ""
    state["csrf_token"] = context.get("csrf_token", "")
    return state


@register.inclusion_tag("onboarding/partials/_hint.html", takes_context=True)
def onboarding_hint(context, key, extra_class=""):
    """Render the first-visit hint ``key`` unless the user has closed it.

    ``extra_class`` adds spacing classes for full-bleed pages such as the composer.
    """
    request = context.get("request")
    user = getattr(request, "user", None)
    workspace = getattr(request, "workspace", None)
    # The example workspace already carries its own banner; no second callout.
    if not user or (workspace and is_sample_workspace(workspace.id)):
        return {"hint": None, "request": request}
    return {"hint": hints.hint_for(user, key), "request": request, "extra_class": extra_class}
