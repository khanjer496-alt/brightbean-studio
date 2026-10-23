"""The brand kit: validation, workspace-over-organization fallback, and where it is used."""

from __future__ import annotations

import io

import pytest
from django.core.files.base import ContentFile
from django.urls import reverse
from PIL import Image

from apps.ai_writer.tests.fakes import patch_anthropic
from apps.media_library.models import MediaAsset
from apps.workspaces import brand_kit
from apps.workspaces.models import BrandKit

pytestmark = pytest.mark.django_db


@pytest.fixture
def workspace(db, organization):
    from apps.workspaces.models import Workspace

    return Workspace.objects.create(name="Kit WS", organization=organization, primary_color="#aa2255")


@pytest.fixture
def logo(workspace, organization):
    buf = io.BytesIO()
    Image.new("RGB", (300, 100), "navy").save(buf, format="PNG")
    asset = MediaAsset.objects.create(
        organization=organization,
        workspace=workspace,
        filename="logo.png",
        media_type=MediaAsset.MediaType.IMAGE,
        file_size=buf.tell(),
    )
    asset.file.save("logo.png", ContentFile(buf.getvalue()), save=True)
    return asset


@pytest.fixture
def org_logo(organization):
    buf = io.BytesIO()
    Image.new("RGB", (300, 100), "green").save(buf, format="PNG")
    asset = MediaAsset.objects.create(
        organization=organization,
        workspace=None,
        filename="org.png",
        media_type=MediaAsset.MediaType.IMAGE,
        file_size=buf.tell(),
    )
    asset.file.save("org.png", ContentFile(buf.getvalue()), save=True)
    return asset


class TestUpdate:
    def test_saves_and_normalizes(self, workspace, logo):
        kit = brand_kit.kit_for(workspace=workspace)
        changed = brand_kit.update(
            kit,
            {
                "logo_primary": str(logo.id),
                "colors": [{"name": "Sunrise", "hex": "#F97316"}, {"name": "", "hex": "#000000"}],
                "default_hashtags": "brand #Launch brand",
                "tagline": " Fresh daily ",
                "disclaimers": [{"name": "", "text": "Not advice"}, {"name": "Blank", "text": "  "}],
                "fonts": [{"name": "Inter", "role": "heading"}],
            },
        )
        assert "logo_primary" in changed
        kit.refresh_from_db()
        assert kit.logo_primary_id == logo.id and kit.tagline == "Fresh daily"
        assert kit.colors[0] == {"name": "Sunrise", "hex": "#f97316"} and kit.colors[1]["name"] == "#000000"
        assert kit.default_hashtags == ["#brand", "#Launch"]
        assert kit.disclaimers == [{"name": "Disclaimer", "text": "Not advice"}]

    @pytest.mark.parametrize(
        "data",
        [
            {"colors": [{"name": "x", "hex": "red"}]},
            {"colors": [{"name": "x", "hex": "#000000"}] * 13},
            {"fonts": [{"name": "", "role": "body"}]},
            {"fonts": [{"name": "A", "role": "fancy"}]},
            {"tagline": "x" * 151},
            {"dos": "x" * 1001},
            {"default_hashtags": ["bad tag!"]},
            {"unknown": "x"},
        ],
    )
    def test_invalid_values_change_nothing(self, workspace, data):
        kit = brand_kit.kit_for(workspace=workspace)
        with pytest.raises(brand_kit.BrandKitError):
            brand_kit.update(kit, {"tagline": "kept", **data})
        kit.refresh_from_db()
        assert kit.tagline == ""

    def test_a_logo_must_be_an_image_of_this_brand(self, workspace, logo, org_logo):
        from apps.organizations.models import Organization
        from apps.workspaces.models import Workspace

        other_org = Organization.objects.create(name="Other")
        other_ws = Workspace.objects.create(name="Other", organization=other_org)
        foreign = MediaAsset.objects.create(
            organization=other_org, workspace=other_ws, filename="f.png", media_type="image", file="x.png"
        )
        doc = MediaAsset.objects.create(
            organization=workspace.organization,
            workspace=workspace,
            filename="d.pdf",
            media_type="document",
            file="d.pdf",
        )
        kit = brand_kit.kit_for(workspace=workspace)
        for bad in (str(foreign.id), str(doc.id), "not-a-uuid"):
            with pytest.raises(brand_kit.BrandKitError):
                brand_kit.update(kit, {"logo_mark": bad})
        # The organization's shared library is fair game; clearing works.
        brand_kit.update(kit, {"logo_mark": str(org_logo.id)})
        brand_kit.update(kit, {"logo_mark": ""})
        kit.refresh_from_db()
        assert kit.logo_mark is None

    def test_a_kit_belongs_to_exactly_one_scope(self, workspace, organization):
        from django.db import IntegrityError, transaction

        with pytest.raises(IntegrityError), transaction.atomic():
            BrandKit.objects.create(workspace=workspace, organization=organization)
        with pytest.raises(IntegrityError), transaction.atomic():
            BrandKit.objects.create()


