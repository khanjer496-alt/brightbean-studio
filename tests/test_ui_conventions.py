"""Template-level UI conventions that are cheap to regress: one confirm dialog, no implicit full-width buttons."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = sorted((ROOT / "templates").rglob("*.html"))


def test_no_native_confirm_dialogs():
    """Confirmations use the shared dialog (hx-confirm / data-confirm), never window.confirm()."""
    offenders = [str(p.relative_to(ROOT)) for p in TEMPLATES if re.search(r"(?<![\w.-])confirm\s*\(", p.read_text())]
    offenders += [
        str(p.relative_to(ROOT))
        for p in (ROOT / "static" / "js").glob("*.js")
        if not p.name.endswith(".min.js") and p.name != "confirm-dialog.js" and re.search(r"(?<![\w.-])confirm\s*\(", p.read_text())
    ]
    assert offenders == []


def test_confirm_dialog_is_in_the_base_template():
    base = (ROOT / "templates" / "base.html").read_text()
    assert "components/_confirm_dialog.html" in base
    assert "js/confirm-dialog.js" in base


def test_btn_brand_defaults_to_auto_width_with_explicit_block_modifier():
    css = (ROOT / "theme" / "static_src" / "src" / "styles.css").read_text()
    brand = re.search(r"\n  \.btn-brand \{(.*?)\n  \}", css, re.S).group(1)
    assert "width" not in brand
    assert re.search(r"\.btn-brand--block \{\s*width: 100%;", css)
