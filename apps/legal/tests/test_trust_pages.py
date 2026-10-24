import re

import pytest
from django.test import override_settings
from django.urls import reverse


@pytest.mark.django_db
def test_security_page_has_contact_and_no_certification_claims(client):
    html = client.get(reverse("legal:security")).content.decode()
    assert "security@postdelegate.com" in html
    assert "/.well-known/security.txt" in html
    assert "AES-256-GCM" in html and "Planned: nightly" in html
    assert "[Hosting region to be confirmed]" in html
    assert "do not hold any security certification" in html


@pytest.mark.django_db
def test_dpa_links_and_placeholders(client):
    html = client.get(reverse("legal:dpa")).content.decode()
    assert reverse("legal:subprocessors") in html and reverse("legal:security") in html
    assert "[Effective date to be completed]" in html
    assert "[Registered business address to be completed]" in html
    assert "72 hours" in html and "Standard Contractual Clauses" in html
    main = html.split("<main", 1)[1]
    assert "part of the" in main.split("<h2>")[0]


@pytest.mark.django_db
def test_security_txt_fields(client):
    response = client.get("/.well-known/security.txt")
    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/plain")
    body = response.content.decode()
    fields = dict(line.split(": ", 1) for line in body.strip().splitlines())
    assert fields["Contact"] == "mailto:security@postdelegate.com"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", fields["Expires"])
    assert fields["Preferred-Languages"].startswith("en")
    assert fields["Policy"].endswith("/legal/security/")
    assert fields["Canonical"].endswith("/.well-known/security.txt")


@pytest.mark.django_db
def test_security_txt_reachable_when_terms_not_accepted(client, django_user_model):
    user = django_user_model.objects.create_user(email="n@example.com", password="pw-12345-abc")
    client.force_login(user)
    assert client.get("/.well-known/security.txt").status_code == 200
    assert client.get(reverse("roadmap:index")).status_code == 200


@pytest.mark.django_db
def test_footer_links_new_pages(client):
    html = client.get(reverse("legal:terms")).content.decode()
    for name in ("legal:security", "legal:dpa", "roadmap:index"):
        assert reverse(name) in html
    with override_settings(STATUS_PAGE_URL="https://status.example.test"):
        assert ">Status<" in client.get(reverse("account_login")).content.decode()
