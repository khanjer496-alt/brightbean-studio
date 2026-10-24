"""Engagement alerts: first-day outperformers, once each, through the notification preferences."""

from datetime import UTC, datetime, timedelta

import pytest

from apps.analytics.engagement_alerts import (
    ALERT_MULTIPLE,
    MIN_BASELINE_POSTS,
    find_outperformers,
    send_engagement_alerts,
)
from apps.analytics.models import EngagementAlert
from apps.billing.tests.support import set_plan
from apps.members.models import WorkspaceMembership
from apps.notifications.models import Channel, EventType, Notification, NotificationDelivery, NotificationPreference
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace

from .helpers import make_account, make_member, make_post

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)


@pytest.fixture
def workspace(db):
    return Workspace.objects.create(organization=Organization.objects.create(name="Org"), name="WS")


@pytest.fixture
def account(workspace):
    return make_account(workspace, "instagram", "ig")


def day_one(published_at, likes):
    """Snapshots as the hourly sync leaves them: captured inside the first 24 hours."""
    return {"likes": [(published_at.date(), float(likes), published_at + timedelta(hours=5))]}


def history(workspace, account, values):
    for i, likes in enumerate(values):
        when = NOW - timedelta(days=10 + i)
        make_post(workspace, account, when, f"old {i}", snapshots=day_one(when, likes))


def star(workspace, account, likes, hours_ago=6):
    when = NOW - timedelta(hours=hours_ago)
    return make_post(workspace, account, when, "star", snapshots=day_one(when, likes))


@pytest.mark.django_db
def test_a_post_far_above_the_median_is_found(workspace, account):
    history(workspace, account, [10, 12, 8, 11, 9, 10])  # median 10
    post = star(workspace, account, 45)
    hits = find_outperformers(NOW)
    assert [(p.id, e, b) for p, e, b in hits] == [(post.id, 45.0, 10.0)]


@pytest.mark.django_db
@pytest.mark.parametrize("likes", [29, 5])
def test_a_post_below_the_threshold_is_not(workspace, account, likes):
    history(workspace, account, [10, 12, 8, 11, 9, 10])
    star(workspace, account, likes)
    assert find_outperformers(NOW) == []
    assert ALERT_MULTIPLE == 3


@pytest.mark.django_db
def test_too_little_history_or_a_trivial_count_never_alerts(workspace, account):
    history(workspace, account, [1] * (MIN_BASELINE_POSTS - 1))
    star(workspace, account, 500)
    assert find_outperformers(NOW) == []

    other = make_account(workspace, "facebook", "fb")
    history(workspace, other, [1, 1, 1, 1, 1, 1])
    when = NOW - timedelta(hours=3)
    make_post(
        workspace, other, when, "tiny", snapshots={"likes": [(when.date(), 6.0, when)]}
    )  # 6x the median, but only 6
    assert find_outperformers(NOW) == []


@pytest.mark.django_db
def test_engagement_after_the_first_day_does_not_count(workspace, account):
    history(workspace, account, [10, 10, 10, 10, 10])
    when = NOW - timedelta(hours=30)
    make_post(
        workspace,
        account,
        when,
        "slow burn",
        snapshots={
            "likes": [
                (when.date(), 12.0, when + timedelta(hours=4)),
                (when.date() + timedelta(days=1), 400.0, when + timedelta(hours=28)),
            ]
        },
    )
    assert find_outperformers(NOW) == []


@pytest.mark.django_db
def test_posts_older_than_two_days_and_native_only_platforms_are_skipped(workspace, account):
    history(workspace, account, [10, 10, 10, 10, 10])
    star(workspace, account, 400, hours_ago=60)
    yt = make_account(workspace, "youtube", "yt")
    history(workspace, yt, [10, 10, 10, 10, 10])
    star(workspace, yt, 400)
    assert find_outperformers(NOW) == []


@pytest.mark.django_db
def test_alert_is_sent_once_to_members_who_may_view_analytics(workspace, account):
    owner = make_member(workspace, "owner@example.com")
    make_member(workspace, "contrib@example.com", WorkspaceMembership.WorkspaceRole.CONTRIBUTOR)  # no view_analytics
    history(workspace, account, [10, 12, 8, 11, 9, 10])
    post = star(workspace, account, 45)

    assert send_engagement_alerts(NOW) == 1
    assert send_engagement_alerts(NOW) == 0  # second run: already flagged
    assert send_engagement_alerts(NOW + timedelta(hours=1)) == 0

    notes = Notification.objects.filter(event_type=EventType.ENGAGEMENT_ALERT)
    assert [n.user_id for n in notes] == [owner.id]
    assert notes[0].data["platform_post_id"] == str(post.id)
    assert "4.5x" in notes[0].title
    assert "\u201cstar\u201d on ig got 45 engagements" in notes[0].body
    alert = EngagementAlert.objects.get(platform_post=post)
    assert (alert.engagement, alert.baseline) == (45.0, 10.0)


@pytest.mark.django_db
def test_user_preferences_are_respected(workspace, account):
    owner = make_member(workspace, "owner@example.com")
    for channel in (Channel.IN_APP, Channel.EMAIL):
        NotificationPreference.objects.create(
            user=owner, event_type=EventType.ENGAGEMENT_ALERT, channel=channel, is_enabled=False
        )
    history(workspace, account, [10, 12, 8, 11, 9, 10])
    star(workspace, account, 45)

    assert send_engagement_alerts(NOW) == 1
    assert not NotificationDelivery.objects.filter(notification__user=owner).exists()


@pytest.mark.django_db
def test_default_preferences_deliver_in_app(workspace, account):
    owner = make_member(workspace, "owner@example.com")
    history(workspace, account, [10, 12, 8, 11, 9, 10])
    star(workspace, account, 45)
    send_engagement_alerts(NOW)
    assert NotificationDelivery.objects.filter(notification__user=owner, channel=Channel.IN_APP).exists()


@pytest.mark.django_db
def test_organizations_without_the_analytics_feature_are_skipped(settings, workspace, account):
    settings.BILLING_ENABLED = True
    set_plan(workspace.organization, "free")
    make_member(workspace, "owner@example.com")
    history(workspace, account, [10, 12, 8, 11, 9, 10])
    star(workspace, account, 45)
    assert send_engagement_alerts(NOW) == 0
    assert not EngagementAlert.objects.exists()

    set_plan(workspace.organization, "starter")
    assert send_engagement_alerts(NOW) == 1


@pytest.mark.django_db
def test_finding_posts_uses_a_fixed_number_of_queries(workspace, django_assert_max_num_queries):
    for i in range(4):
        acct = make_account(workspace, "instagram", f"ig{i}")
        history(workspace, acct, [10, 12, 8, 11, 9, 10])
        star(workspace, acct, 45)
    with django_assert_max_num_queries(6):
        assert len(find_outperformers(NOW)) == 4
