from allauth.core.exceptions import ReauthenticationRequired
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods, require_POST

from . import services
from .forms import ImpersonationForm
from .models import ImpersonationSession


def staff_required(view):
    """Staff pages: a signed-in, active staff member. Anyone else gets a 403."""

    @login_required
    def wrapped(request, *args, **kwargs):
        if not (request.user.is_staff and request.user.is_active):
            raise PermissionDenied
        return view(request, *args, **kwargs)

    wrapped.__name__ = view.__name__
    wrapped.__doc__ = view.__doc__
    return wrapped


@staff_required
@require_http_methods(["GET", "POST"])
def impersonate(request, user_id):
    """Confirm, then start, a "view as" session. Superusers only, and only after a fresh sign-in."""
    if not request.user.is_superuser:
        raise PermissionDenied
    target = get_object_or_404(get_user_model(), pk=user_id)
    try:
        services.check_target(request.user, target)
    except services.ImpersonationRefusedError as exc:
        raise PermissionDenied(str(exc)) from exc
    if not services.has_recent_authentication(request):
        if not services.can_reauthenticate(request.user):
            raise PermissionDenied("Set a password or two-factor authentication on your account first.")
        raise ReauthenticationRequired
    form = ImpersonationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        services.start_impersonation(request, target, form.cleaned_data["reason"])
        return redirect("dashboard")
    return render(request, "support/impersonate.html", {"target": target, "form": form})


@login_required
@require_POST
def impersonation_stop(request):
    services.end_impersonation(request, ImpersonationSession.EndReason.STOPPED)
    return redirect("dashboard")


@staff_required
def platform_errors(request):
    window = request.GET.get("window")
    if window not in services.ERROR_WINDOWS:
        window = "24h"
    return render(
        request,
        "support/platform_errors.html",
        {"platforms": services.platform_error_summary(window), "window": window, "windows": services.ERROR_WINDOWS},
    )


@login_required
@require_POST
def dismiss_announcement(request, announcement_id):
    services.dismiss_announcement(request.user, announcement_id)
    return HttpResponse(status=204)
