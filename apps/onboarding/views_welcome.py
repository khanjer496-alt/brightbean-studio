"""Views for the first-run welcome flow, the example workspace and the hints.

The flow is three steps (workspace, first channel, first post) and every step
can be skipped or left and resumed. Only the owner of a freshly created
workspace has a flow, so an invited member who opens these URLs gets a 404.
"""

import datetime as dt
import logging
import zoneinfo
from zoneinfo import available_timezones

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from django_ratelimit.decorators import ratelimit

from apps.ai_writer.client import AIError, ai_available
from apps.ai_writer.services import AIContext, write_caption
from apps.billing.entitlements import PlanLimitError, billing_enabled, check_can_schedule_posts, get_subscription
from apps.composer.services import create_post
from apps.members.models import WorkspaceMembership
from apps.organizations.views import COMMON_TIMEZONES

from . import flow as flow_service
from . import hints as hint_service
from . import sample as sample_service
from .models import OnboardingFlow

logger = logging.getLogger(__name__)

MAX_WORKSPACE_NAME = 100
MAX_CAPTION = 3000


def _owner_flow(request, workspace_id):
    """The signed-in user's flow for ``workspace_id``; 404 unless they own it."""
    membership = request.workspace_membership
    if membership is None or membership.workspace_role != WorkspaceMembership.WorkspaceRole.OWNER:
        raise PermissionDenied("Only the workspace owner can run the welcome flow.")
    return get_object_or_404(OnboardingFlow, user=request.user, workspace_id=workspace_id)


def _trial_banner(workspace):
    """What to say about the free trial, or ``None`` when billing is off or over."""
    if not billing_enabled():
        return None
    subscription = get_subscription(workspace.organization)
    if not subscription.is_trialing:
        return None
    days_left = max((subscription.trial_ends_at - timezone.now()).days, 0)
    return {"plan": subscription.effective_plan().name, "days_left": days_left}


def _render_step(request, flow, step, extra=None):
    index = flow_service.step_index(step)
    context = {
        "flow": flow,
        "workspace": flow.workspace,
        "step": step,
        "step_number": index + 1,
        "step_count": len(flow_service.STEPS),
        "steps": [
            {"number": number + 1, "label": label, "done": number < index, "current": number == index}
            for number, label in enumerate(["Workspace", "Channel", "First post"])
        ],
        "focus_layout": True,
        "skip_url": _url("welcome_skip", flow.workspace_id),
        "trial": _trial_banner(flow.workspace),
        **(extra or {}),
    }
    return render(request, f"onboarding/welcome/step_{step}.html", context)


def _url(name, workspace_id):
    return reverse(f"onboarding:{name}", kwargs={"workspace_id": workspace_id})


def _guard_step(request, workspace_id, step):
    """Load the flow and redirect when ``step`` is ahead of where the user is."""
    flow = _owner_flow(request, workspace_id)
    flow_service.resume(flow)
    if flow.status == OnboardingFlow.Status.COMPLETED:
        return flow, redirect("onboarding:welcome_done", workspace_id=workspace_id)
    if flow_service.step_index(step) > flow_service.step_index(flow.step):
        return flow, redirect(flow_service.step_url(flow))
    return flow, None


# ---------------------------------------------------------------------------
# Entry, skip
# ---------------------------------------------------------------------------


@login_required
@require_GET
def welcome(request, workspace_id):
    """Open the flow at the step the user left off at."""
    flow = _owner_flow(request, workspace_id)
    flow_service.resume(flow)
    if flow.status == OnboardingFlow.Status.COMPLETED:
        return redirect("onboarding:welcome_done", workspace_id=workspace_id)
    return redirect(flow_service.step_url(flow))


@login_required
@require_POST
def welcome_skip(request, workspace_id):
    """Leave the flow for now; the checklist offers a way back in."""
    flow = _owner_flow(request, workspace_id)
    flow_service.skip(flow)
    return redirect("calendar:calendar", workspace_id=workspace_id)


# ---------------------------------------------------------------------------
# Step 1: workspace
# ---------------------------------------------------------------------------


def _timezone_context(selected):
    return {
        "common_timezones": COMMON_TIMEZONES,
        "all_timezones": sorted(available_timezones()),
        "selected_timezone": selected,
        "kinds": [
            {"value": kind.value, "label": label, "hint": hint}
            for kind, (label, hint) in flow_service.KIND_LABELS.items()
        ],
    }


