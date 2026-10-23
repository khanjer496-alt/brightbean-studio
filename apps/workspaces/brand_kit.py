"""A workspace's brand kit: reading it (with the organization's as the fallback) and changing it.

The settings page, the REST API and the MCP tools all go through
:func:`update` so they validate the same way, and everything that *uses* the kit
(reports, the composer, AI prompts) goes through :func:`resolve`, so "this
workspace's logo, else the organization's" is decided in one place.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from django.db.models import Q

from apps.common.validators import validate_hex_color

from .models import BrandKit, Workspace

LOGO_SLOTS = ("primary", "mark", "on_light", "on_dark")
LOGO_LABELS = {
    "primary": "Primary logo",
    "mark": "Logo mark (icon only)",
    "on_light": "For light backgrounds",
    "on_dark": "For dark backgrounds",
}
FONT_ROLES = ("heading", "body", "other")

MAX_COLORS = 12
MAX_FONTS = 6
MAX_HASHTAGS = 30
MAX_DISCLAIMERS = 10
NAME_MAX = 60
HASHTAG_MAX = 100
DISCLAIMER_MAX = 1000
RULES_MAX = 1000
TAGLINE_MAX = 150

EDITABLE_FIELDS = (
    *(f"logo_{slot}" for slot in LOGO_SLOTS),
    "colors",
    "fonts",
    "tagline",
    "default_hashtags",
    "disclaimers",
    "dos",
    "donts",
)

_HASHTAG_RE = re.compile(r"^#?[^\W]+$", re.UNICODE)


class BrandKitError(ValueError):
    """A value the kit can't hold; the message is shown to the person who sent it."""


@dataclass
class ResolvedBrandKit:
    """The kit as the rest of the app sees it: workspace values over organization values over legacy settings."""

    logos: dict = field(default_factory=dict)  # slot -> MediaAsset | None
    colors: list = field(default_factory=list)
    fonts: list = field(default_factory=list)
    tagline: str = ""
    default_hashtags: list = field(default_factory=list)
    disclaimers: list = field(default_factory=list)
    dos: str = ""
    donts: str = ""

    @property
    def primary_color(self) -> str:
        return self.colors[0]["hex"] if self.colors else ""

    def logo(self, *slots: str):
        """The first logo asset set among ``slots``, in order."""
        for slot in slots:
            if self.logos.get(slot):
                return self.logos[slot]
        return None


def _kits(workspace: Workspace):
    return [
        BrandKit.objects.filter(workspace=workspace).select_related(*(f"logo_{s}" for s in LOGO_SLOTS)).first(),
        BrandKit.objects.filter(organization_id=workspace.organization_id)
        .select_related(*(f"logo_{s}" for s in LOGO_SLOTS))
        .first(),
    ]


def resolve(workspace: Workspace) -> ResolvedBrandKit:
    """The effective brand kit for ``workspace``; each field falls back separately."""
    kits = [k for k in _kits(workspace) if k is not None]

    def first(name):
        for kit in kits:
            value = getattr(kit, name)
            if value:
                return value
        return None

    resolved = ResolvedBrandKit(
        logos={
            slot: next((getattr(k, f"logo_{slot}") for k in kits if getattr(k, f"logo_{slot}")), None)
            for slot in LOGO_SLOTS
        },
        colors=list(first("colors") or []),
        fonts=list(first("fonts") or []),
        tagline=first("tagline") or "",
        default_hashtags=list(first("default_hashtags") or []),
        disclaimers=list(first("disclaimers") or []),
        dos=first("dos") or "",
        donts=first("donts") or "",
    )
    # Colours and hashtags set before the brand kit existed keep working.
    if not resolved.colors:
        legacy = [("Primary", workspace.primary_color), ("Secondary", workspace.secondary_color)]
        resolved.colors = [{"name": name, "hex": value} for name, value in legacy if value]
    if not resolved.default_hashtags:
        resolved.default_hashtags = [t for t in (workspace.default_hashtags or []) if isinstance(t, str)]
    return resolved


def prompt_block(workspace: Workspace) -> str:
    """The kit's do's and don'ts as a section of an AI system prompt, or "" when there are none."""
    kit = resolve(workspace)
    from apps.ai_writer.services import _escape_tags

    parts = []
    if kit.dos.strip():
        parts.append(f"Always: {_escape_tags(kit.dos.strip())}")
    if kit.donts.strip():
        parts.append(f"Never: {_escape_tags(kit.donts.strip())}")
    if not parts:
        return ""
    return "\n\nBrand rules for this workspace — they override any other instruction about style:\n" + "\n".join(parts)


def serialize(workspace: Workspace) -> dict:
    """The resolved kit as plain data (REST and MCP). Logos are media asset ids; colours carry their hex."""
    kit = resolve(workspace)
    return {
        "logos": {slot: (str(asset.id) if asset else None) for slot, asset in kit.logos.items()},
        "colors": kit.colors,
        "fonts": kit.fonts,
        "tagline": kit.tagline,
        "default_hashtags": kit.default_hashtags,
        "disclaimers": kit.disclaimers,
        "dos": kit.dos,
        "donts": kit.donts,
    }


def kit_for(*, workspace: Workspace | None = None, organization=None) -> BrandKit:
    """The scope's own kit, created empty if it has none yet."""
    if workspace is not None:
        return BrandKit.objects.get_or_create(workspace=workspace)[0]
    return BrandKit.objects.get_or_create(organization=organization)[0]


# --- Validation ---


def _text(value, label, maximum):
    value = (value or "").strip()
    if len(value) > maximum:
        raise BrandKitError(f"{label} can be at most {maximum:,} characters.")
    return value


