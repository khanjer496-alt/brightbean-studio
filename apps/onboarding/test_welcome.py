"""The first-run welcome flow: steps, skip and resume, branching, permissions."""

import datetime as dt
from unittest.mock import patch

import pytest
from allauth.account.signals import user_signed_up
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.calendar.models import PostingSlot
from apps.composer.models import PlatformPost
from apps.members.models import WorkspaceMembership
from apps.onboarding.flow import connectable_platforms, resume_url_after_connect, suggest_post_time
from apps.onboarding.models import OnboardingFlow

pytestmark = pytest.mark.django_db

Role = WorkspaceMembership.WorkspaceRole


def url(name, workspace):
    return reverse(f"onboarding:{name}", kwargs={"workspace_id": workspace.id})


def future(days=2, hour=10):
    day = timezone.now().date() + dt.timedelta(days=days)
    return day.isoformat(), f"{hour:02d}:00"


# --- starting the flow ------------------------------------------------------------


def test_signup_starts_the_flow_for_the_new_owner(rf, owner, workspace):
    request = rf.get("/")
    request.session = {}
    user_signed_up.send(sender=User, request=request, user=owner)
    flow = OnboardingFlow.objects.get(user=owner)
    assert flow.workspace == workspace
    assert flow.status == OnboardingFlow.Status.ACTIVE
    assert flow.step == OnboardingFlow.Step.WORKSPACE


def test_existing_users_have_no_flow(owner):
    assert not OnboardingFlow.objects.exists()


def test_dashboard_opens_welcome_once_per_session(owner_client, flow, workspace):
    first = owner_client.get(reverse("dashboard"))
    assert first.status_code == 302
    assert first.url == url("welcome_workspace", workspace)
    second = owner_client.get(reverse("dashboard"))
    assert second.url == reverse("calendar:calendar", kwargs={"workspace_id": workspace.id})


def test_dashboard_without_a_flow_goes_straight_to_the_calendar(owner_client, workspace):
    assert owner_client.get(reverse("dashboard")).url == reverse(
        "calendar:calendar", kwargs={"workspace_id": workspace.id}
    )


# --- step 1 ---------------------------------------------------------------------------


def test_workspace_step_renders_with_prefill(owner_client, flow, workspace):
    html = owner_client.get(url("welcome_workspace", workspace)).content.decode()
    assert 'value="My Workspace"' in html
    assert "Step 1 of 3" in html
    assert "Intl.DateTimeFormat" in html  # timezone pre-filled from the browser
    assert 'name="account_kind"' in html and "Skip for now" in html


def test_workspace_step_saves_and_advances(owner_client, flow, workspace):
    response = owner_client.post(
        url("welcome_workspace", workspace),
        {"name": "Acme Studio", "timezone": "Europe/Berlin", "account_kind": "business"},
    )
    assert response.url == url("welcome_channel", workspace)
    workspace.refresh_from_db()
    flow.refresh_from_db()
    assert (workspace.name, workspace.timezone) == ("Acme Studio", "Europe/Berlin")
    assert flow.account_kind == "business"
    assert flow.step == OnboardingFlow.Step.CHANNEL


@pytest.mark.parametrize(
    "data,field",
    [
        ({"name": "", "timezone": "UTC", "account_kind": "creator"}, "Give your workspace a name"),
        ({"name": "X", "timezone": "Mars/Base", "account_kind": "creator"}, "Choose a timezone"),
        ({"name": "X", "timezone": "UTC", "account_kind": "wizard"}, "Choose one of the options"),
    ],
)
def test_workspace_step_validates_and_moves_focus_to_the_errors(owner_client, flow, workspace, data, field):
    response = owner_client.post(url("welcome_workspace", workspace), data)
    html = response.content.decode()
    assert response.status_code == 200
    assert field in html
    assert 'role="alert"' in html and "$el.focus()" in html
    flow.refresh_from_db()
    assert flow.step == OnboardingFlow.Step.WORKSPACE


def test_agency_gets_a_client_nudge_that_creator_does_not(owner_client, flow, workspace):
    assert "client portal" in owner_client.get(url("welcome_workspace", workspace)).content.decode()
    owner_client.post(url("welcome_workspace", workspace), {"name": "A", "timezone": "UTC", "account_kind": "agency"})
    flow.refresh_from_db()
    assert flow.is_agency
    done = owner_client.get(url("welcome_done", workspace)).content.decode()
    assert "Set up your first client" in done


def test_creator_is_not_nudged_about_clients_on_the_done_screen(owner_client, flow, workspace):
    owner_client.post(url("welcome_workspace", workspace), {"name": "A", "timezone": "UTC", "account_kind": "creator"})
    assert "Set up your first client" not in owner_client.get(url("welcome_done", workspace)).content.decode()


