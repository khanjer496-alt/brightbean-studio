"""Autosave must not create a draft for a composer nobody has typed in."""

import json

import pytest
from django.urls import reverse

from apps.analytics.tests.helpers import make_member
from apps.composer.models import Post
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db

BLANK = {"title": "", "caption": "", "first_comment": "", "internal_notes": "", "tags": "", "selected_accounts": ""}


@pytest.fixture
def workspace():
    return Workspace.objects.create(organization=Organization.objects.create(name="Agency"), name="Acme")


@pytest.fixture
def owner_client(client, workspace):
    client.force_login(make_member(workspace, "owner@example.com"))
    return client


def _autosave(client, workspace, **data):
    return client.post(reverse("composer:autosave", kwargs={"workspace_id": workspace.id}), {**BLANK, **data})


def test_blank_composer_tick_creates_nothing(owner_client, workspace):
    response = _autosave(owner_client, workspace, thread_parts="[]")
    assert response.status_code == 200
    assert "HX-Trigger" not in response.headers
    assert not Post.objects.filter(workspace=workspace).exists()


def test_first_tick_with_content_creates_the_draft(owner_client, workspace):
    response = _autosave(owner_client, workspace, caption="Autumn roast lands Friday")
    post = Post.objects.get(workspace=workspace)
    assert post.caption == "Autumn roast lands Friday"
    assert json.loads(response.headers["HX-Trigger"])["autosaved"] == {"postId": str(post.id), "isNew": True}


def test_a_thread_part_alone_is_worth_saving(owner_client, workspace):
    _autosave(owner_client, workspace, thread_parts=json.dumps([{"caption": "Part two"}]))
    assert Post.objects.filter(workspace=workspace).count() == 1
