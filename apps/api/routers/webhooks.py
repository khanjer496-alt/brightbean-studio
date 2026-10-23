"""``/api/v1/webhooks/*`` — manage outbound webhook endpoints and read their delivery log.

Thin adapters over :mod:`apps.webhooks.services`, the same functions the
workspace settings page and the MCP webhook tools use. Every route needs
``manage_workspace_settings`` and only ever sees the key's own workspace;
an endpoint id from another workspace is a 404.
"""

from __future__ import annotations

import uuid

from django.http import HttpRequest
from django.shortcuts import get_object_or_404
from ninja import Query, Router
from ninja.errors import HttpError

from apps.api.limits import enforce_http_rate_limits
from apps.api.middleware import log_audit_entry
from apps.api.pagination import decode_offset_cursor, encode_offset_cursor
from apps.api.schemas import (
    CreateWebhookEndpointRequest,
    UpdateWebhookEndpointRequest,
    WebhookDeliveriesListResponse,
    WebhookDeliveryResponse,
    WebhookEndpointResponse,
    WebhookEndpointsListResponse,
    WebhookEndpointWithSecretResponse,
)
from apps.webhooks import services
from apps.webhooks.models import DeliveryStatus, WebhookDelivery, WebhookEndpoint
from apps.webhooks.services import WebhookError

router = Router(tags=["webhooks"])

_LIMIT_DEFAULT = 50
_LIMIT_MAX = 100


def _require_perm(request: HttpRequest, key: str) -> None:
    membership = getattr(request, "workspace_membership", None)
    if membership is None or not membership.effective_permissions.get(key, False):
        raise HttpError(403, f"Permission denied: {key}")


def _endpoint(request: HttpRequest, endpoint_id: uuid.UUID) -> WebhookEndpoint:
    return get_object_or_404(WebhookEndpoint, id=endpoint_id, workspace_id=request.api_key.workspace_id)  # type: ignore[attr-defined]


@router.get("/", response=WebhookEndpointsListResponse, summary="List webhook endpoints")
def list_endpoints(request):
    enforce_http_rate_limits(request, is_write=False)
    _require_perm(request, "manage_workspace_settings")
    endpoints = WebhookEndpoint.objects.filter(workspace_id=request.api_key.workspace_id)  # type: ignore[attr-defined]
    log_audit_entry(request, action="webhooks.list", target_id=None, status_code=200)
    return WebhookEndpointsListResponse(endpoints=[WebhookEndpointResponse.from_endpoint(e) for e in endpoints])


@router.post(
    "/",
    response={201: WebhookEndpointWithSecretResponse},
    summary="Create a webhook endpoint",
    description="The response includes the signing `secret`. It is shown only here and when you rotate it.",
)
def create_endpoint(request, payload: CreateWebhookEndpointRequest):
    enforce_http_rate_limits(request, is_write=True)
    _require_perm(request, "manage_workspace_settings")
    try:
        endpoint, secret = services.create_endpoint(
            request.workspace,  # type: ignore[attr-defined]
            url=payload.url,
            event_types=payload.event_types,
            description=payload.description,
            is_enabled=payload.is_enabled,
            created_by=request.user if not request.user.is_anonymous else None,
        )
    except WebhookError as exc:
        raise HttpError(422, str(exc)) from exc
    log_audit_entry(request, action="webhooks.create", target_id=endpoint.id, status_code=201)
    return 201, WebhookEndpointWithSecretResponse.from_endpoint_and_secret(endpoint, secret)


@router.get("/{endpoint_id}", response=WebhookEndpointResponse, summary="Get a webhook endpoint")
def get_endpoint(request, endpoint_id: uuid.UUID):
    enforce_http_rate_limits(request, is_write=False)
    _require_perm(request, "manage_workspace_settings")
    endpoint = _endpoint(request, endpoint_id)
    log_audit_entry(request, action="webhooks.get", target_id=endpoint.id, status_code=200)
    return WebhookEndpointResponse.from_endpoint(endpoint)


@router.patch("/{endpoint_id}", response=WebhookEndpointResponse, summary="Update a webhook endpoint")
def update_endpoint(request, endpoint_id: uuid.UUID, payload: UpdateWebhookEndpointRequest):
    enforce_http_rate_limits(request, is_write=True)
    _require_perm(request, "manage_workspace_settings")
    endpoint = _endpoint(request, endpoint_id)
    try:
        services.update_endpoint(endpoint, **payload.dict(exclude_unset=True))
    except WebhookError as exc:
        raise HttpError(422, str(exc)) from exc
    log_audit_entry(request, action="webhooks.update", target_id=endpoint.id, status_code=200)
    return WebhookEndpointResponse.from_endpoint(endpoint)


