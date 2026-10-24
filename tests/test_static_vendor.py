"""Production builds run collectstatic with a manifest storage that fails on any
reference to a file that isn't shipped, so a vendored script must not point at a
source map we don't vendor."""

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "static"


def test_vendored_scripts_reference_only_shipped_source_maps():
    for script in STATIC.rglob("*.js"):
        for name in re.findall(r"sourceMappingURL=([^\s*/]+)", script.read_text(errors="ignore")):
            assert (script.parent / name).exists(), f"{script.relative_to(STATIC)} points at missing {name}"