# --- skip and resume ------------------------------------------------------------------


def test_skip_leaves_the_flow_and_stops_the_redirect(owner_client, flow, workspace):
    response = owner_client.post(url("welcome_skip", workspace))
    assert response.url == reverse("calendar:calendar", kwargs={"workspace_id": workspace.id})
    flow.refresh_from_db()
    assert flow.status == OnboardingFlow.Status.SKIPPED
    session = owner_client.session
    session.pop("onboarding_welcomed", None)  # a new session
    session.save()
    fresh = owner_client.get(reverse("dashboard"))
    assert fresh.url == reverse("calendar:calendar", kwargs={"workspace_id": workspace.id})


def test_skipped_flow_resumes_at_the_saved_step(owner_client, flow, workspace):
    owner_client.post(url("welcome_workspace", workspace), {"name": "A", "timezone": "UTC", "account_kind": "creator"})
    owner_client.post(url("welcome_skip", workspace))
    response = owner_client.get(url("welcome", workspace))
    assert response.url == url("welcome_channel", workspace)
    flow.refresh_from_db()
    assert flow.status == OnboardingFlow.Status.ACTIVE


def test_cannot_jump_ahead_of_the_current_step(owner_client, flow, workspace):
    assert owner_client.get(url("welcome_post", workspace)).url == url("welcome_workspace", workspace)


def test_can_go_back_to_an_earlier_step_without_losing_progress(owner_client, flow, workspace):
    owner_client.post(url("welcome_workspace", workspace), {"name": "A", "timezone": "UTC", "account_kind": "creator"})
    assert owner_client.get(url("welcome_workspace", workspace)).status_code == 200
    owner_client.post(url("welcome_workspace", workspace), {"name": "B", "timezone": "UTC", "account_kind": "creator"})
    flow.refresh_from_db()
    assert flow.step == OnboardingFlow.Step.CHANNEL


def test_completed_flow_shows_the_done_screen(owner_client, flow, workspace):
    flow.status = OnboardingFlow.Status.COMPLETED
    flow.save()
    assert owner_client.get(url("welcome", workspace)).url == url("welcome_done", workspace)


# --- step 2 ---------------------------------------------------------------------------


@pytest.fixture
def at_channel_step(flow):
    flow.step = OnboardingFlow.Step.CHANNEL
    flow.save()
    return flow


def test_only_networks_that_can_connect_are_listed(owner_client, at_channel_step, workspace, settings):
    settings.PLATFORM_CREDENTIALS_FROM_ENV = {}
    html = owner_client.get(url("welcome_channel", workspace)).content.decode()
    assert "Bluesky" in html and "Mastodon" in html
    assert "Not Configured" not in html
    assert "TikTok" not in html and "Pinterest" not in html


def test_bluesky_and_mastodon_are_highlighted_when_nothing_else_is_configured(
    owner_client, at_channel_step, workspace, settings
):
    settings.PLATFORM_CREDENTIALS_FROM_ENV = {}
    html = owner_client.get(url("welcome_channel", workspace)).content.decode()
    assert html.count(">Works right away</span>") == 2
    assert "only need your own login" in html


def test_no_highlight_once_a_platform_app_is_configured(owner_client, at_channel_step, workspace, settings):
    settings.PLATFORM_CREDENTIALS_FROM_ENV = {"linkedin_personal": {"client_id": "id", "client_secret": "secret"}}
    platforms, spotlight = connectable_platforms(workspace.organization_id)
    assert "linkedin_personal" in dict(platforms)
    assert spotlight == set()
    assert "Works right away" not in owner_client.get(url("welcome_channel", workspace)).content.decode()


def test_spotlighted_networks_come_first(workspace, settings):
    settings.PLATFORM_CREDENTIALS_FROM_ENV = {}
    platforms, spotlight = connectable_platforms(workspace.organization_id)
    assert {value for value, _ in platforms[: len(spotlight)]} == spotlight


def test_connecting_a_channel_moves_on_to_the_first_post(owner, at_channel_step, workspace, account):
    assert resume_url_after_connect(owner, workspace.id) == url("welcome_post", workspace)
    at_channel_step.refresh_from_db()
    assert at_channel_step.step == OnboardingFlow.Step.POST


def test_a_failed_connect_does_not_advance(owner, at_channel_step, workspace):
    assert resume_url_after_connect(owner, workspace.id) is None


def test_channel_step_can_be_skipped(owner_client, at_channel_step, workspace):
    response = owner_client.post(url("welcome_channel_next", workspace))
    assert response.url == url("welcome_post", workspace)


# --- step 3 ---------------------------------------------------------------------------


@pytest.fixture
def at_post_step(flow):
    flow.step = OnboardingFlow.Step.POST
    flow.save()
    return flow