@router.delete("/{endpoint_id}", response={204: None}, summary="Delete a webhook endpoint")
def delete_endpoint(request, endpoint_id: uuid.UUID):
    enforce_http_rate_limits(request, is_write=True)
    _require_perm(request, "manage_workspace_settings")
    endpoint = _endpoint(request, endpoint_id)
    endpoint.delete()
    log_audit_entry(request, action="webhooks.delete", target_id=endpoint_id, status_code=204)
    return 204, None


@router.post(
    "/{endpoint_id}/rotate-secret",
    response=WebhookEndpointWithSecretResponse,
    summary="Replace the signing secret",
    description="The old secret stops working at once. The new one is returned only in this response.",
)
def rotate_secret(request, endpoint_id: uuid.UUID):
    enforce_http_rate_limits(request, is_write=True)
    _require_perm(request, "manage_workspace_settings")
    endpoint = _endpoint(request, endpoint_id)
    secret = services.rotate_secret(endpoint)
    log_audit_entry(request, action="webhooks.rotate_secret", target_id=endpoint.id, status_code=200)
    return WebhookEndpointWithSecretResponse.from_endpoint_and_secret(endpoint, secret)


@router.post(
    "/{endpoint_id}/test",
    response={202: WebhookDeliveryResponse},
    summary="Send a test event",
    description="Queues a `webhook.test` ping, even to an endpoint that is turned off. Read the outcome from the delivery log.",
)
def send_test_event(request, endpoint_id: uuid.UUID):
    enforce_http_rate_limits(request, is_write=True)
    _require_perm(request, "manage_workspace_settings")
    endpoint = _endpoint(request, endpoint_id)
    delivery = services.send_test_event(endpoint, requested_by=None)
    log_audit_entry(request, action="webhooks.test", target_id=endpoint.id, status_code=202)
    return 202, WebhookDeliveryResponse.from_delivery(delivery)


@router.get(
    "/{endpoint_id}/deliveries", response=WebhookDeliveriesListResponse, summary="List an endpoint's deliveries"
)
def list_deliveries(
    request,
    endpoint_id: uuid.UUID,
    status: str | None = Query(None),
    limit: int = Query(_LIMIT_DEFAULT, ge=1, le=_LIMIT_MAX),
    cursor: str | None = Query(None),
):
    enforce_http_rate_limits(request, is_write=False)
    _require_perm(request, "manage_workspace_settings")
    endpoint = _endpoint(request, endpoint_id)
    if status is not None and status not in DeliveryStatus.values:
        raise HttpError(422, f"status must be one of {', '.join(DeliveryStatus.values)}")
    try:
        offset = decode_offset_cursor(cursor)
    except ValueError as exc:
        raise HttpError(422, "cursor is not a valid pagination cursor") from exc

    qs = endpoint.deliveries.all()
    if status:
        qs = qs.filter(status=status)
    rows = list(qs[offset : offset + limit + 1])
    has_more = len(rows) > limit
    rows = rows[:limit]
    log_audit_entry(request, action="webhooks.deliveries.list", target_id=endpoint.id, status_code=200)
    return WebhookDeliveriesListResponse(
        deliveries=[WebhookDeliveryResponse.from_delivery(d) for d in rows],
        limit=limit,
        next_cursor=encode_offset_cursor(offset + limit) if has_more else None,
    )


@router.post(
    "/{endpoint_id}/deliveries/{delivery_id}/resend",
    response={202: WebhookDeliveryResponse},
    summary="Resend a delivery",
    description="Sends the same payload again under the same delivery id, with a fresh retry schedule.",
)
def resend_delivery(request, endpoint_id: uuid.UUID, delivery_id: uuid.UUID):
    enforce_http_rate_limits(request, is_write=True)
    _require_perm(request, "manage_workspace_settings")
    endpoint = _endpoint(request, endpoint_id)
    delivery = get_object_or_404(WebhookDelivery, id=delivery_id, endpoint=endpoint)
    services.resend(delivery)
    log_audit_entry(request, action="webhooks.resend", target_id=delivery.id, status_code=202)
    return 202, WebhookDeliveryResponse.from_delivery(delivery)
