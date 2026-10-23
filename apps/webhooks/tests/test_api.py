"""REST ``/api/v1/webhooks`` and the MCP webhook tools."""

import json

import pytest
from django.test import Client

from apps.api_keys import services as key_services
from apps.members.models import PERMISSION_KEYS, OrgMembership
from apps.social_accounts.models import SocialAccount
from apps.webhooks import services
from apps.webhooks.models import DeliveryStatus, WebhookDelivery, WebhookEndpoint
from apps.workspaces.models import Workspace

from .conftest import PUBLIC_URL

pytestmark = pytest.mark.django_db

MCP_URL = "/api/v1/mcp/"


class SecureClient(Client):
    def generic(self, method, path, *args, **kwargs):
        kwargs["secure"] = True
        return super().generic(method, path, *args, **kwargs)


def _client_for(workspace, account, user, permissions):
    key = key_services.issue_api_key(
        workspace=workspace, social_accounts=[account], issued_by=user, name="hooks", permissions=permissions
    )
    return SecureClient(HTTP_AUTHORIZATION=f"Bearer {key.plaintext_token}")


@pytest.fixture
def api(workspace, account, owner):
    return _client_for(workspace, account, owner, list(PERMISSION_KEYS))


@pytest.fixture
def unprivileged_api(workspace, account, owner):
    return _client_for(workspace, account, owner, ["create_posts"])


def _json(response):
    return json.loads(response.content)


def _create(api, **overrides):
    body = {"url": PUBLIC_URL, "event_types": ["post.published"], **overrides}
    return api.post("/api/v1/webhooks/", data=json.dumps(body), content_type="application/json")


def test_create_returns_the_secret_once(api, workspace):
    response = _create(api, description="CMS")
    assert response.status_code == 201
    body = _json(response)
    assert body["secret"].startswith("whsec_") and body["description"] == "CMS"
    assert WebhookEndpoint.objects.get(pk=body["id"]).secret == body["secret"]

    for url in ("/api/v1/webhooks/", f"/api/v1/webhooks/{body['id']}"):
        assert body["secret"] not in json.dumps(_json(api.get(url)))


def test_create_validation_errors_are_422(api):
    assert _create(api, url="http://169.254.169.254/x").status_code == 422
    assert _create(api, event_types=["nope"]).status_code == 422
    assert _create(api, event_types=[]).status_code == 422


def test_get_update_delete_roundtrip(api, endpoint):
    url = f"/api/v1/webhooks/{endpoint.id}"
    assert _json(api.get(url))["url"] == PUBLIC_URL

    patched = api.patch(
        url, data=json.dumps({"event_types": ["post.failed"], "is_enabled": False}), content_type="application/json"
    )
    assert patched.status_code == 200
    assert _json(patched)["event_types"] == ["post.failed"] and _json(patched)["is_enabled"] is False
    bad = api.patch(url, data=json.dumps({"url": "http://127.0.0.1/"}), content_type="application/json")
    assert bad.status_code == 422

    assert api.delete(url).status_code == 204
    assert api.get(url).status_code == 404


def test_rotate_secret_returns_a_new_secret(api, endpoint):
    response = api.post(f"/api/v1/webhooks/{endpoint.id}/rotate-secret")
    assert response.status_code == 200
    new = _json(response)["secret"]
    endpoint.refresh_from_db()
    assert new == endpoint.secret != endpoint.plain_secret


