"""Plan enforcement for the web UI.

Two jobs, both no-ops unless ``settings.BILLING_ENABLED``:

* Whole sections that belong to a plan feature (the inbox, analytics, the
  client portal admin) are gated here by URL namespace, so no view in those
  apps needs to know plans exist.
* A ``PlanLimitError`` that escapes a view becomes the upgrade message instead
  of a 500: JSON for the composer's and HTMX's fetches, a flash message and a
  redirect for a plain form post.
"""

from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme

from .entitlements import PlanLimitError, billing_enabled, require_feature
from .plans import Feature

# URL namespace → the plan feature every view in it requires.
FEATURE_URL_NAMESPACES = {
    "inbox": Feature.INBOX,
    "analytics": Feature.ANALYTICS,
    "client_portal_admin": Feature.CLIENT_PORTAL,
}


class BillingMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        if not billing_enabled():
            return None
        match = getattr(request, "resolver_match", None)
        feature = FEATURE_URL_NAMESPACES.get(match.namespace) if match else None
        org = getattr(request, "org", None)
        if feature is None or org is None:
            return None
        try:
            require_feature(org, feature)
        except PlanLimitError as exc:
            return self._locked_response(request, exc)
        return None

    def process_exception(self, request, exception):
        if not isinstance(exception, PlanLimitError):
            return None
        if self._wants_json(request):
            return JsonResponse(
                {"errors": {"plan": [exception.message]}, "upgrade_url": exception.upgrade_url},
                status=402,
            )
        messages.error(request, exception.message)
        referer = request.headers.get("referer", "")
        if referer and url_has_allowed_host_and_scheme(
            referer, allowed_hosts={request.get_host()}, require_https=request.is_secure()
        ):
            return redirect(referer)
        return redirect("dashboard")

    @staticmethod
    def _wants_json(request) -> bool:
        return bool(getattr(request, "htmx", False)) or "application/json" in request.headers.get("accept", "")

    def _locked_response(self, request, exc):
        if getattr(request, "htmx", False):
            return JsonResponse({"errors": {"plan": [exc.message]}, "upgrade_url": exc.upgrade_url}, status=402)
        return render(
            request,
            "billing/feature_locked.html",
            {"message": exc.message, "upgrade_url": exc.upgrade_url},
            status=402,
        )
