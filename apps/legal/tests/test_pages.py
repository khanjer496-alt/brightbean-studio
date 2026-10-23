import pytest
from django.test import override_settings
from django.urls import reverse

PAGES = ["legal:terms", "legal:privacy", "legal:data_deletion", "legal:licenses", "legal:subprocessors"]


@pytest.mark.django_db
@pytest.mark.parametrize("name", PAGES)
def test_pages_render_without_login(client, name):
    response = client.get(reverse(name))
    assert response.status_code == 200
    html = response.content.decode()
    assert "<h1>" in html
    assert 'lang="en"' in html
    assert 'style="' not in html.split("<main", 1)[1].split("</main>")[0]


@pytest.mark.django_db
@pytest.mark.parametrize("name", PAGES)
def test_pages_are_marked_as_draft_in_a_hidden_comment(client, name):
    html = client.get(reverse(name)).content.decode()
    assert "<!-- DRAFT" in html
    visible = html.split("<main", 1)[1]
    assert "DRAFT" not in visible


@pytest.mark.django_db
def test_legal_pages_are_reachable_before_terms_are_accepted(client, django_user_model):
    user = django_user_model.objects.create_user(email="new@example.com", password="pw-12345-abc")
    client.force_login(user)
    assert user.tos_accepted_at is None
    assert client.get(reverse("legal:terms")).status_code == 200


@pytest.mark.django_db
@override_settings(
    BRAND_LEGAL_NAME="Acme Test LLC",
    LEGAL_ADDRESS="1 Test Street, Cheyenne",
    LEGAL_EFFECTIVE_DATE="1 January 2030",
    LEGAL_GOVERNING_LAW="the State of Testland",
    BRAND_SUPPORT_EMAIL="help@example.test",
)
def test_values_come_from_settings(client):
    terms = client.get(reverse("legal:terms")).content.decode()
    assert "Acme Test LLC" in terms
    assert "1 January 2030" in terms
    assert "the State of Testland" in terms
    assert "1 Test Street, Cheyenne" in terms
    assert "help@example.test" in terms


@pytest.mark.django_db
def test_missing_address_and_date_show_placeholders(client):
    with override_settings(LEGAL_ADDRESS="", LEGAL_EFFECTIVE_DATE=""):
        html = client.get(reverse("legal:privacy")).content.decode()
    assert "[Registered business address to be completed]" in html
    assert "[Effective date to be completed]" in html


@pytest.mark.django_db
def test_privacy_has_required_platform_disclosures(client):
    html = client.get(reverse("legal:privacy")).content.decode()
    assert "Google API Services User Data Policy" in html
    assert "Limited Use" in html
    assert "https://www.youtube.com/t/terms" in html
    assert "http://www.google.com/policies/privacy" in html
    assert "https://security.google.com/settings/security/permissions" in html
    assert "Meta Platform Terms" in html
    for platform in ("TikTok", "LinkedIn", "Pinterest", "Reddit", "Discord", "Slack", "Telegram"):
        assert platform in html
    assert "CCPA" in html
    assert "GDPR" in html
    assert "under 16" in html


@pytest.mark.django_db
@override_settings(SENTRY_DSN="")
def test_sentry_only_listed_when_configured(client):
    assert "Sentry" not in client.get(reverse("legal:subprocessors")).content.decode()
    with override_settings(SENTRY_DSN="https://key@example.ingest.sentry.io/1"):
        assert "Sentry" in client.get(reverse("legal:subprocessors")).content.decode()


@pytest.mark.django_db
def test_subprocessors_list_the_providers(client):
    html = client.get(reverse("legal:subprocessors")).content.decode()
    for name in ("Hetzner", "Cloudflare", "Stripe", "Anthropic", "Resend"):
        assert name in html


@pytest.mark.django_db
def test_licenses_show_source_links_and_assets(client):
    with override_settings(SOURCE_CODE_URL="https://example.test/our/source"):
        html = client.get(reverse("legal:licenses")).content.decode()
    assert "https://example.test/our/source" in html
    assert "brightbeanxyz/brightbean-studio" in html
    for asset in ("Chart.js", "flatpickr", "SortableJS", "htmx", "Alpine.js", "Cropper.js", "Manrope"):
        assert asset in html


def test_brand_urls_default_to_the_legal_routes():
    from django.conf import settings

    assert settings.BRAND_TERMS_URL.endswith("/legal/terms/")
    assert settings.BRAND_PRIVACY_URL.endswith("/legal/privacy/")


@pytest.mark.django_db
def test_login_and_signup_footers_link_to_legal_pages(client):
    for name in ("account_login", "account_signup"):
        html = client.get(reverse(name)).content.decode()
        assert "/legal/licenses/" in html
        assert "/legal/terms/" in html
        assert "/legal/privacy/" in html


@pytest.mark.django_db
@override_settings(BRAND_TERMS_URL="https://elsewhere.test/tos", BRAND_PRIVACY_URL="https://elsewhere.test/priv")
def test_custom_brand_urls_are_kept(client):
    html = client.get(reverse("account_login")).content.decode()
    assert "https://elsewhere.test/tos" in html
    assert "https://elsewhere.test/priv" in html


@pytest.mark.django_db
def test_in_app_menu_links_to_legal_pages(client, org_owner):
    client.force_login(org_owner)
    response = client.get(reverse("accounts:settings"))
    html = response.content.decode()
    assert "/legal/terms/" in html
    assert "/legal/licenses/" in html


@pytest.mark.django_db
def test_accept_terms_page_links_to_policies(client, django_user_model):
    user = django_user_model.objects.create_user(email="social@example.com", password="pw-12345-abc")
    client.force_login(user)
    html = client.get(reverse("accounts:accept_terms")).content.decode()
    assert "/legal/terms/" in html
    assert "/legal/privacy/" in html
