"""Reading the brand kit settings forms (workspace and organization pages share them)."""

from __future__ import annotations

from . import brand_kit

# Sections of the settings page, each its own form.
SECTIONS = ("identity", "colors", "fonts", "copy")


def _rows(post, *names):
    """Parallel repeated inputs as dicts, skipping rows left entirely blank."""
    columns = [post.getlist(name) for name in names]
    rows = []
    for values in zip(*columns, strict=False):
        if any(v.strip() for v in values):
            rows.append(dict(zip(names, (v.strip() for v in values), strict=True)))
    return rows


def kit_data_from_post(post, section: str) -> dict:
    """The ``brand_kit.update`` data a section's form submitted."""
    if section == "identity":
        data = {f"logo_{slot}": post.get(f"logo_{slot}", "") for slot in brand_kit.LOGO_SLOTS}
        data["tagline"] = post.get("tagline", "")
        return data
    if section == "colors":
        return {
            "colors": [{"name": r["color_name"], "hex": r["color_hex"]} for r in _rows(post, "color_name", "color_hex")]
        }
    if section == "fonts":
        return {
            "fonts": [
                {"name": r["font_name"], "role": r["font_role"], "asset_id": r["font_asset"]}
                for r in _rows(post, "font_name", "font_role", "font_asset")
            ]
        }
    if section == "copy":
        return {
            "default_hashtags": post.get("default_hashtags", ""),
            "disclaimers": [
                {"name": r["disclaimer_name"], "text": r["disclaimer_text"]}
                for r in _rows(post, "disclaimer_name", "disclaimer_text")
            ],
            "dos": post.get("dos", ""),
            "donts": post.get("donts", ""),
        }
    raise brand_kit.BrandKitError("Unknown section.")


def page_context(kit) -> dict:
    """What the shared kit sections need to render ``kit`` (the scope's own values, not the resolved ones)."""
    assets = brand_kit._owned_assets(kit)
    return {
        "kit": kit,
        "logo_slots": [
            (slot, brand_kit.LOGO_LABELS[slot], getattr(kit, f"logo_{slot}_id")) for slot in brand_kit.LOGO_SLOTS
        ],
        "image_assets": list(assets.filter(media_type="image").order_by("filename")[:200]),
        "font_assets": list(assets.exclude(media_type="image").order_by("filename")[:200]),
        "color_rows": list(kit.colors) + [{"name": "", "hex": ""}] * 2,
        "font_rows": list(kit.fonts) + [{"name": "", "role": "other", "asset_id": ""}] * 2,
        "disclaimer_rows": list(kit.disclaimers) + [{"name": "", "text": ""}],
        "hashtags_text": " ".join(kit.default_hashtags),
        "font_roles": brand_kit.FONT_ROLES,
    }