@login_required
@require_http_methods(["GET", "POST"])
def welcome_workspace(request, workspace_id):
    flow, redirection = _guard_step(request, workspace_id, OnboardingFlow.Step.WORKSPACE)
    if redirection:
        return redirection
    workspace = flow.workspace

    if request.method == "GET":
        context = _timezone_context(flow_service.initial_timezone(request, workspace))
        context.update({"name": workspace.name, "kind": flow.account_kind, "errors": {}})
        return _render_step(request, flow, OnboardingFlow.Step.WORKSPACE, context)

    name = request.POST.get("name", "").strip()
    tz_name = request.POST.get("timezone", "").strip()
    kind = request.POST.get("account_kind", "").strip()
    errors = {}
    if not name:
        errors["name"] = "Give your workspace a name."
    elif len(name) > MAX_WORKSPACE_NAME:
        errors["name"] = f"Keep the name under {MAX_WORKSPACE_NAME} characters."
    if tz_name not in available_timezones():
        errors["timezone"] = "Choose a timezone from the list."
    if kind not in OnboardingFlow.AccountKind.values:
        errors["account_kind"] = "Tell us which one fits best."
    if errors:
        context = _timezone_context(tz_name if tz_name in available_timezones() else workspace.effective_timezone)
        context.update({"name": name, "kind": kind, "errors": errors})
        return _render_step(request, flow, OnboardingFlow.Step.WORKSPACE, context)

    workspace.name = name
    workspace.timezone = tz_name
    workspace.save(update_fields=["name", "timezone", "updated_at"])
    flow.account_kind = kind
    flow.save(update_fields=["account_kind", "updated_at"])
    # Never move a user backwards if they came back to edit this step.
    if flow_service.step_index(flow.step) < flow_service.step_index(OnboardingFlow.Step.CHANNEL):
        flow_service.advance(flow, OnboardingFlow.Step.CHANNEL)
    return redirect("onboarding:welcome_channel", workspace_id=workspace_id)


# ---------------------------------------------------------------------------
# Step 2: first channel
# ---------------------------------------------------------------------------


@login_required
@require_GET
def welcome_channel(request, workspace_id):
    flow, redirection = _guard_step(request, workspace_id, OnboardingFlow.Step.CHANNEL)
    if redirection:
        return redirection
    workspace = flow.workspace
    platforms, spotlight = flow_service.connectable_platforms(workspace.organization_id)
    connected = flow_service.first_post_accounts(workspace)
    return _render_step(
        request,
        flow,
        OnboardingFlow.Step.CHANNEL,
        {
            "platforms": platforms,
            "spotlight": spotlight,
            "connected_accounts": connected,
            "next_url": _url("welcome_channel_next", workspace_id),
        },
    )


@login_required
@require_POST
def welcome_channel_next(request, workspace_id):
    """Move on from the channel step, with or without a channel connected."""
    flow, redirection = _guard_step(request, workspace_id, OnboardingFlow.Step.CHANNEL)
    if redirection:
        return redirection
    if flow_service.step_index(flow.step) < flow_service.step_index(OnboardingFlow.Step.POST):
        flow_service.advance(flow, OnboardingFlow.Step.POST)
    return redirect("onboarding:welcome_post", workspace_id=workspace_id)


# ---------------------------------------------------------------------------
# Step 3: first post
# ---------------------------------------------------------------------------


def _first_post_context(request, flow, accounts, *, caption=None, account=None, when=None, error=""):
    workspace = flow.workspace
    chosen = account or (accounts[0] if accounts else None)
    suggested, is_best = flow_service.suggest_post_time(chosen) if chosen else (None, False)
    when = when or suggested
    return {
        "accounts": accounts,
        "chosen_account": chosen,
        "caption": caption if caption is not None else flow_service.example_caption(flow, settings.BRAND_SHORT_NAME),
        "schedule_date": when.strftime("%Y-%m-%d") if when else "",
        "schedule_time": when.strftime("%H:%M") if when else "",
        "suggested_label": suggested.strftime("%a %b %-d, %-I:%M %p") if suggested else "",
        "is_best_time": is_best,
        "timezone_name": workspace.effective_timezone or "UTC",
        "ai_enabled": ai_available(workspace.organization),
        "error": error,
        "max_caption": MAX_CAPTION,
        "compose_url": reverse("composer:compose", kwargs={"workspace_id": workspace.id}),
        "connect_url": reverse("social_accounts:connect", kwargs={"workspace_id": workspace.id}),
    }


