"""The composer's two-pane layout: channel strip, tabs, per-channel panes, the Schedule split button, the preview pane.

Behaviour (tab switching, counts, the Post now confirmation) lives in Alpine and is covered by
tests/frontend/composer-tabs.test.cjs; this pins the markup that behaviour hangs off.
"""

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace


class ComposerLayoutTests(TestCase):
    def setUp(self):
        user = User.objects.create_user(
            email="owner@example.com", password="testpass123", tos_accepted_at=timezone.now()
        )
        org = Organization.objects.create(name="Org")
        self.ws = Workspace.objects.create(organization=org, name="WS")
        OrgMembership.objects.create(user=user, organization=org, org_role=OrgMembership.OrgRole.OWNER)
        WorkspaceMembership.objects.create(
            user=user, workspace=self.ws, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER
        )
        self.account = SocialAccount.objects.create(
            workspace=self.ws,
            platform="bluesky",
            account_platform_id="bs-1",
            account_name="Studio",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        self.client.force_login(user)
        self.body = self.client.get(reverse("composer:compose", kwargs={"workspace_id": self.ws.id})).content.decode()

    def test_each_channel_is_a_toggle_and_has_a_tab_and_a_remove_button(self):
        assert 'class="channel-toggle"' in self.body
        assert 'role="tablist"' in self.body
        assert 'id="tab-global"' in self.body
        assert f'id="tab-{self.account.id}"' in self.body
        assert f"Remove {self.account.display_name} from this post" in self.body

    def test_global_and_channel_panes_are_tab_panels(self):
        assert 'id="pane-global" role="tabpanel"' in self.body
        assert 'id="pane-channel" role="tabpanel"' in self.body
        assert "composer-tabs.js" in self.body

    def test_split_button_offers_queue_schedule_post_now_and_draft(self):
        for mode in ("add_to_queue", "add_to_queue_priority", "schedule", "publish_now", "save_draft"):
            assert f'data-mode="{mode}"' in self.body, mode
        assert 'id="publish-action-btn"' in self.body
        # Post now is never a plain submit: the menu item asks first (composerApp.publishNow).
        assert 'name="action" value="publish_now"' not in self.body

    def test_form_fields_and_preview_wiring_are_unchanged(self):
        for needle in (
            'name="caption"',
            'name="selected_accounts"',
            'hx-trigger="previewUpdate from:body"',
            'id="autosave-trigger"',
            "data-unsaved-guard",
        ):
            assert needle in self.body, needle
