"""The example workspace: created and removed in one click, and never publishable."""

import datetime as dt

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.billing.entitlements import channel_count, workspace_count
from apps.composer.models import PlatformPost, Post
from apps.composer.services import create_post, transition_platform_post
from apps.members.models import WorkspaceMembership
from apps.onboarding import sample
from apps.onboarding.models import SampleWorkspace
from apps.publisher.engine import PublishEngine
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db

Role = WorkspaceMembership.WorkspaceRole


def create_url(workspace):
    return reverse("onboarding:sample_create", kwargs={"workspace_id": workspace.id})


@pytest.fixture
def example(owner, workspace):
    return sample.create_sample_workspace(user=owner, source_workspace=workspace)


def test_toggle_is_off_by_default(workspace):
    assert not SampleWorkspace.objects.exists()
    assert sample.sample_workspace_for(workspace.organization_id) is None


def test_create_builds_a_marked_workspace_with_sample_drafts(example, owner, workspace):
    assert example.id != workspace.id and example.organization_id == workspace.organization_id
    assert example.sample_marker.created_by == owner
    posts = Post.objects.filter(workspace=example)
    assert posts.count() >= 5
    assert all(p.title.startswith("Sample: ") and "sample" in p.tags for p in posts)
    assert set(PlatformPost.objects.filter(post__workspace=example).values_list("status", flat=True)) == {"draft"}
    assert all(p.scheduled_at > timezone.now() for p in posts)


def test_create_is_idempotent(example, owner, workspace):
    again = sample.create_sample_workspace(user=owner, source_workspace=workspace)
    assert again.id == example.id
    assert Workspace.objects.filter(sample_marker__isnull=False).count() == 1


def test_sample_channel_is_disconnected(example):
    account = SocialAccount.objects.get(workspace=example)
    assert account.connection_status == SocialAccount.ConnectionStatus.DISCONNECTED


def test_create_view_opens_the_example_calendar_and_remove_cleans_up(owner_client, owner, workspace):
    response = owner_client.post(create_url(workspace))
    example = Workspace.objects.get(sample_marker__isnull=False)
    assert response.url.startswith(reverse("calendar:calendar", kwargs={"workspace_id": example.id}))
    owner.refresh_from_db()
    assert owner.last_workspace_id == example.id

    removed = owner_client.post(reverse("onboarding:sample_remove", kwargs={"workspace_id": example.id}))
    assert removed.url == reverse("calendar:calendar", kwargs={"workspace_id": workspace.id})
    assert not Workspace.objects.filter(pk=example.pk).exists()
    assert not Post.objects.filter(title__startswith="Sample: ").exists()
    owner.refresh_from_db()
    assert owner.last_workspace_id == workspace.id


def test_example_calendar_shows_sample_posts_and_a_banner(owner_client, example):
    html = owner_client.get(
        reverse("calendar:calendar", kwargs={"workspace_id": example.id}), {"mode": "calendar", "view": "week"}
    ).content.decode()
    assert "Example workspace." in html and "Remove example" in html
    drafts = owner_client.get(
        reverse("calendar:publish_tab_drafts", kwargs={"workspace_id": example.id})
    ).content.decode()
    assert "Sample: Welcome post" in drafts


def test_banner_is_only_in_the_example(owner_client, workspace, example):
    html = owner_client.get(reverse("calendar:calendar", kwargs={"workspace_id": workspace.id})).content.decode()
    assert "Remove example" not in html


def test_checklist_is_not_shown_in_the_example(owner_client, example):
    html = owner_client.get(reverse("calendar:calendar", kwargs={"workspace_id": example.id})).content.decode()
    assert 'id="onboarding-checklist"' not in html


# --- never publishable ----------------------------------------------------------------


def _sample_platform_post(example):
    return PlatformPost.objects.filter(post__workspace=example).first()


def test_sample_post_cannot_transition_to_scheduled_or_publishing(example):
    pp = _sample_platform_post(example)
    assert not pp.can_transition_to("scheduled")
    assert not pp.can_transition_to("publishing")
    with pytest.raises(ValueError):
        transition_platform_post(pp, "scheduled", scheduled_at=timezone.now() + dt.timedelta(days=1))
    pp.refresh_from_db()
    assert pp.status == "draft"


def test_a_real_draft_still_schedules(workspace, account):
    post = Post.objects.create(workspace=workspace, caption="x")
    pp = PlatformPost.objects.create(post=post, social_account=account, status="draft")
    assert pp.can_transition_to("scheduled")


def test_create_post_service_refuses_to_schedule_in_the_example(example, owner):
    account = SocialAccount.objects.get(workspace=example)
    with pytest.raises(ValueError, match="never published"):
        create_post(
            workspace=example,
            social_account=account,
            caption="x",
            scheduled_at=timezone.now() + dt.timedelta(days=1),
            status="scheduled",
        )


