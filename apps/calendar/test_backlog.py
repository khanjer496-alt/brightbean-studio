"""Spreading out what piled up while the worker was down."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.accounts.models import User
from apps.calendar.models import Queue, QueueEntry
from apps.calendar.services import stagger_overdue_backlog
from apps.composer.models import PlatformPost, Post
from apps.organizations.models import Organization
from apps.publisher.engine import PublishEngine
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db


@pytest.fixture
def workspace():
    return Workspace.objects.create(organization=Organization.objects.create(name="Org"), name="WS")


@pytest.fixture
def author():
    return User.objects.create_user(email="a@example.com", password="x", name="A", tos_accepted_at=timezone.now())


def make_account(workspace, handle="one", **extra):
    return SocialAccount.objects.create(
        workspace=workspace,
        platform="facebook",
        account_platform_id=handle,
        account_name=handle,
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        **extra,
    )


def overdue(workspace, author, account, count, *, late=timedelta(hours=3), status=PlatformPost.Status.SCHEDULED):
    """``count`` posts due ``late`` ago, oldest first."""
    rows = []
    now = timezone.now()
    for index in range(count):
        due = now - late + timedelta(minutes=index)
        post = Post.objects.create(workspace=workspace, caption=f"post {index}", author=author, scheduled_at=due)
        rows.append(PlatformPost.objects.create(post=post, social_account=account, status=status, scheduled_at=due))
    return rows


def test_a_backlog_is_sent_a_minute_apart_in_the_order_it_was_due(workspace, author):
    account = make_account(workspace)
    rows = overdue(workspace, author, account, 5)
    before = timezone.now()

    assert stagger_overdue_backlog() == 5

    times = []
    for pp in rows:
        pp.refresh_from_db()
        assert pp.status == PlatformPost.Status.SCHEDULED
        times.append(pp.scheduled_at)
    assert [b - a for a, b in zip(times, times[1:], strict=False)] == [timedelta(minutes=1)] * 4
    assert before <= times[0] <= timezone.now()
    for pp in rows:
        assert pp.post.__class__.objects.get(pk=pp.post_id).scheduled_at == pp.scheduled_at


def test_only_the_first_of_a_backlog_is_due_straight_away(workspace, author):
    account = make_account(workspace)
    rows = overdue(workspace, author, account, 5)
    stagger_overdue_backlog()

    assert [pp.id for pp in PublishEngine()._get_due_platform_posts()] == [rows[0].id]


def test_a_second_pass_leaves_an_already_spread_backlog_alone(workspace, author):
    account = make_account(workspace)
    rows = overdue(workspace, author, account, 5)
    stagger_overdue_backlog()
    first = [PlatformPost.objects.get(pk=pp.pk).scheduled_at for pp in rows]

    assert stagger_overdue_backlog() == 0
    assert [PlatformPost.objects.get(pk=pp.pk).scheduled_at for pp in rows] == first


def test_a_few_overdue_posts_are_left_for_the_publisher(workspace, author):
    account = make_account(workspace)
    rows = overdue(workspace, author, account, 3)
    original = [pp.scheduled_at for pp in rows]

    assert stagger_overdue_backlog() == 0
    assert [PlatformPost.objects.get(pk=pp.pk).scheduled_at for pp in rows] == original


def test_posts_that_are_only_slightly_late_are_not_a_backlog(workspace, author):
    account = make_account(workspace)
    overdue(workspace, author, account, 10, late=timedelta(minutes=10))

    assert stagger_overdue_backlog() == 0


def test_each_channel_is_counted_on_its_own(workspace, author):
    busy = make_account(workspace, "busy")
    quiet = make_account(workspace, "quiet")
    overdue(workspace, author, busy, 5)
    quiet_rows = overdue(workspace, author, quiet, 2)
    original = [pp.scheduled_at for pp in quiet_rows]

    assert stagger_overdue_backlog() == 5
    assert [PlatformPost.objects.get(pk=pp.pk).scheduled_at for pp in quiet_rows] == original


def test_a_paused_channel_keeps_its_held_posts_for_resume_to_handle(workspace, author):
    account = make_account(workspace, paused_at=timezone.now())
    rows = overdue(workspace, author, account, 5)
    original = [pp.scheduled_at for pp in rows]

    assert stagger_overdue_backlog() == 0
    assert [PlatformPost.objects.get(pk=pp.pk).scheduled_at for pp in rows] == original


def test_posts_that_are_retrying_or_not_scheduled_are_not_moved(workspace, author):
    account = make_account(workspace)
    drafts = overdue(workspace, author, account, 5, status=PlatformPost.Status.DRAFT)
    retrying = overdue(workspace, author, account, 5)
    PlatformPost.objects.filter(pk__in=[pp.pk for pp in retrying]).update(retry_count=1)

    assert stagger_overdue_backlog() == 0
    assert all(PlatformPost.objects.get(pk=pp.pk).scheduled_at == pp.scheduled_at for pp in drafts + retrying)


def test_a_queue_entry_follows_its_moved_post(workspace, author):
    account = make_account(workspace)
    rows = overdue(workspace, author, account, 5)
    queue = Queue.objects.create(workspace=workspace, name="Q", social_account=account)
    entry = QueueEntry.objects.create(queue=queue, post=rows[2].post, position=1)

    stagger_overdue_backlog()

    entry.refresh_from_db()
    assert entry.assigned_slot_datetime == PlatformPost.objects.get(pk=rows[2].pk).scheduled_at


def test_the_thresholds_are_parameters(workspace, author):
    account = make_account(workspace)
    overdue(workspace, author, account, 2)

    assert stagger_overdue_backlog(threshold=1) == 2


def test_the_publisher_spreads_the_backlog_before_it_publishes(workspace, author):
    account = make_account(workspace)
    overdue(workspace, author, account, 6)

    with patch.object(PublishEngine, "_publish_post_group", return_value=True) as publish:
        published = PublishEngine().poll_and_publish()

    assert published == 1, "only the first of the backlog was due this cycle"
    assert publish.call_count == 1


def test_a_failure_while_spreading_never_stops_publishing(workspace, author):
    account = make_account(workspace)
    overdue(workspace, author, account, 1, late=timedelta(minutes=1))

    with (
        patch("apps.calendar.services.stagger_overdue_backlog", side_effect=RuntimeError("boom")),
        patch.object(PublishEngine, "_publish_post_group", return_value=True),
    ):
        assert PublishEngine().poll_and_publish() == 1


def test_the_settings_tune_what_counts_as_a_backlog(workspace, author, settings):
    settings.PUBLISHER_BACKLOG_THRESHOLD = 1
    settings.PUBLISHER_BACKLOG_GRACE_MINUTES = 5
    account = make_account(workspace)
    overdue(workspace, author, account, 2, late=timedelta(minutes=30))

    with patch.object(PublishEngine, "_publish_post_group", return_value=True):
        PublishEngine().poll_and_publish()

    times = sorted(PlatformPost.objects.values_list("scheduled_at", flat=True))
    assert times[1] - times[0] == timedelta(minutes=1)
