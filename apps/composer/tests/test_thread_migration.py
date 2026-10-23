"""The thread migration is additive: every existing post stays a single post."""

from __future__ import annotations

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

APP = "composer"
BEFORE = "0025_post_recurrence_source"
THREADS = "0026_thread_parts"


def _migrate(target: str):
    executor = MigrationExecutor(connection)
    executor.migrate([(APP, target)])
    # The state after the step, for historical models.
    return MigrationExecutor(connection).loader.project_state([(APP, target)]).apps


@pytest.fixture
def restore_migrations():
    yield
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())


@pytest.mark.django_db(transaction=True)
def test_existing_posts_become_single_part_threads(restore_migrations):
    from apps.organizations.models import Organization
    from apps.social_accounts.models import SocialAccount
    from apps.workspaces.models import Workspace

    old = _migrate(BEFORE)
    # Only the composer is rolled back, so the other apps' current models apply.
    org = Organization.objects.create(name="Migration Org")
    workspace = Workspace.objects.create(organization=org, name="Migration WS")
    account = SocialAccount.objects.create(
        workspace=workspace, platform="bluesky", account_platform_id="m-1", account_name="M"
    )
    post = old.get_model(APP, "Post").objects.create(workspace_id=workspace.id, caption="written before threads")
    old.get_model(APP, "PlatformPost").objects.create(post=post, social_account_id=account.id, status="published")

    new = _migrate(THREADS)

    platform_post = new.get_model(APP, "PlatformPost").objects.get(post_id=post.id)
    assert platform_post.thread_status == "none"
    assert platform_post.thread_error == ""
    assert not new.get_model(APP, "PostPart").objects.filter(post_id=post.id).exists()