def test_composer_refuses_schedule_actions_in_the_example(owner_client, example):
    url = reverse("composer:save_post", kwargs={"workspace_id": example.id})
    response = owner_client.post(url, {"action": "schedule", "caption": "x"})
    assert response.status_code == 400
    assert "never published" in response.content.decode()
    assert not Post.objects.filter(workspace=example, caption="x").exists()


def test_publisher_skips_sample_posts_even_if_forced_to_scheduled(example):
    """Belt and braces: a row forced to ``scheduled`` in the database still never goes out."""
    pp = _sample_platform_post(example)
    PlatformPost.objects.filter(pk=pp.pk).update(
        status="scheduled", scheduled_at=timezone.now() - dt.timedelta(hours=1)
    )
    assert pp.pk not in {due.pk for due in PublishEngine()._get_due_platform_posts()}


def test_publisher_still_sees_real_due_posts(workspace, account):
    post = Post.objects.create(workspace=workspace, caption="x")
    pp = PlatformPost.objects.create(
        post=post, social_account=account, status="scheduled", scheduled_at=timezone.now() - dt.timedelta(hours=1)
    )
    assert pp.pk in {due.pk for due in PublishEngine()._get_due_platform_posts()}


def test_billing_ignores_the_example(workspace, account, example):
    org = workspace.organization
    assert channel_count(org) == 1
    assert workspace_count(org) == 1


# --- permissions ----------------------------------------------------------------------


def test_only_the_owner_can_create_the_example(client, workspace, make_member):
    client.force_login(make_member("editor@example.com", Role.MANAGER))
    assert client.post(create_url(workspace)).status_code == 403
    assert not SampleWorkspace.objects.exists()


def test_create_requires_post(owner_client, workspace):
    assert owner_client.get(create_url(workspace)).status_code == 405


def test_remove_404s_on_a_real_workspace(owner_client, workspace):
    assert (
        owner_client.post(reverse("onboarding:sample_remove", kwargs={"workspace_id": workspace.id})).status_code == 404
    )
    assert Workspace.objects.filter(pk=workspace.pk).exists()


def test_empty_calendar_offers_the_example_to_owners_only(owner_client, workspace, client, make_member):
    html = owner_client.get(
        reverse("calendar:calendar", kwargs={"workspace_id": workspace.id}), {"mode": "list"}
    ).content.decode()
    assert "Show me an example" in html and create_url(workspace) in html
    client.force_login(make_member("editor@example.com", Role.EDITOR))
    html = client.get(
        reverse("calendar:calendar", kwargs={"workspace_id": workspace.id}), {"mode": "list"}
    ).content.decode()
    assert "Show me an example" not in html


def test_channels_cannot_be_connected_into_the_example(example):
    from django.core.exceptions import PermissionDenied

    from apps.social_accounts.views import _create_or_update_account
    from providers.types import AccountProfile

    with pytest.raises(PermissionDenied):
        _create_or_update_account(
            workspace_id=example.id,
            platform="bluesky",
            profile=AccountProfile(platform_id="x", name="x", handle="x"),
            access_token="t",
        )
    assert SocialAccount.objects.filter(workspace=example).count() == 1  # only the placeholder


def test_example_sidebar_offers_no_reconnect_or_connect(owner_client, example):
    html = owner_client.get(reverse("calendar:calendar", kwargs={"workspace_id": example.id})).content.decode()
    assert "Reconnect" not in html


def test_no_hint_callout_inside_the_example(owner_client, example):
    html = owner_client.get(reverse("calendar:calendar", kwargs={"workspace_id": example.id})).content.decode()
    assert 'data-hint="calendar"' not in html


def test_remove_deletes_uploaded_files(owner, workspace, example, django_capture_on_commit_callbacks):
    from django.core.files.base import ContentFile

    from apps.media_library.models import MediaAsset

    asset = MediaAsset(organization=workspace.organization, workspace=example, media_type="image", filename="a.png")
    asset.file.save("sample-upload.png", ContentFile(b"png"), save=False)
    asset.save()
    storage, name = asset.file.storage, asset.file.name
    assert storage.exists(name)
    with django_capture_on_commit_callbacks(execute=True):
        sample.remove_sample_workspace(user=owner, organization_id=workspace.organization_id)
    assert not storage.exists(name)
    assert not MediaAsset.objects.filter(pk=asset.pk).exists()


def test_oauth_agents_never_act_in_the_example(owner, workspace, example):
    from apps.api.auth import _resolve_active_membership

    owner.last_workspace_id = example.id
    owner.save(update_fields=["last_workspace_id"])
    assert _resolve_active_membership(owner).workspace_id == workspace.id


def test_api_keys_cannot_be_issued_for_the_example(workspace, example):
    from apps.api_keys.views import _initial_issuance_context

    workspaces = list(_initial_issuance_context(workspace.organization, None)["workspaces"])
    assert workspace in workspaces and example not in workspaces
