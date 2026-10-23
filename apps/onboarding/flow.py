"""The first-run welcome flow: state, steps and the suggestions it offers.

A new owner is walked through three short steps (workspace, first channel,
first post). The flow is skippable and resumable: ``OnboardingFlow.step`` is
always the step to show next, and a skipped flow can be picked up again.
"""

from __future__ import annotations

import datetime as dt
import zoneinfo
from urllib.parse import unquote

from django.urls import reverse
from django.utils import timezone

from apps.onboarding.models import OnboardingFlow

STEPS = [
    OnboardingFlow.Step.WORKSPACE,
    OnboardingFlow.Step.CHANNEL,
    OnboardingFlow.Step.POST,
]

# Set when "/" has already sent the user into the flow this session, so
# clicking the logo afterwards takes them to their calendar instead of back.
SESSION_KEY = "onboarding_welcomed"

KIND_LABELS = {
    OnboardingFlow.AccountKind.CREATOR: ("Creator", "I post as myself or for my own audience."),
    OnboardingFlow.AccountKind.BUSINESS: ("Business", "I post for my company or brand."),
    OnboardingFlow.AccountKind.AGENCY: ("Agency", "I manage social media for clients."),
}

_EXAMPLE_CAPTIONS = {
    OnboardingFlow.AccountKind.CREATOR: (
        "Hello, world! I'm planning my posts ahead with {brand}, so I can show up consistently. "
        "What should I share next?"
    ),
    OnboardingFlow.AccountKind.BUSINESS: (
        "We're getting set up to share more with you here. What would you like to see from us this month? "
        "Tell us in the replies."
    ),
    OnboardingFlow.AccountKind.AGENCY: (
        "A little update from our team: we're lining up the content calendar for the weeks ahead. More soon."
    ),
}
_DEFAULT_CAPTION = "Hello, world! This is our first post, scheduled with {brand}. More coming soon, so stay tuned."


def start_flow(user, workspace):
    """Begin the welcome flow for a newly provisioned owner (idempotent)."""
    flow, _created = OnboardingFlow.objects.get_or_create(user=user, workspace=workspace)
    return flow


def active_flow(user):
    """The flow to resume for ``user``, or ``None``.

    Only a flow still in progress counts, on a workspace the user still owns.
    """
    from apps.members.models import WorkspaceMembership

    return (
        OnboardingFlow.objects.filter(
            user=user,
            status=OnboardingFlow.Status.ACTIVE,
            workspace__is_archived=False,
            workspace__memberships__user=user,
            workspace__memberships__workspace_role=WorkspaceMembership.WorkspaceRole.OWNER,
        )
        .select_related("workspace")
        .order_by("-created_at")
        .first()
    )


def step_url(flow, step=None):
    """URL of ``step`` (default: the flow's current one)."""
    step = step or flow.step
    return reverse(f"onboarding:welcome_{step}", kwargs={"workspace_id": flow.workspace_id})


def step_index(step):
    return STEPS.index(step)


def advance(flow, step):
    flow.step = step
    flow.save(update_fields=["step", "updated_at"])


def skip(flow):
    flow.status = OnboardingFlow.Status.SKIPPED
    flow.save(update_fields=["status", "updated_at"])


def resume(flow):
    """Pick a skipped flow back up where it was left."""
    if flow.status == OnboardingFlow.Status.SKIPPED:
        flow.status = OnboardingFlow.Status.ACTIVE
        flow.save(update_fields=["status", "updated_at"])


def complete(flow):
    flow.status = OnboardingFlow.Status.COMPLETED
    flow.finished_at = timezone.now()
    flow.save(update_fields=["status", "finished_at", "updated_at"])


def resume_url_after_connect(user, workspace_id):
    """Where to send a user who just connected a channel, or ``None``.

    While the welcome flow waits on its channel step, connecting one moves it
    on to the first post rather than dropping the user on the calendar.
    """
    from apps.social_accounts.models import SocialAccount

    # The post step counts too: "Connect a channel" there leads back to the
    # channel step after the user skipped it, and should return to the post.
    flow = OnboardingFlow.objects.filter(
        user=user,
        workspace_id=workspace_id,
        status=OnboardingFlow.Status.ACTIVE,
        step__in=[OnboardingFlow.Step.CHANNEL, OnboardingFlow.Step.POST],
    ).first()
    # A failed attempt ends in the same redirect; only a real connection moves on.
    if flow is None or not SocialAccount.objects.for_workspace(workspace_id).exists():
        return None
    if flow.step == OnboardingFlow.Step.CHANNEL:
        advance(flow, OnboardingFlow.Step.POST)
    return step_url(flow)


