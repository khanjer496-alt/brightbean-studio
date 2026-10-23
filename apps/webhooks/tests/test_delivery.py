import datetime as dt
import json
from unittest.mock import patch

import httpx
import pytest
from django.utils import timezone

from apps.notifications.models import EventType as NotificationEventType
from apps.notifications.models import Notification
from apps.webhooks import services
from apps.webhooks.models import DeliveryStatus, EventType, WebhookDelivery, WebhookEndpoint
from apps.webhooks.tasks import LOG_RETENTION_DAYS, prune_webhook_deliveries, retry_due_webhook_deliveries

pytestmark = pytest.mark.django_db


def _queue(endpoint, event_type=EventType.POST_PUBLISHED, **extra):
    delivery = services.emit(event_type, endpoint.workspace, {"post_id": "p1", **extra})
    assert len(delivery) == 1
    return delivery[0]


def _attempt(delivery, response=(200, "ok")):
    with patch.object(services, "_send_request", return_value=response) as send:
        sent = services.attempt_delivery(delivery.id)
    delivery.refresh_from_db()
    return sent, send


def test_emit_builds_the_documented_envelope(endpoint, workspace):
    delivery = _queue(endpoint)
    assert delivery.payload["type"] == "post.published"
    assert delivery.payload["id"] == str(delivery.event_id)
    assert delivery.payload["workspace"] == {"id": str(workspace.id), "name": "Acme"}
    assert delivery.payload["data"] == {"post_id": "p1"}
    assert delivery.payload["created_at"]


def test_successful_delivery_sends_signed_request_and_records_it(endpoint):
    delivery = _queue(endpoint)
    sent, send = _attempt(delivery, (200, "thanks"))

    assert sent
    url, body, headers = send.call_args.args
    assert url == endpoint.url
    assert json.loads(body) == delivery.payload
    assert headers["PostDelegate-Event"] == "post.published"
    assert headers["PostDelegate-Delivery"] == str(delivery.id)
    assert services.verify_signature(endpoint.plain_secret, body, headers["PostDelegate-Signature"])
    assert delivery.status == DeliveryStatus.DELIVERED
    assert (delivery.attempts, delivery.last_status_code, delivery.response_body) == (1, 200, "thanks")
    assert delivery.last_duration_ms is not None and delivery.delivered_at and delivery.next_attempt_at is None


def test_backoff_schedule_then_final_failure(endpoint):
    delivery = _queue(endpoint)
    expected_waits = [60, 300, 1800, 7200, 43200]
    for attempt, wait in enumerate(expected_waits, start=1):
        delivery.next_attempt_at = timezone.now()
        delivery.save(update_fields=["next_attempt_at"])
        before = timezone.now()
        _attempt(delivery, (500, "nope"))
        assert delivery.status == DeliveryStatus.PENDING
        assert delivery.attempts == attempt
        gap = (delivery.next_attempt_at - before).total_seconds()
        assert wait - 5 <= gap <= wait + 5

    delivery.next_attempt_at = timezone.now()
    delivery.save(update_fields=["next_attempt_at"])
    _attempt(delivery, (503, "still no"))
    assert delivery.status == DeliveryStatus.FAILED
    assert delivery.attempts == services.MAX_ATTEMPTS == 6
    assert delivery.next_attempt_at is None
    assert delivery.last_error == "HTTP 503"


def test_a_delivery_that_is_not_due_is_not_sent(endpoint):
    delivery = _queue(endpoint)
    delivery.next_attempt_at = timezone.now() + dt.timedelta(minutes=5)
    delivery.save(update_fields=["next_attempt_at"])
    sent, send = _attempt(delivery)
    assert not sent
    send.assert_not_called()


def test_claiming_stops_a_second_worker_sending_the_same_delivery(endpoint):
    delivery = _queue(endpoint)
    with patch.object(services, "_send_request", return_value=(500, "")) as send:
        services.attempt_delivery(delivery.id)
        services.attempt_delivery(delivery.id)
    assert send.call_count == 1