class TestResolve:
    def test_falls_back_field_by_field_to_the_organization(self, workspace, organization, org_logo, logo):
        brand_kit.update(
            brand_kit.kit_for(organization=organization),
            {
                "logo_primary": str(org_logo.id),
                "tagline": "Org tagline",
                "dos": "Be kind",
                "colors": [{"name": "Org", "hex": "#00ff00"}],
            },
        )
        brand_kit.update(
            brand_kit.kit_for(workspace=workspace), {"tagline": "Local tagline", "logo_mark": str(logo.id)}
        )
        kit = brand_kit.resolve(workspace)
        assert kit.tagline == "Local tagline"
        assert kit.dos == "Be kind" and kit.primary_color == "#00ff00"
        assert kit.logos["primary"] == org_logo and kit.logos["mark"] == logo and kit.logos["on_dark"] is None

    def test_falls_back_to_the_legacy_workspace_colour_and_hashtags(self, workspace):
        workspace.default_hashtags = ["#legacy"]
        workspace.save()
        kit = brand_kit.resolve(workspace)
        assert kit.colors == [{"name": "Primary", "hex": "#aa2255"}] and kit.default_hashtags == ["#legacy"]

    def test_a_workspace_does_not_see_another_organizations_kit(self, workspace):
        from apps.organizations.models import Organization

        brand_kit.update(brand_kit.kit_for(organization=Organization.objects.create(name="Other")), {"tagline": "x"})
        assert brand_kit.resolve(workspace).tagline == ""


class TestUsedBy:
    def test_ai_prompts_carry_the_dos_and_donts(self, workspace, organization, settings):
        from apps.ai_writer import services as ai
        from apps.ai_writer.services import AIContext

        settings.ANTHROPIC_API_KEY = "sk-ant-platform-key-for-tests"
        brand_kit.update(brand_kit.kit_for(organization=organization), {"dos": "Mention free shipping"})
        brand_kit.update(brand_kit.kit_for(workspace=workspace), {"donts": "Use exclamation marks"})
        with patch_anthropic() as fake:
            fake.queue({"text": "A caption"})
            ai.write_caption(AIContext(workspace=workspace), "Announce our sale")
        system = fake.systems[0]
        assert "Always: Mention free shipping" in system and "Never: Use exclamation marks" in system

    def test_no_rules_adds_nothing(self, workspace):
        assert brand_kit.prompt_block(workspace) == ""

    def test_reports_use_the_kit_colour_and_logo_with_fallbacks(self, workspace, logo):
        from types import SimpleNamespace

        from apps.reports import branding

        definition = SimpleNamespace(workspace=workspace, accent_color="", white_label=False)
        assert branding.branding_for(definition)["accent"] == branding.readable_accent("#aa2255")
        assert branding.branding_for(definition)["logo"] == ""
        brand_kit.update(
            brand_kit.kit_for(workspace=workspace),
            {"colors": [{"name": "Brand", "hex": "#0b6bcb"}], "logo_primary": str(logo.id)},
        )
        data = branding.branding_for(definition)
        assert data["accent"] == "#0b6bcb" and data["logo"].startswith("data:image/png;base64,")
        # A report's own accent still wins.
        definition.accent_color = "#123456"
        assert branding.branding_for(definition)["accent"] == "#123456"

    def test_composer_pickers_offer_disclaimers_and_hashtags(self, workspace, inbox_owner_for):
        brand_kit.update(
            brand_kit.kit_for(workspace=workspace),
            {"disclaimers": [{"name": "Legal", "text": "T&Cs apply"}], "default_hashtags": ["#brand"]},
        )
        client = inbox_owner_for(workspace)
        sigs = client.get(reverse("composer:signature_picker", kwargs={"workspace_id": workspace.id})).json()
        assert {
            "id": "brand-kit-0",
            "name": "Legal",
            "body": "T&Cs apply",
            "scope": "brand",
            "is_default": False,
        } in sigs["signatures"]
        tags = client.get(reverse("composer:hashtag_picker", kwargs={"workspace_id": workspace.id})).json()
        assert tags["sets"][0]["name"] == "Brand kit hashtags" and tags["sets"][0]["text"] == "#brand"


