"""PWA endpoints: root-scope service worker, public offline page and the install-shortcut redirect."""

import hashlib
from pathlib import Path

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET

SW_SOURCE = Path(settings.BASE_DIR) / "static" / "sw.js"
VERSION_TOKEN = "__PD_SW_VERSION__"


def service_worker_source() -> str:
    """The worker script with the build id baked in, so each deploy gets its own cache names."""
    source = SW_SOURCE.read_text(encoding="utf-8")
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:8]
    build = "".join(ch for ch in settings.PWA_BUILD_ID if ch.isalnum() or ch in "-_.")[:24] or "dev"
    return source.replace(VERSION_TOKEN, f"{build}-{digest}")


@require_GET
def service_worker(request):
    """Served at /sw.js so its scope is the whole site. Never cached, so a new deploy is picked up on the next check."""
    response = HttpResponse(service_worker_source(), content_type="application/javascript; charset=utf-8")
    response["Service-Worker-Allowed"] = "/"
    response["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@require_GET
def offline(request):
    """Public, branded fallback shown when a navigation fails. Holds no account or workspace data."""
    return render(request, "offline.html")


@require_GET
@login_required
def new_post_shortcut(request):
    """Target of the installed app's "New post" shortcut: open the composer in the user's last workspace."""
    from apps.members.models import WorkspaceMembership

    memberships = WorkspaceMembership.objects.filter(user=request.user, workspace__is_archived=False)
    chosen = None
    if request.user.last_workspace_id:
        chosen = memberships.filter(workspace_id=request.user.last_workspace_id).first()
    chosen = chosen or memberships.order_by("workspace__name").first()
    if chosen is None:
        return redirect("dashboard")
    return redirect("composer:compose", workspace_id=chosen.workspace_id)
