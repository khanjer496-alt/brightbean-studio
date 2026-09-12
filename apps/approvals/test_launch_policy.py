"""Approval authority is current, tenant scoped, and separated by review stage."""

import pytest
from django.http import HttpResponse
from django.test import RequestFactory

from apps.accounts.models import User
from apps.approvals import services
from apps.client_portal.decorators import portal_auth_required
from apps.composer.models import PlatformPost, Post
from apps.members.models import WorkspaceMembership
from apps.social_accounts.models import SocialAccount

pytestmark = pytest.mark.django_db


@pytest.fixture
def review():
    owner = User.objects.create_user(email="approval-owner@example.com")
    workspace = WorkspaceMembership.objects.get(user=owner).workspace
    workspace.approval_workflow_mode = "required_internal_and_client"
    workspace.save(update_fields=["approval_workflow_mode"])
    client = User.objects.create_user(email="approval-client@example.com")
    membership = WorkspaceMembership.objects.create(user=client, workspace=workspace, workspace_role="client")
    account = SocialAccount.objects.create(
        workspace=workspace, platform="linkedin_personal", account_platform_id="approval-fixture", account_name="Test"
    )
    post = Post.objects.create(workspace=workspace, author=owner, caption="Reviewed content")
    child = PlatformPost.objects.create(post=post, social_account=account, status="pending_review")
    return owner, client, membership, workspace, post, child


def test_both_review_stages_require_their_own_actor_even_for_single_platform_target(review):
    owner, client, membership, workspace, post, child = review
    assert services.approve_post(child, client, workspace) == []
    child.refresh_from_db()
    assert child.status == "pending_review"
    services.approve_post(child, owner, workspace)
    child.refresh_from_db()
    assert child.status == "pending_client"
    assert not child.approval_fingerprint
    assert services.approve_post(child, owner, workspace) == []
    services.approve_post(child, client, workspace)
    child.refresh_from_db()
    assert child.status == "approved"
    assert child.approval_fingerprint


@pytest.mark.parametrize("action", ["approve", "reject", "changes"])
def test_demoted_client_cannot_make_decisions(review, action):
    owner, client, membership, workspace, post, child = review
    services.approve_post(post, owner, workspace)
    WorkspaceMembership.objects.filter(pk=membership.pk).update(workspace_role="viewer")
    with pytest.raises(ValueError, match="permission"):
        if action == "approve":
            services.approve_post(post, client, workspace)
        elif action == "reject":
            services.reject_post(post, client, workspace, "No")
        else:
            services.request_changes(post, client, workspace, "Change")
    child.refresh_from_db()
    assert child.status == "pending_client"
    assert not child.approval_fingerprint


@pytest.mark.parametrize("disabled", ["user", "workspace"])
def test_disabled_authority_cannot_approve(review, disabled):
    owner, client, membership, workspace, post, child = review
    if disabled == "user":
        User.objects.filter(pk=owner.pk).update(is_active=False)
    else:
        type(workspace).objects.filter(pk=workspace.pk).update(is_archived=True)
    with pytest.raises(ValueError, match="permission"):
        services.approve_post(post, owner, workspace)
    child.refresh_from_db()
    assert child.status == "pending_review"


def test_existing_portal_session_cannot_survive_role_demotion(review):
    owner, client, membership, workspace, post, child = review
    request = RequestFactory().get("/portal/")
    request.user = client
    request.session = {"is_portal_session": True, "portal_workspace_id": str(workspace.pk)}
    view = portal_auth_required(lambda request: HttpResponse("allowed"))
    assert view(request).status_code == 200
    WorkspaceMembership.objects.filter(pk=membership.pk).update(workspace_role="viewer")
    assert view(request).status_code == 302