def test_post_step_without_a_channel_asks_to_connect_one(owner_client, at_post_step, workspace):
    html = owner_client.get(url("welcome_post", workspace)).content.decode()
    assert "No channel connected yet" in html and "Connect a channel" in html
    assert url("sample_create", workspace) in html  # not a dead end: the example workspace is offered
    assert "We wrote a friendly example" not in html
    assert "Schedule post" not in html


def test_post_step_is_prefilled_with_an_example_and_best_time(owner_client, at_post_step, workspace, account):
    PostingSlot.objects.create(social_account=account, day_of_week=0, time="09:38")
    html = owner_client.get(url("welcome_post", workspace)).content.decode()
    assert "Hello, world!" in html
    assert "Best time for this channel" in html
    assert "09:38" in html


def test_without_slots_the_suggestion_is_tomorrow_morning(workspace, account):
    when, is_best = suggest_post_time(account)
    assert is_best is False
    assert (when.hour, when.minute) == (9, 0)
    assert when > timezone.now()


def test_example_caption_follows_the_account_kind(owner_client, at_post_step, workspace, account):
    at_post_step.account_kind = "business"
    at_post_step.save()
    assert "What would you like to see from us" in owner_client.get(url("welcome_post", workspace)).content.decode()


def test_writing_with_ai_is_hidden_without_a_key(owner_client, at_post_step, workspace, account):
    with patch("apps.onboarding.views_welcome.ai_available", return_value=False):
        html = owner_client.get(url("welcome_post", workspace)).content.decode()
    assert "Write with AI" not in html


def test_writing_with_ai_is_offered_when_configured(owner_client, at_post_step, workspace, account):
    with patch("apps.onboarding.views_welcome.ai_available", return_value=True):
        html = owner_client.get(url("welcome_post", workspace)).content.decode()
    assert "Write with AI" in html


def test_ai_endpoint_404s_without_a_key(owner_client, flow, workspace):
    with patch("apps.onboarding.views_welcome.ai_available", return_value=False):
        assert owner_client.post(url("welcome_ai_write", workspace), {"prompt": "x"}).status_code == 404


def test_ai_endpoint_returns_the_drafted_caption(owner_client, flow, workspace, account):
    with (
        patch("apps.onboarding.views_welcome.ai_available", return_value=True),
        patch("apps.onboarding.views_welcome.write_caption", return_value="Fresh AI caption") as write,
    ):
        response = owner_client.post(
            url("welcome_ai_write", workspace), {"prompt": "launch day", "account": str(account.id)}
        )
    assert "Fresh AI caption" in response.content.decode()
    assert write.call_args.args[1] == "launch day"


def test_ai_endpoint_asks_for_a_prompt(owner_client, flow, workspace):
    with patch("apps.onboarding.views_welcome.ai_available", return_value=True):
        response = owner_client.post(url("welcome_ai_write", workspace), {"prompt": ""})
    assert "Say what the post is about" in response.content.decode()


def test_scheduling_the_first_post_completes_the_flow(owner_client, at_post_step, workspace, account):
    date, time = future()
    response = owner_client.post(
        url("welcome_post", workspace),
        {"account": str(account.id), "caption": "My first post", "schedule_date": date, "schedule_time": time},
    )
    assert response.url == url("welcome_done", workspace)
    pp = PlatformPost.objects.get(post__workspace=workspace)
    assert pp.status == "scheduled" and pp.post.caption == "My first post"
    at_post_step.refresh_from_db()
    assert at_post_step.status == OnboardingFlow.Status.COMPLETED
    done = owner_client.get(url("welcome_done", workspace)).content.decode()
    assert "Your first post is scheduled" in done and "My first post" in done


def test_save_as_draft(owner_client, at_post_step, workspace, account):
    owner_client.post(
        url("welcome_post", workspace), {"account": str(account.id), "caption": "Later", "action": "draft"}
    )
    assert PlatformPost.objects.get(post__workspace=workspace).status == "draft"
    assert "draft is saved" in owner_client.get(url("welcome_done", workspace)).content.decode()


@pytest.mark.parametrize(
    "overrides,message",
    [
        ({"caption": ""}, "Write a caption first"),
        ({"schedule_date": "2001-01-01"}, "future"),
        ({"schedule_date": "nope"}, "Pick a date and time"),
        ({"account": "00000000-0000-0000-0000-000000000000"}, "Choose a channel"),
    ],
)
def test_first_post_validation(owner_client, at_post_step, workspace, account, overrides, message):
    date, time = future()
    data = {"account": str(account.id), "caption": "Hi", "schedule_date": date, "schedule_time": time, **overrides}
    response = owner_client.post(url("welcome_post", workspace), data)
    assert response.status_code == 200 and message in response.content.decode()
    assert not PlatformPost.objects.exists()