def test_redirects_and_network_errors_are_failures(endpoint):
    delivery = _queue(endpoint)
    _attempt(delivery, (302, ""))
    assert "Redirects are not followed" in delivery.last_error

    delivery.next_attempt_at = timezone.now()
    delivery.save(update_fields=["next_attempt_at"])
    with patch.object(services, "_send_request", side_effect=httpx.ConnectTimeout("slow")):
        services.attempt_delivery(delivery.id)
    delivery.refresh_from_db()
    assert delivery.last_status_code is None and "10 seconds" in delivery.last_error
    assert delivery.attempts == 2


def test_send_request_does_not_follow_redirects_and_truncates_the_response(endpoint):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.path == "/postdelegate":
            return httpx.Response(302, headers={"Location": "http://169.254.169.254/"})
        return httpx.Response(200, text="x" * 50_000)

    status, text = services._send_request(endpoint.url, b"{}", {}, transport=httpx.MockTransport(handler))
    assert (status, text) == (302, "")
    assert seen == [endpoint.url]

    _, text = services._send_request("https://hooks.example.com/big", b"{}", {}, transport=httpx.MockTransport(handler))
    assert len(text) == services.RESPONSE_BODY_LIMIT


def test_url_is_checked_again_at_send_time(endpoint):
    delivery = _queue(endpoint)
    with (
        patch.object(services, "resolve_public_ip", return_value=None),
        patch.object(services, "_send_request") as send,
    ):
        services.attempt_delivery(delivery.id)
    send.assert_not_called()
    delivery.refresh_from_db()
    assert delivery.status == DeliveryStatus.FAILED
    assert "public" in delivery.last_error
    endpoint.refresh_from_db()
    assert endpoint.consecutive_failures == 0


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/hook",
        "https://10.0.0.5/hook",
        "https://192.168.1.10/hook",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/hook",
        "ftp://example.com/hook",
        "file:///etc/passwd",
        "https://user:pass@example.com/hook",
        "not a url",
    ],
)
def test_private_and_odd_urls_are_rejected(workspace, url):
    with pytest.raises(services.WebhookError):
        services.create_endpoint(workspace, url=url, event_types=["post.published"])


def test_localhost_hostname_is_rejected(workspace):
    with pytest.raises(services.WebhookError):
        services.create_endpoint(workspace, url="http://localhost:8000/hook", event_types=["post.published"])


def test_event_types_are_validated(workspace):
    for bad in ([], ["nope"], ["webhook.test"], "post.published"):
        with pytest.raises(services.WebhookError):
            services.create_endpoint(workspace, url="https://hooks.example.com/x", event_types=bad)


def test_endpoint_cap_per_workspace(workspace):
    for _ in range(services.MAX_ENDPOINTS_PER_WORKSPACE):
        services.create_endpoint(workspace, url="https://hooks.example.com/x", event_types=["post.failed"])
    with pytest.raises(services.WebhookError):
        services.create_endpoint(workspace, url="https://hooks.example.com/x", event_types=["post.failed"])


def test_secret_is_encrypted_at_rest_and_hint_stored(endpoint):
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute("SELECT secret FROM webhooks_endpoint WHERE id = %s", [endpoint.id])
        raw = cursor.fetchone()[0]
    assert endpoint.plain_secret not in raw
    endpoint.refresh_from_db()
    assert endpoint.secret == endpoint.plain_secret
    assert endpoint.secret_hint == endpoint.plain_secret[-4:]


def test_rotating_the_secret_changes_what_signs(endpoint):
    delivery = _queue(endpoint)
    new_secret = services.rotate_secret(endpoint)
    _, send = _attempt(delivery)
    _, body, headers = send.call_args.args
    assert services.verify_signature(new_secret, body, headers["PostDelegate-Signature"])
    assert not services.verify_signature(endpoint.plain_secret, body, headers["PostDelegate-Signature"])


def test_emitting_the_same_event_twice_makes_one_delivery(endpoint, workspace):
    first = services.emit("post.failed", workspace, {"x": 1}, event_id=__import__("uuid").uuid4())
    event_id = first[0].event_id
    assert services.emit("post.failed", workspace, {"x": 1}, event_id=event_id) == []
    assert WebhookDelivery.objects.filter(event_id=event_id).count() == 1


