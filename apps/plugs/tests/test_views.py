"""The Automations page and the composer panel: permissions, workspace scoping and the form."""

from __future__ import annotations

import pytest
from django.urls import reverse

from apps.plugs.models import PlugRule
from apps.workspaces.models import Workspace

from .conftest import make_account, publish_post

pytestmark = pytest.mark.django_db


def url(name, workspace, **kwargs):
    return reverse(f"plugs:{name}", kwargs={"workspace_id": workspace.id, **kwargs})


FORM = {"trigger": "likes", "threshold": "25", "window_hours": "12", "action": "repost"}


class TestHome:
    def test_lists_rules_and_explains_the_policy(self, owner_client, workspace, published):
        PlugRule.objects.create(
            workspace=workspace, social_account=published.social_account, platform_post=published,
            trigger="likes", threshold=25, window_hours=12, action="repost",
        )  # fmt: skip
        page = owner_client.get(url("home", workspace)).content.decode()
        assert "At 25 likes within 12 hours: repost it" in page
        assert "autonomy policy" in page

    def test_networks_without_support_get_an_explanation(self, owner_client, workspace):
        make_account(workspace, "pinterest", "Pins")
        assert "nothing to automate" in owner_client.get(url("home", workspace)).content.decode()

    def test_viewer_can_look_but_not_change(self, viewer_client, workspace, account):
        # A viewer has no create_posts, so the page is closed to them as well.
        assert viewer_client.get(url("home", workspace)).status_code == 403
        assert viewer_client.post(url("rule_add", workspace), {"account": str(account.id), **FORM}).status_code == 403
        assert not PlugRule.objects.exists()


class TestAdd:
    def test_default_rule_for_an_account(self, owner_client, workspace, account):
        response = owner_client.post(url("rule_add", workspace), {"account": str(account.id), **FORM})
        assert response.status_code == 302
        rule = PlugRule.objects.get()
        assert rule.is_default and rule.threshold == 25 and rule.social_account == account

    def test_rule_error_is_shown_in_place(self, owner_client, workspace, account):
        response = owner_client.post(
            url("rule_add", workspace), {"account": str(account.id), **FORM, "action": "reply", "message": ""}
        )
        assert response.status_code == 422
        assert "Write the message" in response.content.decode()

    def test_cannot_use_another_workspaces_account(self, owner_client, workspace, organization):
        other = make_account(Workspace.objects.create(organization=organization, name="Other"), "bluesky", "Elsewhere")
        response = owner_client.post(url("rule_add", workspace), {"account": str(other.id), **FORM})
        assert response.status_code == 422 and not PlugRule.objects.exists()

    def test_cannot_pick_another_workspaces_account_as_reposter(self, owner_client, workspace, account, organization):
        other = make_account(Workspace.objects.create(organization=organization, name="Other"), "bluesky", "Elsewhere")
        response = owner_client.post(
            url("rule_add", workspace), {"account": str(account.id), **FORM, "actor_account": str(other.id)}
        )
        assert response.status_code == 422 and not PlugRule.objects.exists()

    def test_cross_account_repost(self, owner_client, workspace, toot, toot_booster):
        owner_client.post(
            url("rule_add", workspace), {"account": str(toot.id), **FORM, "actor_account": str(toot_booster.id)}
        )
        assert PlugRule.objects.get().actor_account == toot_booster

    def test_check_interval_is_kept(self, owner_client, workspace, account):
        owner_client.post(
            url("rule_add", workspace), {"account": str(account.id), **FORM, "check_interval_minutes": "90"}
        )
        assert PlugRule.objects.get().check_interval_minutes == 90


class TestToggleDelete:
    @pytest.fixture
    def rule(self, workspace, account):
        return PlugRule.objects.create(
            workspace=workspace, social_account=account, trigger="likes", threshold=5, window_hours=6, action="repost"
        )

    def test_toggle_and_delete(self, owner_client, workspace, rule):
        owner_client.post(url("rule_toggle", workspace, rule_id=rule.id))
        rule.refresh_from_db()
        assert rule.is_active is False
        owner_client.post(url("rule_delete", workspace, rule_id=rule.id))
        assert not PlugRule.objects.exists()

    def test_another_workspaces_rule_is_a_404(self, owner_client, owner, organization, rule):
        other = Workspace.objects.create(organization=organization, name="Other")
        # Same person, different workspace in the URL: the rule is not found there.
        from apps.members.models import WorkspaceMembership

        WorkspaceMembership.objects.create(
            user=owner, workspace=other, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
        )
        assert owner_client.post(url("rule_delete", other, rule_id=rule.id)).status_code == 404
        assert PlugRule.objects.filter(pk=rule.pk).exists()


class TestComposerPanel:
    def test_panel_lists_a_posts_accounts_and_rules(self, owner_client, workspace, published):
        PlugRule.objects.create(
            workspace=workspace, social_account=published.social_account, platform_post=published,
            trigger="likes", threshold=7, window_hours=24, action="reply", message="More here",
        )  # fmt: skip
        page = owner_client.get(url("composer_panel", workspace, post_id=published.post_id)).content.decode()
        assert 'id="plugs-panel"' in page and "At 7 likes" in page and "Add an automation" in page

    def test_adding_from_the_panel_returns_the_panel(self, owner_client, workspace, published):
        response = owner_client.post(
            url("rule_add", workspace),
            {"account": str(published.social_account_id), "post": str(published.post_id), "panel": "composer", **FORM},
            HTTP_HX_REQUEST="true",
        )
        assert response.status_code == 200 and 'id="plugs-panel"' in response.content.decode()
        assert PlugRule.objects.get().platform_post == published

    def test_panel_error_still_swaps(self, owner_client, workspace, published):
        response = owner_client.post(
            url("rule_add", workspace),
            {"account": str(published.social_account_id), "post": str(published.post_id), "panel": "composer",
             **FORM, "threshold": "0"},
            HTTP_HX_REQUEST="true",
        )  # fmt: skip
        assert response.status_code == 200 and "at least 1" in response.content.decode()

    def test_other_workspaces_post_is_a_404(self, owner_client, workspace, organization):
        other_ws = Workspace.objects.create(organization=organization, name="Other")
        theirs = publish_post(make_account(other_ws, "bluesky", "Elsewhere"))
        assert owner_client.get(url("composer_panel", workspace, post_id=theirs.post_id)).status_code == 404