# --- permissions ----------------------------------------------------------------------


@pytest.mark.parametrize("name", ["welcome", "welcome_workspace", "welcome_channel", "welcome_post", "welcome_done"])
def test_invited_members_cannot_open_the_owners_flow(client, flow, workspace, make_member, name):
    editor = make_member("editor@example.com", Role.EDITOR)
    client.force_login(editor)
    assert client.get(url(name, workspace)).status_code == 403


def test_invited_members_cannot_skip_the_owners_flow(client, flow, workspace, make_member):
    client.force_login(make_member("editor@example.com", Role.MANAGER))
    assert client.post(url("welcome_skip", workspace)).status_code == 403
    flow.refresh_from_db()
    assert flow.status == OnboardingFlow.Status.ACTIVE


def test_invited_members_are_never_sent_into_the_flow(client, flow, workspace, make_member):
    editor = make_member("editor@example.com", Role.EDITOR)
    client.force_login(editor)
    assert client.get(reverse("dashboard")).url == reverse("calendar:calendar", kwargs={"workspace_id": workspace.id})


def test_an_owner_without_a_flow_gets_a_404(owner_client, workspace):
    assert owner_client.get(url("welcome_workspace", workspace)).status_code == 404


def test_anonymous_users_are_sent_to_login(client, workspace):
    assert client.get(url("welcome_workspace", workspace)).status_code == 302


def test_flow_pages_use_the_focused_layout_without_inline_styles(owner_client, flow, workspace):
    html = owner_client.get(url("welcome_workspace", workspace)).content.decode()
    main = html[html.index('id="welcome-main"') :]
    main = main[: main.index("</section>")]
    assert "style=" not in main
    assert "sidebar" not in main
    assert 'aria-current="step"' in html and 'id="welcome-heading"' in html


# --- review fixes ---------------------------------------------------------------------


def test_connecting_from_the_post_step_returns_to_it(owner, at_post_step, workspace, account):
    # Skipped the channel step, then used "Connect a channel" on the post step.
    assert resume_url_after_connect(owner, workspace.id) == url("welcome_post", workspace)
    at_post_step.refresh_from_db()
    assert at_post_step.step == OnboardingFlow.Step.POST


def test_account_kind_is_optional(owner_client, flow, workspace):
    response = owner_client.post(url("welcome_workspace", workspace), {"name": "Solo", "timezone": "UTC"})
    assert response.url == url("welcome_channel", workspace)
    flow.refresh_from_db()
    assert flow.account_kind == ""


def test_caption_over_the_channel_limit_is_refused(owner_client, at_post_step, workspace, account):
    date, time = future()
    data = {"account": str(account.id), "caption": "x" * 301, "schedule_date": date, "schedule_time": time}
    response = owner_client.post(url("welcome_post", workspace), data)
    assert "allows 300 characters" in response.content.decode()
    assert not PlatformPost.objects.exists()


def test_schedule_is_the_default_submit_button(owner_client, at_post_step, workspace, account):
    html = owner_client.get(url("welcome_post", workspace)).content.decode()
    # Enter in a field submits with the first submit button in the form.
    assert html.index('value="schedule"') < html.index('value="draft"')


def test_done_screen_shows_the_time_in_the_workspace_timezone(owner_client, at_post_step, workspace, account):
    workspace.timezone = "Asia/Dubai"
    workspace.save(update_fields=["timezone"])
    date, _time = future()
    owner_client.post(
        url("welcome_post", workspace),
        {"account": str(account.id), "caption": "Hi", "schedule_date": date, "schedule_time": "09:00"},
    )
    assert "9:00 AM" in owner_client.get(url("welcome_done", workspace)).content.decode()


def test_dashboard_completes_the_flow_when_a_post_was_scheduled_elsewhere(
    owner_client, at_post_step, workspace, account
):
    from apps.composer.services import create_post

    create_post(
        workspace=workspace,
        social_account=account,
        caption="From the composer",
        scheduled_at=timezone.now() + dt.timedelta(days=1),
        status="scheduled",
    )
    assert owner_client.get(reverse("dashboard")).url == reverse(
        "calendar:calendar", kwargs={"workspace_id": workspace.id}
    )
    at_post_step.refresh_from_db()
    assert at_post_step.status == OnboardingFlow.Status.COMPLETED


def test_invited_workspace_owner_never_gets_the_new_owner_flow(rf, workspace, make_member):
    invited = make_member("co-owner@example.com", Role.OWNER)
    request = rf.get("/")
    request.session = {}
    user_signed_up.send(sender=User, request=request, user=invited)
    assert not OnboardingFlow.objects.filter(user=invited).exists()
