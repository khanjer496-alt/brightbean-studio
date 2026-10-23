"""Workspace webhook settings: endpoints, their secrets and the delivery log.

Every view needs ``manage_workspace_settings`` (workspace owners by default)
and looks endpoints up through ``request.workspace``, so an id from another
workspace is a 404 rather than a 403.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods, require_POST

from apps.members.decorators import require_permission

from . import services
from .models import SUBSCRIBABLE_EVENT_TYPES, EventType, WebhookDelivery, WebhookEndpoint
from .services import WebhookError

DELIVERY_LOG_LIMIT = 100
SECRET_SESSION_KEY = "webhook_secret_reveal"


def _guard(view_func):
    return login_required(require_permission("manage_workspace_settings")(view_func))


def _endpoint(request, endpoint_id) -> WebhookEndpoint:
    return get_object_or_404(WebhookEndpoint, id=endpoint_id, workspace=request.workspace)


def _form_values(request) -> dict:
    return {
        "url": request.POST.get("url", ""),
        "description": request.POST.get("description", ""),
        "event_types": request.POST.getlist("event_types"),
        "is_enabled": request.POST.get("is_enabled") == "on",
    }


def _event_choices(selected) -> list[tuple[str, str, bool]]:
    labels = dict(EventType.choices)
    return [(value, labels[value], value in selected) for value in SUBSCRIBABLE_EVENT_TYPES]


def _reveal_secret(request, endpoint, secret) -> None:
    # Post/Redirect/Get: the detail page pops it, so a reload never shows it twice.
    request.session[SECRET_SESSION_KEY] = {"endpoint_id": str(endpoint.id), "secret": secret}


@_guard
def endpoint_list(request, workspace_id):
    endpoints = WebhookEndpoint.objects.filter(workspace=request.workspace)
    return render(
        request,
        "webhooks/list.html",
        {"workspace": request.workspace, "settings_active": "webhooks", "endpoints": endpoints},
    )


@_guard
@require_http_methods(["GET", "POST"])
def endpoint_create(request, workspace_id):
    values = {"url": "", "description": "", "event_types": [], "is_enabled": True}
    if request.method == "POST":
        values = _form_values(request)
        try:
            endpoint, secret = services.create_endpoint(
                request.workspace,
                url=values["url"],
                description=values["description"],
                event_types=values["event_types"],
                is_enabled=values["is_enabled"],
                created_by=request.user,
            )
        except WebhookError as exc:
            messages.error(request, str(exc))
        else:
            _reveal_secret(request, endpoint, secret)
            messages.success(request, "Webhook endpoint created.")
            return redirect("webhooks:detail", workspace_id=request.workspace.id, endpoint_id=endpoint.id)
    return render(
        request,
        "webhooks/form.html",
        {
            "workspace": request.workspace,
            "settings_active": "webhooks",
            "endpoint": None,
            "values": values,
            "event_choices": _event_choices(values["event_types"]),
        },
    )


@_guard
def endpoint_detail(request, workspace_id, endpoint_id):
    endpoint = _endpoint(request, endpoint_id)
    revealed = request.session.pop(SECRET_SESSION_KEY, None)
    secret = revealed["secret"] if revealed and revealed["endpoint_id"] == str(endpoint.id) else None
    deliveries = endpoint.deliveries.all()[:DELIVERY_LOG_LIMIT]
    return render(
        request,
        "webhooks/detail.html",
        {
            "workspace": request.workspace,
            "settings_active": "webhooks",
            "endpoint": endpoint,
            "values": {
                "url": endpoint.url,
                "description": endpoint.description,
                "is_enabled": endpoint.is_enabled,
            },
            "event_choices": _event_choices(endpoint.event_types),
            "new_secret": secret,
            "deliveries": deliveries,
            "delivery_log_limit": DELIVERY_LOG_LIMIT,
        },
    )


@_guard
@require_POST
def endpoint_update(request, workspace_id, endpoint_id):
    endpoint = _endpoint(request, endpoint_id)
    values = _form_values(request)
    try:
        services.update_endpoint(
            endpoint,
            url=values["url"],
            description=values["description"],
            event_types=values["event_types"],
            is_enabled=values["is_enabled"],
        )
    except WebhookError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Webhook endpoint saved.")
    return redirect("webhooks:detail", workspace_id=request.workspace.id, endpoint_id=endpoint.id)


@_guard
@require_POST
def endpoint_rotate_secret(request, workspace_id, endpoint_id):
    endpoint = _endpoint(request, endpoint_id)
    _reveal_secret(request, endpoint, services.rotate_secret(endpoint))
    messages.success(request, "Signing secret replaced. The old one no longer works.")
    return redirect("webhooks:detail", workspace_id=request.workspace.id, endpoint_id=endpoint.id)


@_guard
@require_POST
def endpoint_test(request, workspace_id, endpoint_id):
    endpoint = _endpoint(request, endpoint_id)
    services.send_test_event(endpoint, requested_by=request.user)
    messages.success(request, "Test event queued. It shows up in the delivery log in a moment.")
    return redirect("webhooks:detail", workspace_id=request.workspace.id, endpoint_id=endpoint.id)


@_guard
@require_POST
def endpoint_delete(request, workspace_id, endpoint_id):
    _endpoint(request, endpoint_id).delete()
    messages.success(request, "Webhook endpoint deleted.")
    return redirect("webhooks:list", workspace_id=request.workspace.id)


@_guard
@require_POST
def delivery_resend(request, workspace_id, endpoint_id, delivery_id):
    endpoint = _endpoint(request, endpoint_id)
    delivery = get_object_or_404(WebhookDelivery, id=delivery_id, endpoint=endpoint)
    services.resend(delivery)
    messages.success(request, "Delivery queued to be sent again.")
    return redirect("webhooks:detail", workspace_id=request.workspace.id, endpoint_id=endpoint.id)