@pytest.fixture
def inbox_owner_for(client, user, org_owner):
    from apps.members.models import WorkspaceMembership

    def make(workspace, role=WorkspaceMembership.WorkspaceRole.OWNER):
        WorkspaceMembership.objects.get_or_create(user=user, workspace=workspace, defaults={"workspace_role": role})
        client.force_login(user)
        return client

    return make


class TestSettingsPages:
    def url(self, workspace):
        return reverse("workspaces:brand_voice_settings", kwargs={"workspace_id": workspace.id})

    def test_page_shows_voice_and_kit_and_writes_nothing_on_get(self, workspace, inbox_owner_for):
        client = inbox_owner_for(workspace)
        html = client.get(self.url(workspace)).content.decode()
        assert "Brand voice" in html and "Brand colours" in html and "Review reply policy" in html
        assert BrandKit.objects.count() == 0

    def test_owner_saves_each_section(self, workspace, inbox_owner_for):
        client = inbox_owner_for(workspace)
        client.post(self.url(workspace), {"section": "colors", "color_name": ["Sun", ""], "color_hex": ["#ff0000", ""]})
        client.post(
            self.url(workspace),
            {
                "section": "copy",
                "default_hashtags": "#a #b",
                "disclaimer_name": ["Legal"],
                "disclaimer_text": ["Hi"],
                "dos": "d",
                "donts": "n",
            },
        )
        client.post(self.url(workspace), {"section": "review", "review_reply_policy": "No refunds in public"})
        client.post(self.url(workspace), {"brand_voice_tone": "Warm"})
        kit = BrandKit.objects.get(workspace=workspace)
        workspace.refresh_from_db()
        assert kit.colors == [{"name": "Sun", "hex": "#ff0000"}] and kit.default_hashtags == ["#a", "#b"]
        assert kit.dos == "d" and workspace.review_reply_policy == "No refunds in public"
        assert workspace.brand_voice_tone == "Warm"

    def test_a_bad_value_is_reported_and_not_saved(self, workspace, inbox_owner_for):
        client = inbox_owner_for(workspace)
        response = client.post(
            self.url(workspace), {"section": "colors", "color_name": ["x"], "color_hex": ["nope"]}, follow=True
        )
        assert (
            "is not a hex colour" in response.content.decode() and not BrandKit.objects.filter(colors__len=1).exists()
        )

    def test_only_owners_and_managers_can_change_it(self, workspace, inbox_owner_for):
        from apps.members.models import WorkspaceMembership

        client = inbox_owner_for(workspace, WorkspaceMembership.WorkspaceRole.EDITOR)
        assert client.post(self.url(workspace), {"section": "copy", "dos": "x"}).status_code == 404
        assert not BrandKit.objects.exists()
        assert client.get(self.url(workspace)).status_code == 200

    def test_organization_page_is_for_admins(self, workspace, inbox_owner_for):
        client = inbox_owner_for(workspace)
        url = reverse("organizations:brand_kit")
        assert client.get(url).status_code == 200
        client.post(url, {"section": "copy", "dos": "Org rule"})
        kit = BrandKit.objects.get(organization__isnull=False)
        assert kit.dos == "Org rule" and kit.workspace_id is None


class TestAssetReferences:
    def test_a_font_points_at_a_pdf_of_this_brand(self, workspace, organization, logo):
        pdf = MediaAsset.objects.create(
            organization=organization, workspace=workspace, filename="spec.pdf", media_type="document"
        )
        kit = brand_kit.kit_for(workspace=workspace)
        with pytest.raises(brand_kit.BrandKitError):
            brand_kit.update(kit, {"fonts": [{"name": "Inter", "asset_id": str(logo.id)}]})
        brand_kit.update(kit, {"fonts": [{"name": "Inter", "asset_id": str(pdf.id)}]})
        assert kit.fonts[0]["asset_id"] == str(pdf.id)

    def test_the_orphan_sweep_keeps_logos_and_font_files(self, workspace, organization, logo):
        from apps.media_library.services import _fk_referenced_asset_ids

        pdf = MediaAsset.objects.create(
            organization=organization, workspace=workspace, filename="spec.pdf", media_type="document"
        )
        brand_kit.update(
            brand_kit.kit_for(workspace=workspace),
            {"logo_primary": str(logo.id), "fonts": [{"name": "Inter", "asset_id": str(pdf.id)}]},
        )
        assert {logo.id, pdf.id} <= _fk_referenced_asset_ids()
