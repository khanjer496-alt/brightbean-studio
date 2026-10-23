import pytest
from django.urls import reverse

from apps.members.models import WorkspaceMembership
from apps.webhooks import services
from apps.webhooks.models import WebhookEndpoint
from apps.workspaces.models import Workspace

from .conftest import PUBLIC_URL, add_member, make_user

pytestmark = pytest.mark.django_db


def _url(name, workspace, *args):
    return reverse(f"webhooks:{name}", kwargs={"workspace_id": workspace.id, **dict(args)})


def _endpoint_url(name, workspace, endpoint, **extra):
    return reverse(f"webhooks:{name}", kwargs={"workspace_id": workspace.id, "endpoint_id": endpoint.id, **extra})


def test_owner_sees_the_list_and_the_settings_nav_link(client, owner, workspace, endpoint):
    client.force_login(owner)
    response = client.get(_url("list", workspace))
    assert response.status_code == 200
    html = response.content.decode()
    assert PUBLIC_URL in html
    assert _url("list", workspace) in html  # sidebar link


def test_managers_and_non_members_are_refused_everywhere(client, manager, workspace, endpoint, org):
    outsider = make_user("outsider@example.com")
    delivery = services.send_test_event(endpoint)
    pages = [
        ("get", _url("list", workspace)),
        ("get", _url("create", workspace)),
        ("post", _url("create", workspace)),
        ("get", _endpoint_url("detail", workspace, endpoint)),
        ("post", _endpoint_url("update", workspace, endpoint)),
        ("post", _endpoint_url("rotate_secret", workspace, endpoint)),
        ("post", _endpoint_url("test", workspace, endpoint)),
        ("post", _endpoint_url("delete", workspace, endpoint)),
        ("post", _endpoint_url("resend", workspace, endpoint, delivery_id=delivery.id)),
    ]
    for user in (manager, outsider):
        client.force_login(user)
        for method, url in pages:
            assert getattr(client, method)(url, {"url": PUBLIC_URL}).status_code == 403, (user.email, method, url)
    assert WebhookEndpoint.objects.filter(pk=endpoint.pk).exists()


def test_anonymous_users_are_sent_to_login(client, workspace):
    response = client.get(_url("list", workspace))
    assert response.status_code == 302 and "login" in response["Location"]


def test_an_endpoint_of_another_workspace_is_a_404(client, owner, org, endpoint):
    other = Workspace.objects.create(organization=org, name="Other")
    add_member(owner, org, other, WorkspaceMembership.WorkspaceRole.OWNER)
    client.force_login(owner)
    # Same endpoint id, reached through a workspace it does not belong to.
    for name in ("detail",):
        assert client.get(_endpoint_url(name, other, endpoint)).status_code == 404
    for name in ("update", "rotate_secret", "test", "delete"):
        assert client.post(_endpoint_url(name, other, endpoint)).status_code == 404
    assert WebhookEndpoint.objects.filter(pk=endpoint.pk).exists()


def test_creating_shows_the_secret_once(client, owner, workspace):
    client.force_login(owner)
    response = client.post(
        _url("create", workspace),
        {
            "url": PUBLIC_URL,
            "description": "CMS sync",
            "event_types": ["post.published", "post.failed"],
            "is_enabled": "on",
        },
        follow=True,
    )
    endpoint = WebhookEndpoint.objects.get()
    assert endpoint.event_types == ["post.published", "post.failed"] and endpoint.created_by == owner
    assert endpoint.secret in response.content.decode()

    again = client.get(_endpoint_url("detail", workspace, endpoint))
    assert endpoint.secret not in again.content.decode()
    assert endpoint.secret_hint in again.content.decode()


def test_invalid_input_re_renders_with_an_error_and_keeps_the_values(client, owner, workspace):
    client.force_login(owner)
    response = client.post(_url("create", workspace), {"url": "http://10.0.0.1/x", "event_types": ["post.failed"]})
    assert response.status_code == 200
    assert "public server" in response.content.decode()
    assert 'value="http://10.0.0.1/x"' in response.content.decode()
    assert not WebhookEndpoint.objects.exists()


def test_update_changes_the_endpoint_and_enabling_clears_a_shut_off(client, owner, workspace, endpoint):
    WebhookEndpoint.objects.filter(pk=endpoint.pk).update(is_enabled=False, consecutive_failures=15)
    client.force_login(owner)
    client.post(
        _endpoint_url("update", workspace, endpoint),
        {"url": "https://hooks.example.com/new", "event_types": ["post.failed"], "is_enabled": "on"},
    )
    endpoint.refresh_from_db()
    assert endpoint.url == "https://hooks.example.com/new"
    assert endpoint.event_types == ["post.failed"]
    assert endpoint.is_enabled and endpoint.consecutive_failures == 0


def test_rotate_shows_a_new_secret_once_and_replaces_the_old(client, owner, workspace, endpoint):
    client.force_login(owner)
    response = client.post(_endpoint_url("rotate_secret", workspace, endpoint), follow=True)
    endpoint.refresh_from_db()
    assert endpoint.secret != endpoint.plain_secret
    assert endpoint.secret in response.content.decode()


def test_test_event_and_resend_queue_deliveries_and_the_log_lists_them(client, owner, workspace, endpoint):
    client.force_login(owner)
    client.post(_endpoint_url("test", workspace, endpoint))
    delivery = endpoint.deliveries.get()
    assert delivery.event_type == "webhook.test"

    delivery.status = "failed"
    delivery.attempts = 6
    delivery.save()
    client.post(_endpoint_url("resend", workspace, endpoint, delivery_id=delivery.id))
    delivery.refresh_from_db()
    assert delivery.status == "pending" and delivery.attempts == 0

    html = client.get(_endpoint_url("detail", workspace, endpoint)).content.decode()
    assert "webhook.test" in html and str(delivery.id) in html and "Resend" in html


def test_the_log_shows_at_most_100_deliveries(client, owner, workspace, endpoint):
    for _ in range(105):
        services.send_test_event(endpoint)
    client.force_login(owner)
    html = client.get(_endpoint_url("detail", workspace, endpoint)).content.decode()
    assert html.count("<details") == 100


def test_delete_removes_the_endpoint_and_its_log(client, owner, workspace, endpoint):
    services.send_test_event(endpoint)
    client.force_login(owner)
    client.post(_endpoint_url("delete", workspace, endpoint))
    assert not WebhookEndpoint.objects.exists()


def test_writes_need_post(client, owner, workspace, endpoint):
    client.force_login(owner)
    for name in ("update", "rotate_secret", "test", "delete"):
        assert client.get(_endpoint_url(name, workspace, endpoint)).status_code == 405
