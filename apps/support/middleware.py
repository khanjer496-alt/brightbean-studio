"""Make a staff "view as" session act as the target user, read-only.

Runs right after authentication. While a session is active ``request.user`` is
the target, so every view, permission check and template sees exactly what the
customer sees, and ``request.impersonator`` is the real staff member. Because
this is the one place the swap happens, it is also the one place that decides
what the staff member may not do.
"""

import re

from django.contrib.auth import get_user_model
from django.http import HttpResponseForbidden
from django.urls import Resolver404, resolve
from django.utils import timezone

from .models import ImpersonationSession
from .services import SESSION_KEY, end_impersonation

SAFE_METHODS = ("GET", "HEAD", "OPTIONS")

# The only writes allowed while viewing as someone: leaving the session.
ALLOWED_WRITE_URL_NAMES = frozenset({"support:impersonation_stop", "accounts:logout", "account_logout"})

# GET requests that still act outside the app or reveal secrets: starting or
# finishing a platform connection, opening checkout or the billing portal,
# authorising a connected app, and the 2FA, password and email screens.
BLOCKED_READ_PATHS = (
    re.compile(r"^/social-accounts/[^/]+/connect/"),
    re.compile(r"^/social-accounts/callback/"),
    re.compile(r"^/social-accounts/select-account/"),
    re.compile(r"^/organizations/billing/(checkout|portal)/"),
    re.compile(r"^/oauth/"),
    re.compile(r"^/accounts/(2fa|password|email|reauthenticate)/"),
    re.compile(r"^/accounts/settings/delete/"),
    re.compile(r"^/members/invite/"),
)

BLOCKED_MESSAGE = "You're viewing this account as support, so changes are turned off. Stop viewing to make changes."


class ImpersonationMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.impersonator = None
        if request.path.startswith(("/static/", "/media/")):
            return self.get_response(request)
        state = request.session.get(SESSION_KEY)
        if state and request.user.is_authenticated:
            target = self._resolve_target(request, state)
            if target is not None:
                request.impersonator = request.user
                request.user = target
                if not self._allowed(request):
                    return HttpResponseForbidden(BLOCKED_MESSAGE)
        return self.get_response(request)

    @staticmethod
    def _resolve_target(request, state):
        """The user to act as, or None after ending a session that is stale or no longer allowed."""
        record = ImpersonationSession.objects.filter(id=state["record_id"], ended_at__isnull=True).first()
        staff = request.user
        if record is None or str(record.staff_user_id) != str(staff.pk):
            request.session.pop(SESSION_KEY, None)
            return None
        if record.expires_at <= timezone.now():
            end_impersonation(request, ImpersonationSession.EndReason.EXPIRED)
            return None
        target = get_user_model().objects.filter(pk=state["target_id"], is_active=True).first()
        if not (staff.is_superuser and staff.is_active) or target is None or target.is_staff or target.is_superuser:
            end_impersonation(request, ImpersonationSession.EndReason.INVALID)
            return None
        return target

    @staticmethod
    def _allowed(request) -> bool:
        if request.method in SAFE_METHODS:
            return not any(pattern.match(request.path) for pattern in BLOCKED_READ_PATHS)
        try:
            match = resolve(request.path_info)
        except Resolver404:
            return False
        name = f"{match.namespace}:{match.url_name}" if match.namespace else match.url_name
        return name in ALLOWED_WRITE_URL_NAMES
