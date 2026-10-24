"""Public legal pages. No login: they must be readable before signing up and by platform reviewers."""

from django.conf import settings
from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST
from django_ratelimit.decorators import ratelimit

from . import meta_deletion
from .models import DataDeletionRequest
from .subprocessors import subprocessors

# Front-end assets served from this repository's static files or a CDN, with their licences.
THIRD_PARTY_ASSETS = [
    {
        "name": "htmx",
        "version": "2.0.4",
        "license": "0BSD",
        "url": "https://htmx.org",
        "delivery": "Bundled (static/js/htmx.min.js)",
    },
    {
        "name": "Alpine.js",
        "version": "3.14.8",
        "license": "MIT",
        "url": "https://alpinejs.dev",
        "delivery": "Bundled (static/js/alpine.min.js)",
    },
    {
        "name": "Cropper.js",
        "version": "1.6.2",
        "license": "MIT",
        "url": "https://fengyuanchen.github.io/cropperjs",
        "delivery": "Bundled (static/js/vendor)",
    },
    {
        "name": "Chart.js",
        "version": "4.4.6",
        "license": "MIT",
        "url": "https://www.chartjs.org",
        "delivery": "Bundled (static/js/vendor), including @kurkle/color (MIT)",
    },
    {
        "name": "flatpickr",
        "version": "4.6.13",
        "license": "MIT",
        "url": "https://flatpickr.js.org",
        "delivery": "Bundled (static/js/vendor)",
    },
    {
        "name": "SortableJS",
        "version": "1.15.6",
        "license": "MIT",
        "url": "https://sortablejs.github.io/Sortable/",
        "delivery": "Bundled (static/js/vendor)",
    },
    {
        "name": "Tailwind CSS",
        "version": "4.x",
        "license": "MIT",
        "url": "https://tailwindcss.com",
        "delivery": "Compiled into the stylesheet at build time",
    },
    {
        "name": "Manrope (font)",
        "version": "",
        "license": "SIL Open Font License 1.1",
        "url": "https://github.com/sharanda/manrope",
        "delivery": "Bundled (theme/static/fonts, licence text included)",
    },
    {
        "name": "Noto Sans, Noto Sans Arabic and Noto Emoji (fonts)",
        "version": "",
        "license": "SIL Open Font License 1.1",
        "url": "https://notofonts.github.io",
        "delivery": "Embedded in report PDFs (apps/reports/fonts, licence texts included)",
    },
    {
        "name": "Emojibase (emojibase-data)",
        "version": "18.0.0",
        "license": "MIT",
        "url": "https://emojibase.dev",
        "delivery": "Bundled (static/js/emoji-data.json); built from Unicode emoji data (Unicode License)",
    },
    {
        "name": "Swagger UI",
        "version": "5.x",
        "license": "Apache-2.0",
        "url": "https://swagger.io/tools/swagger-ui/",
        "delivery": "Loaded from cdn.jsdelivr.net on the API documentation page only",
    },
    {
        "name": "YouTube icon",
        "version": "",
        "license": "YouTube brand asset, used under the YouTube Brand Guidelines",
        "url": "https://brand.youtube/",
        "delivery": "Bundled (theme/static/brand/youtube)",
    },
]


def _context(page, **extra):
    return {
        "legal_page": page,
        "legal_name": settings.BRAND_LEGAL_NAME or settings.LEGAL_ENTITY_NAME,
        "legal_jurisdiction": settings.LEGAL_ENTITY_JURISDICTION,
        "legal_address": settings.LEGAL_ADDRESS or "[Registered business address to be completed]",
        "legal_effective_date": settings.LEGAL_EFFECTIVE_DATE or "[Effective date to be completed]",
        "legal_governing_law": settings.LEGAL_GOVERNING_LAW,
        "legal_email_provider": settings.LEGAL_EMAIL_PROVIDER,
        "sentry_enabled": bool(settings.SENTRY_DSN),
        "youtube_retention_enforced": settings.YOUTUBE_COMPLIANCE_ENFORCEMENT,
        **extra,
    }


@require_GET
def terms(request):
    return render(request, "legal/terms.html", _context("terms"))


@require_GET
def privacy(request):
    return render(request, "legal/privacy.html", _context("privacy", subprocessors=subprocessors()))


@require_GET
def data_deletion(request):
    return render(
        request,
        "legal/data_deletion.html",
        _context("data_deletion", meta_callback_path=reverse("legal:meta_data_deletion")),
    )


@require_GET
def data_deletion_status(request, code):
    deletion_request = DataDeletionRequest.objects.filter(code=code).first()
    if deletion_request is None:
        raise Http404("Unknown confirmation code.")
    return render(
        request,
        "legal/data_deletion_status.html",
        _context("data_deletion", deletion_request=deletion_request),
    )


@require_GET
def licenses(request):
    return render(request, "legal/licenses.html", _context("licenses", assets=THIRD_PARTY_ASSETS))


@require_GET
def subprocessors_list(request):
    return render(request, "legal/subprocessors.html", _context("subprocessors", subprocessors=subprocessors()))


@csrf_exempt
@ratelimit(key="ip", rate="60/m", block=True)
@require_POST
def meta_data_deletion_callback(request):
    """Meta's data deletion request callback; authenticated by the signed request, not a session."""
    try:
        payload = meta_deletion.parse_signed_request(request.POST.get("signed_request", ""))
    except meta_deletion.InvalidSignedRequestError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    deletion_request = meta_deletion.delete_for_meta_user(payload)
    status_url = request.build_absolute_uri(
        reverse("legal:data_deletion_status", kwargs={"code": deletion_request.code})
    )
    return JsonResponse({"url": status_url, "confirmation_code": deletion_request.code})
