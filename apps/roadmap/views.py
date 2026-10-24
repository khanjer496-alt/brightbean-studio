"""Public roadmap. No login, no voting, no comments."""

from django.conf import settings
from django.shortcuts import render
from django.views.decorators.http import require_GET

from .models import RoadmapItem


@require_GET
def index(request):
    items = list(RoadmapItem.objects.all())
    columns = [
        {"key": status.value, "label": status.label, "items": [i for i in items if i.status == status.value]}
        for status in RoadmapItem.Status
    ]
    return render(
        request,
        "roadmap/index.html",
        {
            "legal_page": "roadmap",
            "legal_name": settings.BRAND_LEGAL_NAME or settings.LEGAL_ENTITY_NAME,
            "columns": columns,
        },
    )