def test_deliveries_list_paginates_filters_and_resends(api, endpoint):
    for _ in range(3):
        api.post(f"/api/v1/webhooks/{endpoint.id}/test")
    assert endpoint.deliveries.count() == 3

    page = _json(api.get(f"/api/v1/webhooks/{endpoint.id}/deliveries?limit=2"))
    assert len(page["deliveries"]) == 2 and page["next_cursor"]
    rest = _json(api.get(f"/api/v1/webhooks/{endpoint.id}/deliveries?limit=2&cursor={page['next_cursor']}"))
    assert len(rest["deliveries"]) == 1 and rest["next_cursor"] is None
    assert page["deliveries"][0]["payload"]["type"] == "webhook.test"
    assert _json(api.get(f"/api/v1/webhooks/{endpoint.id}/deliveries?status=failed"))["deliveries"] == []
    assert api.get(f"/api/v1/webhooks/{endpoint.id}/deliveries?status=bogus").status_code == 422

    delivery = endpoint.deliveries.first()
    in_flight = api.post(f"/api/v1/webhooks/{endpoint.id}/deliveries/{delivery.id}/resend")
    assert in_flight.status_code == 409  # still due now: the first attempt has not been made

    WebhookDelivery.objects.filter(pk=delivery.pk).update(status=DeliveryStatus.FAILED, next_attempt_at=None)
    resent = api.post(f"/api/v1/webhooks/{endpoint.id}/deliveries/{delivery.id}/resend")
    assert resent.status_code == 202 and _json(resent)["id"] == str(delivery.id)
    assert _json(resent)["status"] == "pending"


def test_a_key_without_manage_workspace_settings_is_refused(unprivileged_api, endpoint):
    base = "/api/v1/webhooks/"
    assert unprivileged_api.get(base).status_code == 403
    assert _create(unprivileged_api).status_code == 403
    assert unprivileged_api.get(f"{base}{endpoint.id}").status_code == 403
    assert unprivileged_api.delete(f"{base}{endpoint.id}").status_code == 403
    assert unprivileged_api.get(f"{base}{endpoint.id}/deliveries").status_code == 403
    assert WebhookEndpoint.objects.filter(pk=endpoint.pk).exists()


def test_a_key_scoped_to_some_accounts_is_refused(api, workspace, endpoint):
    # Events report on every account, so a key that cannot see one of them
    # must not be able to subscribe to its posts and messages.
    SocialAccount.objects.create(
        workspace=workspace, platform="bluesky", account_platform_id="bsky-2", account_name="Second"
    )
    for response in (api.get("/api/v1/webhooks/"), _create(api), api.get(f"/api/v1/webhooks/{endpoint.id}")):
        assert response.status_code == 403
        assert "covers all of its accounts" in _json(response)["detail"]
    assert "covers all of its accounts" in json.dumps(_rpc(api, "list_webhook_endpoints"))


def test_a_manager_key_can_manage_webhooks(workspace, account, manager):
    # Issuing a key is an org permission; the webhook check is the workspace role.
    OrgMembership.objects.filter(user=manager).update(org_role=OrgMembership.OrgRole.ADMIN)
    manager_api = _client_for(workspace, account, manager, ["manage_social_accounts"])
    assert _create(manager_api).status_code == 201


def test_a_key_only_sees_its_own_workspace(api, org):
    other = Workspace.objects.create(organization=org, name="Other")
    foreign, _ = services.create_endpoint(other, url=PUBLIC_URL, event_types=["post.failed"])
    foreign_delivery = services.send_test_event(foreign)

    assert _json(api.get("/api/v1/webhooks/"))["endpoints"] == []
    assert api.get(f"/api/v1/webhooks/{foreign.id}").status_code == 404
    assert api.patch(f"/api/v1/webhooks/{foreign.id}", data="{}", content_type="application/json").status_code == 404
    assert api.delete(f"/api/v1/webhooks/{foreign.id}").status_code == 404
    assert api.post(f"/api/v1/webhooks/{foreign.id}/rotate-secret").status_code == 404
    assert api.get(f"/api/v1/webhooks/{foreign.id}/deliveries").status_code == 404
    assert api.post(f"/api/v1/webhooks/{foreign.id}/deliveries/{foreign_delivery.id}/resend").status_code == 404
    assert WebhookEndpoint.objects.filter(pk=foreign.pk).exists()


def test_unauthenticated_requests_are_rejected():
    assert SecureClient().get("/api/v1/webhooks/").status_code == 401


# --- MCP -------------------------------------------------------------------


def _rpc(client, name, arguments=None):
    response = client.post(
        MCP_URL,
        data=json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments or {}}}
        ),
        content_type="application/json",
    )
    return _json(response)


def _tool_result(body):
    return json.loads(body["result"]["content"][0]["text"])


