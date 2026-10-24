"""App shell, theme and shared-component regressions: landmarks, titles, RTL, CSP, contrast, components."""

import re
from pathlib import Path

import pytest
from django.conf import settings
from django.template import Context, Template, TemplateSyntaxError
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import translation

STYLES = Path(settings.BASE_DIR) / "theme" / "static_src" / "src" / "styles.css"


def _render(source, **context):
    return Template("{% load common_extras %}" + source).render(Context(context))


# --- shared components -------------------------------------------------------------------------


def test_icon_button_requires_label():
    html = _render('{% icon_button label="Close dialog" icon="close" click="open = false" %}')
    assert 'aria-label="Close dialog"' in html
    assert 'title="Close dialog"' in html
    assert 'type="button"' in html
    assert '@click="open = false"' in html
    assert 'aria-hidden="true"' in html  # the svg is decorative
    with pytest.raises(TemplateSyntaxError):
        _render('{% icon_button label="" icon="close" %}')
    with pytest.raises(TemplateSyntaxError):
        _render('{% icon_button icon="close" %}')


def test_icon_button_link_and_disclosure_attributes():
    html = _render(
        '{% icon_button label="Open menu" icon="menu" href="/x/" expanded="menuOpen" controls="menu" id="m" %}'
    )
    assert html.startswith('<a href="/x/"')
    assert ':aria-expanded="menuOpen"' in html and 'aria-controls="menu"' in html and 'id="m"' in html


def test_modal_is_an_accessible_dialog():
    html = _render(
        '{% modal open="confirmOpen" title="Delete post" description="Gone for good." %}<p>Body</p>{% endmodal %}'
    )
    assert 'role="dialog"' in html and 'aria-modal="true"' in html
    labelled_by = re.search(r'aria-labelledby="([^"]+)"', html).group(1)
    described_by = re.search(r'aria-describedby="([^"]+)"', html).group(1)
    assert f'id="{labelled_by}"' in html and f'id="{described_by}"' in html
    assert 'x-pd-dialog="confirmOpen"' in html
    assert 'aria-label="Close dialog"' in html and "confirmOpen = false" in html
    assert "x-teleport" in html and "<p>Body</p>" in html
    assert "x-teleport" not in _render('{% modal open="o" title="T" teleport=False %}x{% endmodal %}')


def test_modal_requires_a_title():
    with pytest.raises(TemplateSyntaxError):
        _render('{% modal open="o" title="" %}x{% endmodal %}')


# --- landmarks, titles, skip link --------------------------------------------------------------


AUTH_PAGES = ["account_login", "account_signup", "account_reset_password"]


@pytest.mark.django_db
@pytest.mark.parametrize("name", AUTH_PAGES)
def test_auth_pages_have_skip_link_single_main_and_convention_title(client, name):
    html = client.get(reverse(name)).content.decode()
    assert 'class="pd-skip-link"' in html and 'href="#main-content"' in html
    assert html.count("<main") == 1 and 'id="main-content"' in html
    assert re.search(r"<title>[^<·]+ · PostDelegate</title>", html)
    assert "<footer" in html


@pytest.mark.django_db
@pytest.mark.parametrize("name", ["terms", "privacy", "data_deletion", "subprocessors", "licenses"])
def test_legal_pages_have_title_skip_link_and_single_main(client, name):
    html = client.get(reverse(f"legal:{name}")).content.decode()
    assert re.search(r"<title>[^<·]+ · PostDelegate</title>", html)
    assert 'href="#legal-content"' in html and html.count("<main") == 1


@pytest.mark.django_db
def test_signed_in_shell_landmarks_and_labels(client, org_owner):
    client.force_login(org_owner)
    html = client.get(reverse("notifications:list")).content.decode()
    assert html.count("<main") == 1 and 'id="main-content"' in html
    assert re.search(r'<nav[^>]*aria-label="Primary"', html)
    assert 'id="mobile-menu-btn"' in html and 'aria-label="Open menu"' in html
    assert 'aria-controls="app-sidebar"' in html and 'id="app-sidebar"' in html
    assert 'aria-label="Collapse sidebar"' in html
    assert "x-pd-dialog" in html  # the dialog directive is registered and used by the drawer
    assert re.search(r"<title>[^<]+ · PostDelegate</title>", html)
    # one banner at any breakpoint: the sidebar header and the mobile top bar exclude each other
    assert 'class="hidden lg:block"' in html and "pd-topbar lg:hidden" in html


def test_settings_layouts_follow_title_convention():
    rendered = render_to_string("layouts/settings.html", {"brand_name": "PostDelegate"})
    assert "<title>Settings · Settings · PostDelegate</title>" in rendered


