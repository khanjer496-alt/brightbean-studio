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


class ChannelOverrideRoundTripTests(ComposerLayoutTests):
    """A channel's own text survives reopening and saving; only an explicit reset (or deselecting) clears it."""

    def _save(self, post, **extra):
        url = reverse("composer:save_post_edit", kwargs={"workspace_id": self.ws.id, "post_id": post.id})
        data = {"caption": "Shared", "selected_accounts": str(self.account.id), "action": "save_draft", **extra}
        return self.client.post(url, data, HTTP_HX_REQUEST="true")

    def _post(self):
        from apps.composer.models import PlatformPost, Post

        post = Post.objects.create(workspace=self.ws, author=User.objects.get(), caption="Shared")
        pp = PlatformPost.objects.create(
            post=post,
            social_account=self.account,
            platform_specific_caption="Own text",
            platform_specific_first_comment="Own comment",
        )
        return post, pp

    def test_reopening_exposes_the_saved_override_to_the_form(self):
        post, _pp = self._post()
        resp = self.client.get(
            reverse("composer:compose_edit", kwargs={"workspace_id": self.ws.id, "post_id": post.id})
        )
        saved = resp.context["platform_overrides"][str(self.account.id)]
        assert saved["caption"] == "Own text"
        assert 'id="composer-platform-overrides"' in resp.content.decode()

    def test_saving_without_the_field_keeps_the_override(self):
        post, pp = self._post()
        assert self._save(post).status_code == 204
        pp.refresh_from_db()
        assert pp.platform_specific_caption == "Own text"
        assert pp.platform_specific_first_comment == "Own comment"

    def test_saving_the_submitted_text_updates_it_and_a_blank_clears_it(self):
        post, pp = self._post()
        self._save(post, **{f"override_caption_{self.account.id}": " Edited "})
        pp.refresh_from_db()
        assert pp.platform_specific_caption == "Edited"
        self._save(post, **{f"override_caption_{self.account.id}": ""})
        pp.refresh_from_db()
        assert pp.platform_specific_caption is None

    def test_explicit_reset_clears_every_override(self):
        post, pp = self._post()
        self._save(post, **{f"override_reset_{self.account.id}": "1"})
        pp.refresh_from_db()
        assert pp.platform_specific_caption is None
        assert pp.platform_specific_first_comment is None

    def test_deselecting_and_reselecting_starts_shared(self):
        from apps.composer.models import PlatformPost

        post, _pp = self._post()
        self._save(post, selected_accounts="")
        assert not PlatformPost.objects.filter(post=post).exists()
        self._save(post)
        assert PlatformPost.objects.get(post=post).platform_specific_caption is None