def test_only_enabled_subscribed_endpoints_in_the_workspace_get_events(endpoint, workspace, org):
    from apps.workspaces.models import Workspace

    other = Workspace.objects.create(organization=org, name="Other")
    services.create_endpoint(other, url="https://hooks.example.com/o", event_types=["post.published"])
    narrow, _ = services.create_endpoint(workspace, url="https://hooks.example.com/n", event_types=["post.failed"])
    off, _ = services.create_endpoint(
        workspace, url="https://hooks.example.com/off", event_types=["post.published"], is_enabled=False
    )

    deliveries = services.emit("post.published", workspace, {})
    assert [d.endpoint_id for d in deliveries] == [endpoint.id]
    assert not WebhookDelivery.objects.filter(endpoint__in=[narrow, off]).exists()


def test_emit_never_raises(workspace):
    with patch.object(services.WebhookEndpoint.objects, "filter", side_effect=RuntimeError("db down")):
        assert services.emit("post.failed", workspace, {}) == []


def test_first_attempt_is_queued_when_the_transaction_commits(endpoint, workspace, django_capture_on_commit_callbacks):
    with patch("apps.webhooks.tasks.deliver_webhook") as task:
        with django_capture_on_commit_callbacks(execute=True) as callbacks:
            delivery = _queue(endpoint)
        assert len(callbacks) == 1
        task.assert_called_once_with(str(delivery.id))


def test_endpoint_is_disabled_after_repeated_failures_and_owners_are_told(endpoint, owner, manager):
    endpoint.consecutive_failures = services.AUTO_DISABLE_THRESHOLD - 1
    endpoint.save(update_fields=["consecutive_failures"])
    delivery = _queue(endpoint)
    _attempt(delivery, (500, ""))

    endpoint.refresh_from_db()
    assert not endpoint.is_enabled
    assert endpoint.disabled_at and "15" in endpoint.disabled_reason
    note = Notification.objects.get(event_type=NotificationEventType.WEBHOOK_DISABLED)
    assert note.user == owner
    assert not Notification.objects.filter(user=manager).exists()
    assert services.emit("post.published", endpoint.workspace, {}) == []

    # Already off: further failures do not notify again.
    delivery.next_attempt_at = timezone.now()
    delivery.save(update_fields=["next_attempt_at"])
    _attempt(delivery, (500, ""))
    assert Notification.objects.filter(event_type=NotificationEventType.WEBHOOK_DISABLED).count() == 1


def test_success_resets_the_failure_streak(endpoint):
    endpoint.consecutive_failures = 7
    endpoint.save(update_fields=["consecutive_failures"])
    _attempt(_queue(endpoint))
    endpoint.refresh_from_db()
    assert endpoint.consecutive_failures == 0


def test_re_enabling_clears_the_auto_disable(endpoint):
    WebhookEndpoint.objects.filter(pk=endpoint.pk).update(
        is_enabled=False, consecutive_failures=15, disabled_at=timezone.now(), disabled_reason="x"
    )
    endpoint.refresh_from_db()
    services.update_endpoint(endpoint, is_enabled=True)
    endpoint.refresh_from_db()
    assert endpoint.is_enabled and endpoint.consecutive_failures == 0 and not endpoint.disabled_at


def test_in_flight_deliveries_of_a_disabled_endpoint_are_dropped_but_tests_still_go_out(endpoint):
    delivery = _queue(endpoint)
    services.update_endpoint(endpoint, is_enabled=False)
    sent, send = _attempt(delivery)
    assert not sent and delivery.status == DeliveryStatus.FAILED
    send.assert_not_called()

    test_delivery = services.send_test_event(endpoint)
    sent, send = _attempt(test_delivery)
    assert sent and test_delivery.status == DeliveryStatus.DELIVERED
    assert test_delivery.event_type == "webhook.test"


def test_resend_keeps_the_delivery_id_and_payload(endpoint):
    delivery = _queue(endpoint)
    payload = delivery.payload
    for _ in range(3):
        delivery.next_attempt_at = timezone.now()
        delivery.save(update_fields=["next_attempt_at"])
        _attempt(delivery, (500, ""))
    original_id = delivery.id

    services.resend(delivery)
    sent, send = _attempt(delivery, (200, "fine"))
    assert sent and delivery.id == original_id and delivery.payload == payload
    assert delivery.status == DeliveryStatus.DELIVERED and delivery.attempts == 1
    assert send.call_args.args[2]["PostDelegate-Delivery"] == str(original_id)


