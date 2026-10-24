import pytest
from django.test import override_settings
from django.urls import reverse

from apps.roadmap.models import RoadmapItem


@pytest.mark.django_db
def test_roadmap_is_public_and_seeded(client):
    response = client.get(reverse("roadmap:index"))
    assert response.status_code == 200
    html = response.content.decode()
    for title in ("More networks", "Mobile app (PWA)", "White-label for agencies", "Report builder"):
        assert title in html
    assert "Arabic" in html
    assert html.index(">Now<") < html.index(">Next<") < html.index(">Later<")


@pytest.mark.django_db
def test_items_are_grouped_by_status_and_sorted_by_order(client):
    RoadmapItem.objects.all().delete()
    RoadmapItem.objects.create(title="Later B", status="later", order=2)
    RoadmapItem.objects.create(title="Later A", status="later", order=1)
    RoadmapItem.objects.create(title="Soon", status="next", order=1)
    RoadmapItem.objects.create(title="Linked", status="now", order=1, link_url="https://example.test/x")
    html = client.get(reverse("roadmap:index")).content.decode()
    assert html.index("Linked") < html.index(">Next<") < html.index("Soon") < html.index(">Later<")
    assert html.index("Later A") < html.index("Later B")
    assert 'href="https://example.test/x"' in html


@pytest.mark.django_db
def test_empty_column_says_so(client):
    RoadmapItem.objects.all().delete()
    assert "Nothing listed yet." in client.get(reverse("roadmap:index")).content.decode()


@pytest.mark.django_db
def test_footer_status_link_only_when_configured(client):
    url = reverse("legal:terms")
    assert ">Status<" not in client.get(url).content.decode()
    with override_settings(STATUS_PAGE_URL="https://status.example.test"):
        html = client.get(url).content.decode()
    assert 'href="https://status.example.test"' in html and ">Status<" in html
