"""Template lint for UI stability: nothing that mutates data can be fired twice, nothing flashes or shifts on load.

Each test scans every template and fails with the file:line of each offender.

PENDING_SHELL lists the files owned by the shell/mobile redesign (base layout and shared components). They are checked
by the HTMX-guard test like everything else but are skipped by the markup checks below until that redesign lands;
shrink this tuple, never grow it.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = sorted((ROOT / "templates").rglob("*.html"))

PENDING_SHELL = ("templates/base.html", "templates/layouts/", "templates/components/")

# One opening tag, tolerating Django tags and quoted attribute values that contain ">".
TAG = re.compile(r"<([a-zA-Z][\w-]*)((?:\{%.*?%\}|\{\{.*?\}\}|\"[^\"]*\"|'[^']*'|[^>\"'])*)>", re.S)


def tags(path):
    text = path.read_text()
    for match in TAG.finditer(text):
        yield match.group(1).lower(), match.group(2), text.count("\n", 0, match.start()) + 1


def rel(path):
    return str(path.relative_to(ROOT))


def pending(path):
    return rel(path).startswith(PENDING_SHELL)


def has_attr(attrs, name):
    return re.search(rf"(?<![\w:@.-]){re.escape(name)}(?=[\s=/]|$)", attrs) is not None


def attr(attrs, name):
    match = re.search(rf"(?<![\w:@.-]){re.escape(name)}\s*=\s*(\"([^\"]*)\"|'([^']*)')", attrs)
    return (match.group(2) if match.group(2) is not None else match.group(3)) if match else None


# --- HTMX mutations -------------------------------------------------------------------------------------------------

MUTATING = ("hx-post", "hx-put", "hx-patch", "hx-delete")
# What static/js/form-guard.js locks while a request is in flight: buttons, submit inputs, links and forms.
GLOBALLY_LOCKED_TAGS = {"button", "a", "form"}
# Attributes that give an element its own real protection against a second concurrent request.
EXPLICIT_GUARDS = ("hx-disabled-elt", "hx-sync")


def test_form_guard_is_wired_into_the_page_and_covers_every_mutating_verb():
    base = (ROOT / "templates" / "base.html").read_text()
    assert re.search(r"<script[^>]+js/form-guard\.js", base), "base.html must load static/js/form-guard.js"
    js = (ROOT / "static" / "js" / "form-guard.js").read_text()
    assert re.search(r"VERBS = \['post', 'put', 'patch', 'delete'\]", js)
    assert "htmx:beforeRequest" in js and "htmx:afterRequest" in js
    assert "data-no-lock" in js


def mutating_offenders():
    offenders = []
    for path in TEMPLATES:
        for name, attrs, line in tags(path):
            if not any(has_attr(attrs, verb) for verb in MUTATING):
                continue
            where = f"{rel(path)}:{line} <{name}>"
            explicit = any(has_attr(attrs, guard) for guard in EXPLICIT_GUARDS)
            if has_attr(attrs, "data-no-lock") and not explicit:
                offenders.append(f"{where} opts out with data-no-lock but has no hx-sync/hx-disabled-elt")
            elif name == "input" and (attr(attrs, "type") or "").lower() in {"submit", "button", "image"}:
                continue
            elif name not in GLOBALLY_LOCKED_TAGS and not explicit:
                # div/tr/select/input...: the global guard only marks these busy, so they need their own guard.
                offenders.append(f"{where} is not a button/link/form: add hx-sync or hx-disabled-elt")
    return offenders


def test_every_htmx_mutation_is_locked_while_in_flight():
    assert mutating_offenders() == []


def test_ai_writer_actions_share_one_single_flight_group():
    """Write/improve/adapt/hashtags/link/save must never run twice or in parallel (they spend the user's own AI quota)."""
    missing = []
    for path in (ROOT / "templates" / "ai_writer" / "partials").glob("*.html"):
        for _name, attrs, line in tags(path):
            if has_attr(attrs, "hx-post") and attr(attrs, "hx-sync") != "#ai-panel:drop":
                missing.append(f"{rel(path)}:{line}")
    assert missing == []


# --- Alpine: no flash of content that starts hidden ----------------------------------------------------------------


def test_x_show_elements_are_cloaked_until_alpine_boots():
    offenders = []
    for path in TEMPLATES:
        if pending(path):
            continue
        for name, attrs, line in tags(path):
            expression = re.search(r"(?<![\w:@.-])x-show(?:\.[\w.]+)?\s*=\s*\"([^\"]*)\"", attrs)
            if not expression:
                continue
            starts_visible = expression.group(1).strip().startswith("!") or expression.group(1).strip() == "true"
            if not starts_visible and not has_attr(attrs, "x-cloak"):
                offenders.append(f'{rel(path)}:{line} <{name}> x-show="{expression.group(1)[:40]}"')
    assert offenders == []


def test_x_cloak_rule_exists():
    css = (ROOT / "static" / "css" / "stability.css").read_text()
    assert re.search(r"\[x-cloak\]\s*\{\s*display:\s*none\s*!important", css)


# --- Images: reserve their space -----------------------------------------------------------------------------------

# Email templates are rendered by mail clients, which ignore the stylesheet and have their own rules.
IMAGE_EXEMPT = ("templates/reports/email/", "templates/approvals/email/", "templates/notifications/email/")


def image_has_reserved_space(attrs):
    if has_attr(attrs, "width") and has_attr(attrs, "height"):
        return True
    classes = attr(attrs, "class") or ""
    if re.search(r"(?<![\w-])aspect-", classes) or re.search(r"(?<![\w-])size-", classes):
        return True
    return bool(re.search(r"(?<![\w-])w-", classes) and re.search(r"(?<![\w-])h-", classes))


def test_images_declare_their_size_so_the_page_does_not_jump():
    offenders = []
    for path in TEMPLATES:
        if pending(path) or rel(path).startswith(IMAGE_EXEMPT):
            continue
        for name, attrs, line in tags(path):
            if name == "img" and not image_has_reserved_space(attrs):
                offenders.append(f"{rel(path)}:{line}")
    assert offenders == []


# --- Focus rings ---------------------------------------------------------------------------------------------------

# Classes that draw their own focus state in styles.css, or utilities that do it inline.
FOCUS_REPLACEMENT = re.compile(
    r"focus-visible:|focus:|focus-within:|(?<![\w-])(input-focus|input-focus-surface|focus-ring|ml-search-input)(?![\w-])"
)


def test_outline_none_always_comes_with_a_visible_focus_style():
    offenders = []
    for path in TEMPLATES:
        if pending(path):
            continue
        for name, attrs, line in tags(path):
            classes = attr(attrs, "class") or ""
            if not re.search(r"(?<![\w:-])outline-none", classes):
                continue
            programmatic_focus_only = attr(attrs, "tabindex") == "-1"
            if not programmatic_focus_only and not FOCUS_REPLACEMENT.search(classes):
                offenders.append(f"{rel(path)}:{line} <{name}>")
    assert offenders == []


# --- Viewport units ------------------------------------------------------------------------------------------------


def test_no_100vh_or_h_screen_that_breaks_under_mobile_browser_chrome():
    offenders = []
    css = [p for p in (ROOT / "static" / "css").glob("*.css")]
    for path in [*TEMPLATES, *css]:
        if pending(path):
            continue
        text = path.read_text()
        for number, line in enumerate(text.splitlines(), 1):
            if re.search(r"(?<![\w.])100vh", line) or re.search(r"(?<![\w-])(min-|max-)?h-screen(?![\w-])", line):
                offenders.append(f"{rel(path)}:{number}")
    assert offenders == []