def test_retry_sweep_attempts_only_due_deliveries(endpoint):
    due = _queue(endpoint)
    later = _queue(endpoint, post_id2="b")
    WebhookDelivery.objects.filter(pk=later.pk).update(next_attempt_at=timezone.now() + dt.timedelta(hours=1))
    with patch.object(services, "_send_request", return_value=(200, "")) as send:
        retry_due_webhook_deliveries.now()
    assert send.call_count == 1
    due.refresh_from_db(), later.refresh_from_db()
    assert due.status == DeliveryStatus.DELIVERED and later.status == DeliveryStatus.PENDING


def test_prune_removes_old_finished_rows_but_keeps_pending_and_recent(endpoint):
    old = timezone.now() - dt.timedelta(days=LOG_RETENTION_DAYS + 1)
    rows = {}
    for name, status, created in [
        ("old_done", DeliveryStatus.DELIVERED, old),
        ("old_failed", DeliveryStatus.FAILED, old),
        ("old_pending", DeliveryStatus.PENDING, old),
        ("recent", DeliveryStatus.DELIVERED, timezone.now()),
    ]:
        row = _queue(endpoint, name=name)
        WebhookDelivery.objects.filter(pk=row.pk).update(status=status, created_at=created)
        rows[name] = row.pk
    prune_webhook_deliveries.now()
    assert set(WebhookDelivery.objects.values_list("pk", flat=True)) == {rows["old_pending"], rows["recent"]}


def test_the_request_goes_to_the_address_that_was_checked(endpoint):
    seen = {}

    def handler(request):
        seen.update(url=str(request.url), host=request.headers["host"], sni=request.extensions.get("sni_hostname"))
        return httpx.Response(204)

    status, _ = services._send_request(
        "https://hooks.example.com:8443/in?x=1",
        b"{}",
        {},
        pinned_ip="93.184.216.34",
        transport=httpx.MockTransport(handler),
    )
    assert status == 204
    assert seen == {
        "url": "https://93.184.216.34:8443/in?x=1",
        "host": "hooks.example.com:8443",
        "sni": "hooks.example.com",
    }

    services._send_request(
        "http://hooks.example.com/in", b"{}", {}, pinned_ip="2001:db8::1", transport=httpx.MockTransport(handler)
    )
    assert seen["url"] == "http://[2001:db8::1]/in" and seen["host"] == "hooks.example.com" and seen["sni"] is None


def test_attempt_delivery_pins_the_resolved_address(endpoint):
    delivery = _queue(endpoint)
    with patch.object(services, "_send_request", return_value=(200, "")) as send:
        services.attempt_delivery(delivery.id)
    assert send.call_args.kwargs["pinned_ip"] == "93.184.216.34"


def test_a_resend_waits_for_an_attempt_in_flight(endpoint):
    delivery = _queue(endpoint)
    assert services._claim(delivery.id) is not None  # an attempt is under way
    with pytest.raises(services.WebhookError):
        services.resend(delivery)

    # Waiting hours for its next retry: nothing is in flight, so it can go now.
    WebhookDelivery.objects.filter(pk=delivery.pk).update(next_attempt_at=timezone.now() + dt.timedelta(hours=2))
    services.resend(delivery)
    delivery.refresh_from_db()
    assert delivery.status == DeliveryStatus.PENDING and delivery.attempts == 0
    assert delivery.next_attempt_at <= timezone.now()


def test_an_attempt_that_outlived_its_lease_does_not_overwrite_the_outcome(endpoint):
    delivery = _queue(endpoint)
    stale = services._claim(delivery.id)
    WebhookDelivery.objects.filter(pk=delivery.pk).update(status=DeliveryStatus.DELIVERED, next_attempt_at=None)
    services._settle_failure(stale, status_code=500, duration_ms=5, error="HTTP 500")
    delivery.refresh_from_db()
    assert delivery.status == DeliveryStatus.DELIVERED and delivery.last_error == ""


def test_every_payload_names_its_api_version(endpoint):
    delivery = services.send_test_event(endpoint)
    assert delivery.payload["api_version"] == services.API_VERSION
