"""save_post must not store a thumbnail/cover asset the workspace can't use.

``yt_thumbnail_asset_id_*`` and ``pin_cover_image_asset_id_*`` land in
``platform_extra`` and are published as files, so an id from another tenant
would publish that tenant's media.
"""

import uuid

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.composer.models import PlatformPost, Post
from apps.media_library.models import MediaAsset
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace


def _asset(org, workspace):
    return MediaAsset.objects.create(
        organization=org,
        workspace=workspace,
        filename="thumb.jpg",
        media_type=MediaAsset.MediaType.IMAGE,
        file_size=1,
        processing_status=MediaAsset.ProcessingStatus.COMPLETED,
    )


class PlatformAssetScopeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="owner@example.com",
            password="testpass123",
            tos_accepted_at=timezone.now(),
        )
        self.org = Organization.objects.create(name="Org")
        self.workspace = Workspace.objects.create(organization=self.org, name="WS")
        OrgMembership.objects.create(user=self.user, organization=self.org, org_role=OrgMembership.OrgRole.OWNER)
        WorkspaceMembership.objects.create(
            user=self.user,
            workspace=self.workspace,
            workspace_role=WorkspaceMembership.WorkspaceRole.OWNER,
        )
        self.youtube = SocialAccount.objects.create(
            workspace=self.workspace,
            platform="youtube",
            account_platform_id="yt-1",
            account_name="YT",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        self.pinterest = SocialAccount.objects.create(
            workspace=self.workspace,
            platform="pinterest",
            account_platform_id="pin-1",
            account_name="Pins",
            connection_status=SocialAccount.ConnectionStatus.CONNECTED,
        )
        other_org = Organization.objects.create(name="Other Org")
        self.foreign_asset = _asset(other_org, Workspace.objects.create(organization=other_org, name="Other"))
        self.client.force_login(self.user)
        self.save_url = reverse("composer:save_post", kwargs={"workspace_id": self.workspace.id})

    def _save_youtube(self, thumb_id):
        return self.client.post(
            self.save_url,
            data={
                "action": "save_draft",
                "title": "t",
                "caption": "c",
                "selected_accounts": str(self.youtube.id),
                f"yt_thumbnail_asset_id_{self.youtube.id}": str(thumb_id),
            },
        )

    def _save_pinterest(self, cover_id):
        return self.client.post(
            self.save_url,
            data={
                "action": "save_draft",
                "title": "t",
                "caption": "c",
                "selected_accounts": str(self.pinterest.id),
                f"pin_board_id_{self.pinterest.id}": "board-1",
                f"pin_cover_image_asset_id_{self.pinterest.id}": str(cover_id),
            },
        )

    def test_foreign_youtube_thumbnail_is_rejected(self):
        response = self._save_youtube(self.foreign_asset.id)

        assert response.status_code == 400
        assert "media" in response.json()["errors"]
        assert not Post.objects.filter(workspace=self.workspace).exists()

    def test_foreign_pinterest_cover_is_rejected(self):
        response = self._save_pinterest(self.foreign_asset.id)

        assert response.status_code == 400
        assert "media" in response.json()["errors"]
        assert not PlatformPost.objects.filter(social_account=self.pinterest).exists()

    def test_sibling_workspace_asset_is_rejected(self):
        sibling = Workspace.objects.create(organization=self.org, name="Sibling")

        response = self._save_youtube(_asset(self.org, sibling).id)

        assert response.status_code == 400

    def test_malformed_and_unknown_ids_are_rejected(self):
        assert self._save_youtube("not-a-uuid").status_code == 400
        assert self._save_youtube(uuid.uuid4()).status_code == 400

    def test_own_workspace_asset_is_stored(self):
        asset = _asset(self.org, self.workspace)

        response = self._save_youtube(asset.id)

        assert response.status_code in (200, 204, 302)
        pp = PlatformPost.objects.get(social_account=self.youtube)
        assert pp.platform_extra["thumbnail_asset_id"] == str(asset.id)

    def test_org_shared_library_asset_is_stored(self):
        asset = _asset(self.org, None)

        response = self._save_pinterest(asset.id)

        assert response.status_code in (200, 204, 302)
        pp = PlatformPost.objects.get(social_account=self.pinterest)
        assert pp.platform_extra["cover_image_asset_id"] == str(asset.id)
