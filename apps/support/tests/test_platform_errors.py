import datetime as dt

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.composer.models import PlatformPost, Post
from apps.publisher.models import PublishLog
from apps.social_accounts.models import SocialAccount
from apps.support.services import platform_error_summary, scrub_sample
from apps.workspaces.models import Workspace

from .conftest import make_user

URL = "support:platform_errors"


@pytest.fixture
def workspace(superuser):
    from apps.members.models import OrgMembership

    return Workspace.objects.create(organization=OrgMembership.objects.get(user=superuser).organization, name="WS")


def account(workspace, platform, handle):
    return SocialAccount.objects.create(
        workspace=workspace,
        platform=platform,
        account_platform_id=f"{platform}-{handle}",
        account_name=handle,
        account_handle=handle,
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )


def fail(workspace, social_account, *, status_code=None, message="boom", hours_ago=1):
    post = Post.objects.create(workspace=workspace, caption="hi", author=None)
    platform_post = PlatformPost.objects.create(
        post=post, social_account=social_account, status=PlatformPost.Status.FAILED
    )
    log = PublishLog.objects.create(platform_post=platform_post, status_code=status_code, error_message=message)
    PublishLog.objects.filter(pk=log.pk).update(created_at=timezone.now() - dt.timedelta(hours=hours_ago))
    return log


@pytest.mark.django_db
class TestSummary:
    def test_groups_by_platform_then_status(self, workspace):
        bluesky = account(workspace, "bluesky", "@a.bsky.social")
        mastodon = account(workspace, "mastodon", "@b@mastodon.social")
        fail(workspace, bluesky, status_code=500)
        fail(workspace, bluesky, status_code=500)
        fail(workspace, bluesky, status_code=401)
        fail(workspace, mastodon, status_code=429)

        summary = platform_error_summary("24h")

        assert [p["platform"] for p in summary] == ["bluesky", "mastodon"]
        assert summary[0]["failures"] == 3
        assert [(c["status_code"], c["failures"]) for c in summary[0]["codes"]] == [(500, 2), (401, 1)]
        assert summary[1]["codes"][0]["status_code"] == 429

    def test_counts_distinct_accounts_and_keeps_the_latest_sample(self, workspace):
        first = account(workspace, "bluesky", "@first")
        second = account(workspace, "bluesky", "@second")
        fail(workspace, first, status_code=503, message="old outage", hours_ago=5)
        fail(workspace, second, status_code=503, message="newest outage", hours_ago=1)
        fail(workspace, second, status_code=503, message="middle outage", hours_ago=3)

        code = platform_error_summary("24h")[0]["codes"][0]

        assert code["failures"] == 3
        assert code["accounts"] == 2
        assert code["sample"] == "newest outage"
        assert code["account_handle"] == "@second"

    def test_window_limits_what_counts(self, workspace):
        bluesky = account(workspace, "bluesky", "@a")
        fail(workspace, bluesky, status_code=500, hours_ago=2)
        fail(workspace, bluesky, status_code=500, hours_ago=72)
        fail(workspace, bluesky, status_code=500, hours_ago=24 * 10)

        assert platform_error_summary("24h")[0]["failures"] == 1
        assert platform_error_summary("7d")[0]["failures"] == 2

    def test_successful_attempts_are_not_failures(self, workspace):
        bluesky = account(workspace, "bluesky", "@a")
        post = Post.objects.create(workspace=workspace, caption="hi", author=None)
        platform_post = PlatformPost.objects.create(post=post, social_account=bluesky)
        PublishLog.objects.create(platform_post=platform_post, status_code=200)

        assert platform_error_summary("24h") == []

    def test_samples_are_scrubbed(self):
        scrubbed = scrub_sample(
            "401 for https://x.test/me?access_token=abc123 by ann@example.com Authorization: Bearer sk-live-9 ok"
        )

        assert "abc123" not in scrubbed
        assert "ann@example.com" not in scrubbed
        assert "sk-live-9" not in scrubbed
        assert scrubbed.endswith("ok")

    def test_samples_are_truncated(self):
        assert len(scrub_sample("x" * 1000)) == 200


@pytest.mark.django_db
class TestPage:
    def test_staff_see_the_summary(self, staff_client, workspace):
        fail(workspace, account(workspace, "bluesky", "@a.bsky.social"), status_code=502, message="bad gateway")

        response = staff_client.get(reverse(URL))

        assert response.status_code == 200
        assert b"Bluesky" in response.content
        assert b"502" in response.content
        assert b"bad gateway" in response.content
        assert b"@a.bsky.social" in response.content

    def test_window_switch(self, staff_client, workspace):
        fail(workspace, account(workspace, "bluesky", "@a"), status_code=500, message="three days ago", hours_ago=72)

        assert b"three days ago" not in staff_client.get(reverse(URL)).content
        assert b"three days ago" in staff_client.get(reverse(URL), {"window": "7d"}).content

    def test_unknown_window_falls_back(self, staff_client):
        assert staff_client.get(reverse(URL), {"window": "forever"}).status_code == 200

    def test_empty_state(self, staff_client):
        assert b"No failed publish attempts" in staff_client.get(reverse(URL)).content

    def test_ordinary_users_are_refused(self, client):
        client.force_login(make_user("member@example.com"))

        assert client.get(reverse(URL)).status_code == 403

    def test_anonymous_is_sent_to_login(self, client):
        assert client.get(reverse(URL)).status_code == 302

    def test_refused_while_viewing_as_a_customer(self, staff_client, customer):
        from apps.support.tests.test_impersonation import start

        start(staff_client, customer)

        assert staff_client.get(reverse(URL)).status_code == 403
