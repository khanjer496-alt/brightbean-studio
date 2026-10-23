"""Each event type, emitted from the real code path that owns the change."""

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.approvals import services as approval_services
from apps.approvals.comments import create_comment
from apps.composer.models import PlatformPost, Post
from apps.inbox.models import InboxMessage
from apps.inbox.tasks import InboxSyncEngine
from apps.publisher.engine import PublishEngine, _stop_thread
from apps.social_accounts.models import SocialAccount
from apps.webhooks.models import EventType, WebhookDelivery

pytestmark = pytest.mark.django_db


@pytest.fixture
def post(workspace, owner):
    return Post.objects.create(workspace=workspace, author=owner, caption="Hello")


def _make_platform_post(post, account, status):
    return PlatformPost.objects.create(post=post, social_account=account, status=status)


def _events(endpoint):
    return list(endpoint.deliveries.order_by("created_at").values_list("event_type", flat=True))


def _last(endpoint):
    return endpoint.deliveries.order_by("-created_at").first().payload


def test_scheduling_a_platform_post_emits_post_scheduled(endpoint, post, account):
    platform_post = _make_platform_post(post, account, PlatformPost.Status.DRAFT)
    assert _events(endpoint) == []
    platform_post.transition_to("scheduled")
    platform_post.scheduled_at = timezone.now()
    platform_post.save()

    assert _events(endpoint) == ["post.scheduled"]
    data = _last(endpoint)["data"]
    assert data["platform_post_id"] == str(platform_post.id)
    assert data["post_id"] == str(post.id)
    assert data["social_account"]["platform"] == "bluesky"
    assert data["status"] == "scheduled"


def test_saving_a_scheduled_post_again_does_not_re_emit(endpoint, post, account):
    platform_post = _make_platform_post(post, account, PlatformPost.Status.SCHEDULED)
    platform_post.save()
    platform_post.save()
    assert _events(endpoint) == ["post.scheduled"]  # only the creation


def test_a_retry_going_back_to_scheduled_is_not_a_schedule(endpoint, post, account):
    platform_post = _make_platform_post(post, account, PlatformPost.Status.PUBLISHING)
    platform_post.status = PlatformPost.Status.SCHEDULED
    platform_post.save()
    assert _events(endpoint) == []


def test_publishing_emits_post_published(endpoint, post, account):
    platform_post = _make_platform_post(post, account, PlatformPost.Status.PUBLISHING)
    PublishEngine()._mark_confirmed_published(platform_post, "at://did/post/1")

    assert _events(endpoint) == ["post.published"]
    data = _last(endpoint)["data"]
    assert data["external_id"] == "at://did/post/1"
    assert data["published_at"]


def test_a_permanent_failure_emits_post_failed_once(endpoint, post, account):
    platform_post = _make_platform_post(post, account, PlatformPost.Status.PUBLISHING)
    engine = PublishEngine()
    engine._fail_permanently(platform_post, "boom", user_message="It did not go out.")
    engine._fail_permanently(platform_post, "boom", user_message="It did not go out.")  # already settled

    assert _events(endpoint) == ["post.failed"]
    assert _last(endpoint)["data"]["error"] == "It did not go out."


def test_a_stopped_thread_emits_post_partially_published(endpoint, post, account):
    platform_post = _make_platform_post(post, account, PlatformPost.Status.PUBLISHED)
    events_before = _events(endpoint)
    _stop_thread(platform_post, None, "The network refused part 3.")

    assert _events(endpoint)[len(events_before) :] == ["post.partially_published"]
    assert "refused part 3" in _last(endpoint)["data"]["error"]


def test_final_approval_emits_post_approved(endpoint, post, account, workspace, owner, manager):
    _make_platform_post(post, account, PlatformPost.Status.PENDING_REVIEW)
    approval_services.approve_post(post, manager, workspace, "Looks good")

    assert _events(endpoint) == ["post.approved"]
    data = _last(endpoint)["data"]
    assert data["post_id"] == str(post.id)
    assert data["actor"]["email"] == "manager@example.com"
    assert data["comment"] == "Looks good"
    assert len(data["platform_post_ids"]) == 1
    assert data["social_accounts"][0]["platform"] == "bluesky"


def test_approval_by_the_author_still_emits(endpoint, post, account, workspace, owner):
    _make_platform_post(post, account, PlatformPost.Status.PENDING_REVIEW)
    approval_services.approve_post(post, owner, workspace)  # the author gets no notification, the webhook still fires
    assert _events(endpoint) == ["post.approved"]


def test_the_first_stage_of_a_two_stage_approval_is_not_post_approved(endpoint, post, account, workspace, manager):
    workspace.approval_workflow_mode = "required_internal_and_client"
    workspace.save()
    _make_platform_post(post, account, PlatformPost.Status.PENDING_REVIEW)
    approval_services.approve_post(post, manager, workspace)
    assert _events(endpoint) == []


def test_requesting_changes_emits_post_changes_requested(endpoint, post, account, workspace, manager):
    _make_platform_post(post, account, PlatformPost.Status.PENDING_REVIEW)
    approval_services.request_changes(post, manager, workspace, "Shorter please")

    assert _events(endpoint) == ["post.changes_requested"]
    assert _last(endpoint)["data"]["comment"] == "Shorter please"


def test_creating_a_comment_emits_comment_created(endpoint, post, owner, manager):
    comment = create_comment(post, manager, "Can we soften this?", "internal")

    assert _events(endpoint) == ["comment.created"]
    data = _last(endpoint)["data"]
    assert data["comment_id"] == str(comment.id)
    assert data["visibility"] == "internal"
    assert data["author"]["email"] == "manager@example.com"


