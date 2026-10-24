"""Cross-cutting checks on the endpoints added to close the API/MCP coverage gaps."""

from __future__ import annotations

import pytest
from django.utils import timezone
from ninja.errors import HttpError

from apps.api.api import api
from apps.api.tests.world import make_post
from apps.api_keys.models import ApiKeyAuditLog

NEW_PREFIXES = (
    "/approvals", "/queues", "/slots", "/ideas", "/idea-groups", "/templates", "/tags", "/categories", "/events",
    "/brand-voice", "/usage", "/members",
)  # fmt: skip
SAMPLE = "00000000-0000-4000-8000-000000000001"


def new_operations():
    schema = api.get_openapi_schema()
    for path, methods in schema["paths"].items():
        relative = path.removeprefix("/api/v1")
        if relative.startswith(NEW_PREFIXES) or (relative.startswith("/posts") and relative != "/posts/"):
            for method in methods:
                yield method, relative


def concrete(path: str) -> str:
    import re

    return "/api/v1" + re.sub(r"\{[^}]+\}", SAMPLE, path)


def call(client, method: str, url: str, payload: str = "{}"):
    if method in ("get", "head"):
        return getattr(client, method)(url)
    return getattr(client, method)(url, data=payload, content_type="application/json")


@pytest.mark.django_db
class TestSurface:
    def test_openapi_documents_every_new_route(self):
        paths = set(api.get_openapi_schema()["paths"])
        for expected in (
            "/api/v1/posts/", "/api/v1/posts/{post_id}/duplicate", "/api/v1/approvals/",
            "/api/v1/approvals/{post_id}/approve", "/api/v1/approvals/{post_id}/request-changes",
            "/api/v1/approvals/{post_id}/reject", "/api/v1/approvals/{post_id}/comments", "/api/v1/queues/",
            "/api/v1/queues/{queue_id}/entries", "/api/v1/slots/", "/api/v1/slots/next-free", "/api/v1/ideas/",
            "/api/v1/idea-groups/", "/api/v1/templates/", "/api/v1/tags/", "/api/v1/categories/", "/api/v1/events/",
            "/api/v1/brand-voice/", "/api/v1/usage/",
        ):  # fmt: skip
            assert expected in paths, expected

    def test_every_new_route_has_a_summary_and_requires_auth(self, world):
        checked = 0
        for method, path in new_operations():
            response = call(world.anonymous, method, concrete(path))
            assert response.status_code == 401, (method, path, response.status_code)
            checked += 1
        assert checked > 30
        for path, methods in api.get_openapi_schema()["paths"].items():
            if path.removeprefix("/api/v1").startswith(NEW_PREFIXES):
                for method, op in methods.items():
                    assert op.get("summary"), (method, path)

    def test_error_bodies_use_the_uniform_envelope(self, world):
        not_found = world.full.get(f"/api/v1/ideas/{SAMPLE}")
        assert not_found.status_code == 404
        assert not_found.json() == {"error": "not_found", "detail": "Idea not found"}
        forbidden = world.bare.get("/api/v1/ideas/")
        assert forbidden.json()["error"] == "forbidden"
        assert world.full.post("/api/v1/ideas/", data={}, content_type="application/json").status_code == 422

    def test_writes_are_audited_reads_are_counted_and_failures_are_audited(self, world):
        world.full.get("/api/v1/tags/")
        assert not ApiKeyAuditLog.objects.filter(action="tags.list").exists()  # reads: counted, no row
        r = world.full.post("/api/v1/tags/", data={"name": "audit"}, content_type="application/json")
        row = ApiKeyAuditLog.objects.get(action="tags.create")
        assert row.status_code == 201 and str(row.target_id) == r.json()["id"] and row.method == "POST"
        world.full.get(f"/api/v1/ideas/{SAMPLE}")
        assert ApiKeyAuditLog.objects.filter(action="ideas.read.404").exists()
        post = make_post(world.workspace, world.owner, [world.account1], status="published")
        world.full.delete(f"/api/v1/posts/{post.id}")
        assert ApiKeyAuditLog.objects.filter(action="post.delete.409").exists()
        world.full.delete(f"/api/v1/posts/{make_post(world.workspace, world.owner, [world.account1]).id}")
        assert ApiKeyAuditLog.objects.filter(action="post.delete", status_code=200).exists()

    def test_usage_counters_see_the_new_calls(self, world):
        from apps.api_keys.models import ApiKeyUsageHourly

        world.full.get("/api/v1/usage/")
        assert ApiKeyUsageHourly.objects.filter(action="usage.read", status_code=200).exists()

    @pytest.mark.parametrize(
        ("method", "url"),
        [
            ("get", "/api/v1/posts/"),
            ("delete", f"/api/v1/posts/{SAMPLE}"),
            ("get", "/api/v1/approvals/"),
            ("post", f"/api/v1/approvals/{SAMPLE}/approve"),
            ("get", "/api/v1/queues/"),
            ("get", "/api/v1/slots/"),
            ("post", "/api/v1/ideas/"),
            ("get", "/api/v1/templates/"),
            ("get", "/api/v1/tags/"),
            ("get", "/api/v1/categories/"),
            ("get", "/api/v1/events/"),
            ("get", "/api/v1/brand-voice/"),
            ("get", "/api/v1/usage/"),
        ],
    )
    def test_http_rate_limits_apply(self, world, monkeypatch, method, url):
        calls = []

        def limited(request, *, is_write):
            calls.append(is_write)
            raise HttpError(429, "rate_limited tier=per_key limit=1 remaining=0 retry_after=7")

        monkeypatch.setattr("apps.api.routers.common.enforce_http_rate_limits", limited)
        monkeypatch.setattr("apps.api.routers.posts.enforce_http_rate_limits", limited)
        response = call(world.full, method, url, '{"title": "x"}')
        assert response.status_code == 429, response.content
        assert response["Retry-After"] == "7" and response.json()["tier"] == "per_key"
        assert calls == [method in ("post", "delete", "patch")]

    def test_revoked_key_is_rejected(self, world):
        from apps.api_keys.models import ApiKey

        for key in ApiKey.objects.all():
            ApiKey.objects.filter(pk=key.pk).update(revoked_at=timezone.now())
        assert world.full.get("/api/v1/usage/").status_code == 401