@login_required
@require_http_methods(["GET", "POST"])
def welcome_post(request, workspace_id):
    flow, redirection = _guard_step(request, workspace_id, OnboardingFlow.Step.POST)
    if redirection:
        return redirection
    workspace = flow.workspace
    accounts = flow_service.first_post_accounts(workspace)

    if request.method == "GET":
        return _render_step(request, flow, OnboardingFlow.Step.POST, _first_post_context(request, flow, accounts))

    caption = request.POST.get("caption", "").strip()
    account = next((a for a in accounts if str(a.id) == request.POST.get("account", "")), None)
    save_as_draft = request.POST.get("action") == "draft"

    def fail(message):
        return _render_step(
            request,
            flow,
            OnboardingFlow.Step.POST,
            _first_post_context(request, flow, accounts, caption=caption, account=account, error=message),
        )

    if account is None:
        return fail("Choose a channel to post to.")
    if not caption:
        return fail("Write a caption first, or use the example.")
    if len(caption) > MAX_CAPTION:
        return fail(f"Keep the caption under {MAX_CAPTION} characters.")

    scheduled_at = None
    if not save_as_draft:
        tz = zoneinfo.ZoneInfo(workspace.effective_timezone or "UTC")
        try:
            local = dt.datetime.strptime(
                f"{request.POST.get('schedule_date', '')} {request.POST.get('schedule_time', '')}", "%Y-%m-%d %H:%M"
            )
        except ValueError:
            return fail("Pick a date and time for the post.")
        scheduled_at = local.replace(tzinfo=tz)
        if scheduled_at <= timezone.now():
            return fail("Pick a time in the future.")
        try:
            check_can_schedule_posts(workspace.organization, count=1, when=scheduled_at)
        except PlanLimitError as exc:
            return fail(exc.message)

    try:
        create_post(
            workspace=workspace,
            social_account=account,
            caption=caption,
            scheduled_at=scheduled_at,
            author=request.user,
            status="draft" if save_as_draft else "scheduled",
        )
    except ValueError as exc:
        return fail(str(exc))

    flow_service.complete(flow)
    return redirect("onboarding:welcome_done", workspace_id=workspace_id)


@login_required
@require_POST
@ratelimit(key="user", rate="20/m", method="POST", block=True)
def welcome_ai_write(request, workspace_id):
    """Draft the first caption with the AI writer; answers with the caption field."""
    flow = _owner_flow(request, workspace_id)
    workspace = flow.workspace
    if not ai_available(workspace.organization):
        raise Http404
    prompt = request.POST.get("prompt", "").strip()
    caption = request.POST.get("caption", "")
    error = ""
    if not prompt:
        error = "Say what the post is about, then try again."
    else:
        accounts = [a for a in flow_service.first_post_accounts(workspace) if str(a.id) == request.POST.get("account")]
        try:
            caption = write_caption(AIContext(workspace=workspace, user=request.user), prompt, accounts)
        except (ValueError, PlanLimitError, AIError) as exc:
            error = getattr(exc, "message", str(exc))
    return render(
        request,
        "onboarding/welcome/_caption_field.html",
        {"caption": caption, "error": error, "max_caption": MAX_CAPTION},
    )


@login_required
@require_GET
def welcome_done(request, workspace_id):
    flow = _owner_flow(request, workspace_id)
    from apps.composer.models import PlatformPost

    first_post = (
        PlatformPost.objects.filter(post__workspace_id=workspace_id, post__author=request.user)
        .select_related("post", "social_account")
        .order_by("-created_at")
        .first()
    )
    return render(
        request,
        "onboarding/welcome/done.html",
        {
            "focus_layout": True,
            "flow": flow,
            "workspace": flow.workspace,
            "first_post": first_post,
            "trial": _trial_banner(flow.workspace),
            "ai_enabled": ai_available(flow.workspace.organization),
        },
    )


# ---------------------------------------------------------------------------
# Example workspace
# ---------------------------------------------------------------------------


def _require_workspace_owner(request):
    membership = request.workspace_membership
    if membership is None or membership.workspace_role != WorkspaceMembership.WorkspaceRole.OWNER:
        raise PermissionDenied("Only the workspace owner can use the example workspace.")


@login_required
@require_POST
def sample_create(request, workspace_id):
    """ "Show me an example": build the example workspace and open its calendar."""
    _require_workspace_owner(request)
    if sample_service.is_sample_workspace(workspace_id):
        raise PermissionDenied("This already is the example workspace.")
    sample = sample_service.create_sample_workspace(user=request.user, source_workspace=request.workspace)
    request.user.last_workspace_id = sample.id
    request.user.save(update_fields=["last_workspace_id"])
    return redirect(reverse("calendar:calendar", kwargs={"workspace_id": sample.id}) + "?mode=calendar&view=week")


@login_required
@require_POST
def sample_remove(request, workspace_id):
    """Remove the example workspace in one click and go back to the real one."""
    if not sample_service.is_sample_workspace(workspace_id):
        raise Http404
    _require_workspace_owner(request)
    target = sample_service.remove_sample_workspace(
        user=request.user, organization_id=request.workspace.organization_id
    )
    if target is None:
        return redirect("dashboard")
    return redirect("calendar:calendar", workspace_id=target.id)


# ---------------------------------------------------------------------------
# Hints
# ---------------------------------------------------------------------------


@login_required
@require_POST
def dismiss_hint(request, key):
    if not hint_service.dismiss(request.user, key):
        raise Http404
    return HttpResponse(status=200)