# --- RTL / language ----------------------------------------------------------------------------


@pytest.mark.django_db
def test_html_lang_and_dir_follow_active_language(client):
    with translation.override("en"):
        html = client.get(reverse("account_login")).content.decode()
    assert '<html lang="en" dir="ltr"' in html
    with translation.override("ar"):
        assert translation.get_language_bidi()
        from django.template.loader import render_to_string
        from django.test import RequestFactory

        html = render_to_string("legal/base.html", {"brand_name": "PostDelegate"}, request=RequestFactory().get("/"))
    assert '<html lang="ar" dir="rtl"' in html


def test_languages_include_arabic():
    assert {code for code, _ in settings.LANGUAGES} == {"en", "ar"}


def test_arabic_font_is_bundled_with_license():
    fonts = Path(settings.BASE_DIR) / "theme" / "static" / "fonts"
    assert (fonts / "noto-sans-arabic-arabic.woff2").stat().st_size > 10_000
    assert "SIL OPEN FONT LICENSE" in (fonts / "NotoSansArabic-OFL.txt").read_text().upper()
    css = (Path(settings.BASE_DIR) / "theme" / "static" / "css" / "postdelegate.css").read_text()
    assert (
        "font-family: 'Noto Sans Arabic'" in css and "unicode-range: U+0600-06FF" in css and "font-display: swap" in css
    )


@pytest.mark.django_db
def test_licenses_page_lists_new_fonts_and_swagger(client):
    html = client.get(reverse("legal:licenses")).content.decode()
    assert "Noto Sans Arabic" in html and "Swagger UI" in html and "5.21.0" in html
    assert "jsdelivr" not in html


# --- CSP and self-hosted API docs ---------------------------------------------------------------


def test_csp_allows_no_cdn_host():
    for name in ("CSP_SCRIPT_SRC", "CSP_STYLE_SRC", "CSP_CONNECT_SRC", "CSP_FONT_SRC", "CSP_DEFAULT_SRC"):
        assert not any("jsdelivr" in str(src) or "cdn." in str(src) for src in getattr(settings, name)), name


@pytest.mark.django_db
def test_api_docs_page_is_self_hosted(client):
    html = client.get("/api/v1/docs").content.decode()
    assert "/static/vendor/swagger-ui-5.21.0/swagger-ui-bundle.js" in html
    assert "cdn.jsdelivr.net" not in html and "django-ninja.dev" not in html
    root = Path(settings.BASE_DIR) / "static" / "vendor" / "swagger-ui-5.21.0"
    assert {p.name for p in root.iterdir()} >= {"swagger-ui.css", "swagger-ui-bundle.js", "LICENSE", "NOTICE"}


# --- theme contrast ----------------------------------------------------------------------------


def _luminance(hex_color):
    channels = [int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _contrast(a, b):
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _tokens():
    return dict(re.findall(r"(--[a-z0-9-]+):\s*(#[0-9a-fA-F]{6})\b", STYLES.read_text().split("/* Chart series")[0]))


SURFACES = ["--surface-page", "--surface-0", "--surface-1", "--surface-2", "--surface-3"]


@pytest.mark.parametrize(
    "token",
    [
        "--text-primary",
        "--text-secondary",
        "--text-tertiary",
        "--text-ghost",
        "--brand-400",
        "--brand-700",
        "--success-700",
        "--error-700",
        "--warning-700",
        "--info-700",
    ],
)
def test_text_tokens_pass_aa_on_every_surface(token):
    tokens = _tokens()
    for surface in SURFACES:
        assert _contrast(tokens[token], tokens[surface]) >= 4.5, (token, surface)


def test_brand_fill_carries_white_text_and_text_brand_is_the_light_shade():
    tokens = _tokens()
    assert _contrast("#ffffff", tokens["--brand-500"]) >= 4.5
    assert _contrast("#ffffff", tokens["--brand-600"]) >= 4.5
    assert re.search(r"--text-brand:\s*var\(--brand-400\)", STYLES.read_text())


def test_input_border_meets_non_text_contrast():
    tokens = _tokens()
    for surface in ("--surface-page", "--surface-0", "--surface-1"):
        assert _contrast(tokens["--border-input"], tokens[surface]) >= 3.0, surface


def test_stylesheet_has_no_off_token_hex_outside_the_palette_blocks():
    css = STYLES.read_text()
    body = css.split("@theme inline", 1)[1]
    body = body.split("}", 1)[1]  # skip the @theme block itself
    # literal colours are only allowed inside the documented :root palette (above @theme)
    assert not re.findall(
        r"(?<![&\w%])#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b(?![\w-])", re.sub(r"url\([^)]*\)", "", body)
    )