def test_mcp_lists_the_webhook_tools(api):
    response = api.post(
        MCP_URL,
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}),
        content_type="application/json",
    )
    names = {tool["name"] for tool in _json(response)["result"]["tools"]}
    assert {
        "list_webhook_endpoints",
        "create_webhook_endpoint",
        "update_webhook_endpoint",
        "delete_webhook_endpoint",
        "rotate_webhook_secret",
        "send_webhook_test",
        "list_webhook_deliveries",
        "resend_webhook_delivery",
    } <= names


def test_mcp_full_lifecycle(api, workspace):
    created = _tool_result(_rpc(api, "create_webhook_endpoint", {"url": PUBLIC_URL, "event_types": ["post.failed"]}))
    endpoint_id = created["id"]
    assert created["secret"].startswith("whsec_")

    listed = _tool_result(_rpc(api, "list_webhook_endpoints"))["endpoints"]
    assert [e["id"] for e in listed] == [endpoint_id] and "secret" not in listed[0]

    updated = _tool_result(_rpc(api, "update_webhook_endpoint", {"endpoint_id": endpoint_id, "is_enabled": False}))
    assert updated["is_enabled"] is False

    rotated = _tool_result(_rpc(api, "rotate_webhook_secret", {"endpoint_id": endpoint_id}))
    assert rotated["secret"] != created["secret"]

    test = _tool_result(_rpc(api, "send_webhook_test", {"endpoint_id": endpoint_id}))
    deliveries = _tool_result(_rpc(api, "list_webhook_deliveries", {"endpoint_id": endpoint_id}))["deliveries"]
    assert [d["id"] for d in deliveries] == [test["id"]]
    in_flight = _rpc(api, "resend_webhook_delivery", {"endpoint_id": endpoint_id, "delivery_id": test["id"]})
    assert "being sent" in json.dumps(in_flight)
    WebhookDelivery.objects.filter(pk=test["id"]).update(status=DeliveryStatus.DELIVERED, next_attempt_at=None)
    resent = _tool_result(_rpc(api, "resend_webhook_delivery", {"endpoint_id": endpoint_id, "delivery_id": test["id"]}))
    assert resent["id"] == test["id"]

    assert _tool_result(_rpc(api, "delete_webhook_endpoint", {"endpoint_id": endpoint_id})) == {
        "deleted": True,
        "endpoint_id": endpoint_id,
    }
    assert not WebhookEndpoint.objects.exists()


def test_mcp_validates_input(api, endpoint):
    private = _rpc(api, "create_webhook_endpoint", {"url": "http://10.1.1.1/x", "event_types": ["post.failed"]})
    assert "public server" in json.dumps(private)
    assert "required property" in json.dumps(_rpc(api, "create_webhook_endpoint", {"url": PUBLIC_URL}))
    assert "not a valid UUID" in json.dumps(_rpc(api, "update_webhook_endpoint", {"endpoint_id": "nope"}))
    assert "is not one of" in json.dumps(
        _rpc(api, "list_webhook_deliveries", {"endpoint_id": str(endpoint.id), "status": "bogus"})
    )
    assert not WebhookEndpoint.objects.exclude(pk=endpoint.pk).exists()


def test_mcp_enforces_the_permission_and_workspace_scope(unprivileged_api, api, org):
    for name, args in [
        ("list_webhook_endpoints", {}),
        ("create_webhook_endpoint", {"url": PUBLIC_URL, "event_types": ["post.failed"]}),
    ]:
        body = _rpc(unprivileged_api, name, args)
        assert "manage_workspace_settings" in json.dumps(body)
    assert not WebhookEndpoint.objects.exists()

    other = Workspace.objects.create(organization=org, name="Other")
    foreign, _ = services.create_endpoint(other, url=PUBLIC_URL, event_types=["post.failed"])
    for name in (
        "update_webhook_endpoint",
        "delete_webhook_endpoint",
        "rotate_webhook_secret",
        "send_webhook_test",
        "list_webhook_deliveries",
    ):
        assert "not found" in json.dumps(_rpc(api, name, {"endpoint_id": str(foreign.id)}))
    assert WebhookEndpoint.objects.filter(pk=foreign.pk).exists()