def redirect_url_for_dashboard(request):
    """The welcome step "/" should open, once per session, or ``None``."""
    if request.session.get(SESSION_KEY):
        return None
    flow = active_flow(request.user)
    if flow is None:
        return None
    request.session[SESSION_KEY] = True
    if has_scheduled_post(flow.workspace_id):
        # They scheduled from the composer instead: the guide's job is done.
        complete(flow)
        return None
    return step_url(flow)


def has_scheduled_post(workspace_id):
    """Whether the workspace has a post that was scheduled (or went out) already."""
    from apps.composer.models import PlatformPost

    return PlatformPost.objects.filter(
        post__workspace_id=workspace_id,
        status__in=[
            PlatformPost.Status.SCHEDULED,
            PlatformPost.Status.PUBLISHING,
            PlatformPost.Status.PUBLISHED,
            PlatformPost.Status.FAILED,
        ],
    ).exists()


# ---------------------------------------------------------------------------
# Step 1 helpers
# ---------------------------------------------------------------------------


def initial_timezone(request, workspace):
    """The timezone to preselect: the workspace's own, else the browser's."""
    from zoneinfo import available_timezones

    if workspace.timezone:
        return workspace.timezone
    browser = unquote(request.COOKIES.get("browser_timezone", ""))
    if browser and len(browser) <= 64 and browser in available_timezones():
        return browser
    return workspace.organization.default_timezone or "UTC"


# ---------------------------------------------------------------------------
# Step 2 helpers
# ---------------------------------------------------------------------------


def connectable_platforms(organization_id):
    """Networks the first-channel step offers, and which to spotlight.

    Only networks that can actually be connected here are listed: ones whose
    platform credentials are configured, plus the networks that need none.
    Returns ``(platforms, spotlight)``: ``platforms`` is ``[(value, label)]``
    with the spotlight ones first, and ``spotlight`` the set of values to
    highlight (the networks that work without a platform app, when no network that
    needs one is configured).
    """
    from apps.social_accounts.views import _get_configured_platforms, _get_visible_platform_choices
    from providers import PROVIDER_REGISTRY
    from providers.types import AuthType

    configured = _get_configured_platforms(organization_id)
    open_networks = {
        platform
        for platform, provider_cls in PROVIDER_REGISTRY.items()
        if provider_cls().auth_type in (AuthType.SESSION, AuthType.INSTANCE_OAUTH)
    }
    needs_app = {
        platform
        for platform in configured - open_networks
        if platform in PROVIDER_REGISTRY and not PROVIDER_REGISTRY[platform].user_supplied_credentials
    }
    visible = [(value, label) for value, label in _get_visible_platform_choices() if value in configured]
    spotlight = (
        set()
        if needs_app
        else {value for value, _label in visible if PROVIDER_REGISTRY[value].works_without_platform_app}
    )
    visible.sort(key=lambda choice: choice[0] not in spotlight)
    return visible, spotlight


# ---------------------------------------------------------------------------
# Step 3 helpers
# ---------------------------------------------------------------------------


def example_caption(flow, brand_name):
    template = _EXAMPLE_CAPTIONS.get(flow.account_kind, _DEFAULT_CAPTION)
    return template.format(brand=brand_name)


def suggest_post_time(account):
    """When to propose the first post for ``account``.

    Returns ``(when, is_best_time)``: the channel's next open posting slot
    (the best times it was given on connect) when it has one, otherwise 9:00
    tomorrow in the workspace's timezone.
    """
    from apps.calendar.services import next_available_slots

    tz = zoneinfo.ZoneInfo(account.workspace.effective_timezone or "UTC")
    slots = next_available_slots(account, 1)
    if slots:
        return slots[0].astimezone(tz), True
    tomorrow = timezone.now().astimezone(tz).date() + dt.timedelta(days=1)
    return dt.datetime.combine(tomorrow, dt.time(9, 0), tzinfo=tz), False


def first_post_accounts(workspace):
    from apps.social_accounts.models import SocialAccount

    return list(
        SocialAccount.objects.for_workspace(workspace.id)
        .filter(connection_status=SocialAccount.ConnectionStatus.CONNECTED)
        .order_by("platform", "account_name")
    )