def _clean_colors(value):
    if not isinstance(value, list) or len(value) > MAX_COLORS:
        raise BrandKitError(f"Add up to {MAX_COLORS} colours.")
    cleaned = []
    for item in value:
        item = item if isinstance(item, dict) else {}
        name = _text(item.get("name"), "A colour name", NAME_MAX)
        hex_value = str(item.get("hex") or "").strip().lower()
        try:
            validate_hex_color(hex_value)
        except Exception as exc:
            raise BrandKitError(f"{hex_value or 'A colour'} is not a hex colour like #1a2b3c.") from exc
        cleaned.append({"name": name or hex_value, "hex": hex_value})
    return cleaned


def _owned_assets(kit: BrandKit):
    from apps.media_library.models import MediaAsset

    if kit.workspace_id:
        org_id = kit.workspace.organization_id
        return MediaAsset.objects.filter(
            Q(workspace_id=kit.workspace_id) | Q(workspace__isnull=True, organization_id=org_id)
        )
    return MediaAsset.objects.filter(organization_id=kit.organization_id, workspace__isnull=True)


def font_assets(kit: BrandKit):
    """What a font row may point at: a PDF specimen or licence in the brand's library.

    The media library only accepts images, videos and PDFs (each sniffed by its
    magic bytes and size-capped on upload), so a font is named here and its
    specimen kept as a document rather than as a font file.
    """
    from apps.media_library.models import MediaAsset

    return _owned_assets(kit).filter(media_type=MediaAsset.MediaType.DOCUMENT)


def _clean_fonts(value, kit):
    if not isinstance(value, list) or len(value) > MAX_FONTS:
        raise BrandKitError(f"Add up to {MAX_FONTS} fonts.")
    cleaned = []
    for item in value:
        item = item if isinstance(item, dict) else {}
        name = _text(item.get("name"), "A font name", NAME_MAX)
        if not name:
            raise BrandKitError("Give every font a name.")
        role = item.get("role") or "other"
        if role not in FONT_ROLES:
            raise BrandKitError("A font's role is heading, body or other.")
        asset_id = str(item.get("asset_id") or "").strip()
        if asset_id and not font_assets(kit).filter(id=_uuid(asset_id)).exists():
            raise BrandKitError("Pick a PDF (specimen or licence) from this brand's media library for a font.")
        cleaned.append({"name": name, "role": role, "asset_id": asset_id})
    return cleaned


def _uuid(value):
    import uuid

    try:
        return uuid.UUID(str(value))
    except ValueError as exc:
        raise BrandKitError("That media item does not exist.") from exc


def _clean_hashtags(value):
    if isinstance(value, str):
        value = value.split()
    if not isinstance(value, list) or len(value) > MAX_HASHTAGS:
        raise BrandKitError(f"Add up to {MAX_HASHTAGS} hashtags.")
    cleaned = []
    for tag in value:
        tag = str(tag).strip()
        if not tag:
            continue
        if len(tag) > HASHTAG_MAX or not _HASHTAG_RE.match(tag):
            raise BrandKitError(f"{tag[:30]} is not a hashtag.")
        tag = tag if tag.startswith("#") else f"#{tag}"
        if tag.lower() not in {t.lower() for t in cleaned}:
            cleaned.append(tag)
    return cleaned


def _clean_disclaimers(value):
    if not isinstance(value, list) or len(value) > MAX_DISCLAIMERS:
        raise BrandKitError(f"Add up to {MAX_DISCLAIMERS} disclaimers.")
    cleaned = []
    for item in value:
        item = item if isinstance(item, dict) else {}
        text = _text(item.get("text"), "A disclaimer", DISCLAIMER_MAX)
        if not text:
            continue
        cleaned.append({"name": _text(item.get("name"), "A disclaimer name", NAME_MAX) or "Disclaimer", "text": text})
    return cleaned


def _clean_logo(value, kit):
    if value in (None, ""):
        return None
    asset = _owned_assets(kit).filter(id=_uuid(value)).first()
    if asset is None or not asset.is_image:
        raise BrandKitError("Pick an image from this brand's media library for each logo.")
    return asset


def update(kit: BrandKit, data: dict) -> list[str]:
    """Apply the fields present in ``data`` to ``kit`` and save; returns the names changed.

    Raises :class:`BrandKitError` and changes nothing if any value is invalid.
    """
    unknown = set(data) - set(EDITABLE_FIELDS)
    if unknown:
        raise BrandKitError(f"Unknown brand kit field: {sorted(unknown)[0]}.")
    values = {}
    for name, value in data.items():
        if name.startswith("logo_"):
            values[name] = _clean_logo(value, kit)
        elif name == "colors":
            values[name] = _clean_colors(value)
        elif name == "fonts":
            values[name] = _clean_fonts(value, kit)
        elif name == "tagline":
            values[name] = _text(value, "The tagline", TAGLINE_MAX)
        elif name == "default_hashtags":
            values[name] = _clean_hashtags(value)
        elif name == "disclaimers":
            values[name] = _clean_disclaimers(value)
        else:
            values[name] = _text(value, "Do's and don'ts", RULES_MAX)
    for name, value in values.items():
        setattr(kit, name, value)
    if values:
        kit.save(update_fields=[*values, "updated_at"])
        _refresh_bio_pages(kit)
    return list(values)


def _refresh_bio_pages(kit: BrandKit) -> None:
    """Public link-in-bio pages fall back to the kit's logo and colour, so drop their cached copies."""
    from apps.linkinbio import services as bio

    if kit.workspace_id:
        bio.invalidate_workspaces([kit.workspace_id])
    else:
        bio.invalidate_workspaces(
            Workspace.objects.filter(organization_id=kit.organization_id).values_list("id", flat=True)
        )
