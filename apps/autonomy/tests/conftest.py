"""Fixtures for the autonomy tests.

The wider suite pins permissive defaults (see ``config/settings/test.py``) so the
tests that predate the dial keep their meaning. These tests run on the
conservative production defaults and set every policy they depend on explicitly.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.autonomy.models import AccountAutonomyOverride, AutonomyPolicy


@pytest.fixture(autouse=True)
def conservative_defaults(settings):
    settings.AUTONOMY_DEFAULT_LEVELS = {
        "draft_level": "draft_only",
        "publish_level": "draft_only",
        "reply_level": "draft_only",
        "recycle_level": "per_item",
    }
    settings.AUTONOMY_DEFAULT_UNDO_MINUTES = 5


@pytest.fixture(autouse=True)
def _isolate_failed_auth_throttle():
    from django.core.cache import cache

    cache.delete("agent_api:auth_fail:127.0.0.1")
    yield
    cache.delete("agent_api:auth_fail:127.0.0.1")


@pytest.fixture
def world(db):
    from apps.api.tests.world import build_world

    return build_world()


def set_policy(workspace, **fields) -> AutonomyPolicy:
    policy, _ = AutonomyPolicy.objects.update_or_create(workspace=workspace, defaults=fields)
    return policy


def set_override(workspace, account, **fields) -> AccountAutonomyOverride:
    override, _ = AccountAutonomyOverride.objects.update_or_create(
        workspace=workspace, social_account=account, defaults=fields
    )
    return override


def post_json(client, path, payload, **extra):
    return client.post(path, data=json.dumps(payload), content_type="application/json", **extra)


def patch_json(client, path, payload):
    return client.patch(path, data=json.dumps(payload), content_type="application/json")


def future(hours=2):
    return (timezone.now() + timedelta(hours=hours)).isoformat()


def schedule_via_rest(client, account, caption="A calm update", when=None):
    return post_json(
        client,
        "/api/v1/posts/",
        {
            "social_account_id": str(account.id),
            "caption": caption,
            "action": "schedule",
            "scheduled_at": when or future(),
        },
    )