def _inbox_message(workspace, account, **kwargs):
    return InboxMessage.objects.create(
        workspace=workspace,
        social_account=account,
        platform_message_id="m-1",
        sender_name="Fan",
        body="Love this!",
        received_at=timezone.now(),
        **kwargs,
    )


def test_a_new_inbox_message_emits_inbox_message_received(endpoint, workspace, account):
    message = _inbox_message(workspace, account)
    InboxSyncEngine()._notify_new_message(message)

    assert _events(endpoint) == ["inbox.message.received"]
    data = _last(endpoint)["data"]
    assert data["message_id"] == str(message.id)
    assert data["sender_name"] == "Fan" and data["body"] == "Love this!"


def test_a_silent_backfill_emits_nothing(endpoint, workspace, account):
    from providers.types import InboxMessage as ProviderMessage

    engine = InboxSyncEngine()
    engine._upsert_message(
        account,
        ProviderMessage(
            platform_message_id="old-1",
            sender_id="u-1",
            sender_name="Old Fan",
            text="From last year",
            timestamp=timezone.now(),
        ),
        notify=False,
    )
    assert InboxMessage.objects.filter(platform_message_id="old-1").exists()
    assert _events(endpoint) == []


def test_losing_the_connection_emits_account_disconnected_once(endpoint, account):
    account.connection_status = SocialAccount.ConnectionStatus.ERROR
    account.last_error = "Token revoked"
    account.save(update_fields=["connection_status", "last_error"])
    account.save(update_fields=["connection_status", "last_error"])  # still broken: no second event

    assert _events(endpoint) == ["account.disconnected"]
    data = _last(endpoint)["data"]
    assert data["id"] == str(account.id)
    assert data["reason"] == "Token revoked"
    assert data["connection_status"] == "error"


def test_a_healthy_account_save_emits_nothing(endpoint, account):
    account.account_name = "Renamed"
    account.save()
    assert _events(endpoint) == []


def test_the_disconnect_view_emits_account_disconnected(endpoint, client, owner, account, workspace):
    client.force_login(owner)
    response = client.post(
        reverse("social_accounts:disconnect", kwargs={"workspace_id": workspace.id, "account_id": account.id})
    )
    assert response.status_code in (200, 302)
    assert not SocialAccount.objects.filter(pk=account.pk).exists()
    assert _events(endpoint) == ["account.disconnected"]
    data = _last(endpoint)["data"]
    assert data["removed"] is True and data["connection_status"] == "disconnected"


def test_removing_an_account_that_already_broke_does_not_fire_again(endpoint, client, owner, account, workspace):
    account.connection_status = SocialAccount.ConnectionStatus.ERROR
    account.save(update_fields=["connection_status"])
    client.force_login(owner)
    client.post(reverse("social_accounts:disconnect", kwargs={"workspace_id": workspace.id, "account_id": account.id}))
    assert not SocialAccount.objects.filter(pk=account.pk).exists()
    assert _events(endpoint) == ["account.disconnected"]
    assert _last(endpoint)["data"]["removed"] is False


def test_a_sync_publish_carries_the_permalink(endpoint, post, account):
    from unittest.mock import patch

    platform_post = _make_platform_post(post, account, PlatformPost.Status.PUBLISHING)
    result = {"success": True, "platform_post_id": "at://did/post/9", "url": "https://bsky.app/p/9", "response": {}}
    with patch.object(PublishEngine, "_dispatch_to_provider", return_value=result):
        PublishEngine()._publish_platform_post(platform_post)

    assert _events(endpoint) == ["post.published"]
    data = _last(endpoint)["data"]
    assert data["permalink"] == "https://bsky.app/p/9" and data["external_id"] == "at://did/post/9"


def test_a_publish_we_could_not_record_emits_post_failed(endpoint, post, account):
    from unittest.mock import patch

    from apps.social_accounts.error_messages import PUBLISH_AMBIGUOUS_MESSAGE

    platform_post = _make_platform_post(post, account, PlatformPost.Status.PUBLISHING)
    result = {"success": True, "platform_post_id": "at://did/post/9", "response": {}}
    with (
        patch.object(PublishEngine, "_dispatch_to_provider", return_value=result),
        patch.object(PlatformPost, "save", side_effect=RuntimeError("db down")),
    ):
        PublishEngine()._publish_platform_post(platform_post)

    platform_post.refresh_from_db()
    assert platform_post.status == PlatformPost.Status.FAILED
    assert _events(endpoint) == ["post.failed"]
    data = _last(endpoint)["data"]
    assert data["error"] == PUBLISH_AMBIGUOUS_MESSAGE and data["external_id"] == "at://did/post/9"


def test_a_failing_payload_builder_never_breaks_the_caller(endpoint, post, account):
    platform_post = _make_platform_post(post, account, PlatformPost.Status.PUBLISHING)
    platform_post.social_account = None
    from apps.webhooks import events

    assert events.post_failed(platform_post) is None
    assert WebhookDelivery.objects.count() == 0


def test_unsubscribed_event_types_are_skipped(endpoint, post, account):
    endpoint.event_types = [EventType.POST_FAILED.value]
    endpoint.save()
    platform_post = _make_platform_post(post, account, PlatformPost.Status.PUBLISHING)
    PublishEngine()._mark_confirmed_published(platform_post, "x")
    assert _events(endpoint) == []


def test_other_workspaces_do_not_see_the_event(endpoint, org, account):
    from apps.workspaces.models import Workspace

    other = Workspace.objects.create(organization=org, name="Other")
    other_account = SocialAccount.objects.create(
        workspace=other,
        platform="bluesky",
        account_platform_id="b2",
        account_name="Other",
        connection_status="connected",
    )
    other_post = Post.objects.create(workspace=other, caption="x")
    _make_platform_post(other_post, other_account, PlatformPost.Status.SCHEDULED)
    assert _events(endpoint) == []
