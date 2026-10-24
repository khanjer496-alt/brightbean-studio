"""The OpenAI-style providers (OpenAI, Gemini, any compatible service): requests, JSON fallbacks, errors, SSRF."""

from __future__ import annotations

import json
import logging
from unittest.mock import patch

import httpx
import pytest
from django.urls import reverse

from apps.ai_writer import client
from apps.ai_writer.client import (
    AI_PROVIDERS,
    AIAuthenticationError,
    AIEndpointError,
    AIError,
    AIModelNotFoundError,
    AIRateLimitError,
    AIRefusalError,
    AIResponseError,
    AIUnavailableError,
    GeminiProvider,
    OpenAICompatibleProvider,
    OpenAIProvider,
    check_connection,
    resolve_ai,
    validate_base_url,
)
from apps.ai_writer.models import OrganizationAIKey

SCHEMA = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
PUBLIC_IP = "93.184.216.34"


def chat(content, *, finish="stop", refusal=None):
    message = {"role": "assistant", "content": content}
    if refusal:
        message["refusal"] = refusal
    return {
        "model": "served-model",
        "choices": [{"message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
    }


class Server:
    """Stands in for the provider's HTTP API; records each request."""

    def __init__(self):
        self.replies: list = []
        self.requests: list[httpx.Request] = []

    def queue(self, *replies):
        self.replies.extend(replies)
        return self

    def handler(self, request):
        self.requests.append(request)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        status, body = reply if isinstance(reply, tuple) else (200, reply)
        return httpx.Response(status, json=body)

    def body(self, index=0):
        return json.loads(self.requests[index].content)


@pytest.fixture
def server(settings):
    settings.AI_ALLOW_PRIVATE_BASE_URLS = False
    srv = Server()
    with (
        patch.object(client, "_http_client", lambda: httpx.Client(transport=httpx.MockTransport(srv.handler))),
        patch("apps.common.validators.socket.getaddrinfo", return_value=[(2, 1, 6, "", (PUBLIC_IP, 443))]),
    ):
        yield srv


def run(cls, **kwargs):
    kwargs.setdefault("api_key", "sk-secret-key-0000")
    kwargs.setdefault("model", cls.default_model or "m")
    return cls(**kwargs).generate(system="sys", prompt="hello", schema=SCHEMA)


class TestRegistry:
    def test_all_providers_registered(self):
        assert {"anthropic", "openai", "gemini", "openai_compatible"} <= set(AI_PROVIDERS)

    def test_defaults(self):
        assert OpenAIProvider.default_model and GeminiProvider.default_model
        assert OpenAICompatibleProvider.needs_base_url and not OpenAIProvider.needs_base_url


class TestOpenAI:
    def test_success_uses_structured_outputs(self, server):
        server.queue(chat('{"text": "hi"}'))
        result = run(OpenAIProvider)
        assert result.data == {"text": "hi"}
        assert (result.model, result.input_tokens, result.output_tokens) == ("served-model", 11, 7)
        request = server.requests[0]
        assert request.headers["authorization"] == "Bearer sk-secret-key-0000"
        body = server.body()
        assert body["response_format"]["type"] == "json_schema"
        assert body["response_format"]["json_schema"]["schema"] == SCHEMA
        assert body["max_completion_tokens"] == 8000 and "max_tokens" not in body
        assert body["model"] == OpenAIProvider.default_model
        # Pinned to the address that was checked, with the real host kept.
        assert request.url.host == PUBLIC_IP and request.headers["host"] == "api.openai.com"

    def test_a_400_is_not_retried_without_schema(self, server):
        server.queue((400, {"error": {"message": "nope"}}))
        with pytest.raises(AIError):
            run(OpenAIProvider)
        assert len(server.requests) == 1


class TestGemini:
    def test_success_and_endpoint(self, server):
        server.queue(chat('{"text": "hi"}'))
        assert run(GeminiProvider).data == {"text": "hi"}
        assert server.requests[0].url.path == "/v1beta/openai/chat/completions"
        assert server.body()["max_tokens"] == 8000

    def test_falls_back_when_schema_is_rejected(self, server):
        server.queue((400, {}), chat('{"text": "hi"}'))
        assert run(GeminiProvider).data == {"text": "hi"}
        assert server.body(1)["response_format"] == {"type": "json_object"}


class TestCompatibleFallbackChain:
    kwargs = {"base_url": "https://llm.example.com/v1", "model": "llama"}

    def test_json_schema_first(self, server):
        server.queue(chat('{"text": "a"}'))
        run(OpenAICompatibleProvider, **self.kwargs)
        assert server.requests[0].url.path == "/v1/chat/completions"
        assert server.body()["response_format"]["type"] == "json_schema"
        assert server.requests[0].headers["api-key"] == "sk-secret-key-0000"

    def test_json_object_then_text(self, server):
        server.queue((400, {}), (422, {}), chat('Sure!\n```json\n{"text": "plain"}\n```'))
        result = run(OpenAICompatibleProvider, **self.kwargs)
        assert result.data == {"text": "plain"}
        assert server.body(1)["response_format"] == {"type": "json_object"}
        assert "response_format" not in server.body(2)
        # Without server-side enforcement the schema travels in the prompt.
        assert '"required": ["text"]' in server.body(1)["messages"][0]["content"]
        assert '"required": ["text"]' in server.body(2)["messages"][0]["content"]

    def test_all_modes_rejected(self, server):
        server.queue((400, {}), (400, {}), (400, {}))
        with pytest.raises(AIError):
            run(OpenAICompatibleProvider, **self.kwargs)

    def test_missing_required_key(self, server):
        server.queue(chat('{"other": 1}'))
        with pytest.raises(AIResponseError):
            run(OpenAICompatibleProvider, **self.kwargs)

    @pytest.mark.parametrize("content", ["not json at all", "[1, 2]", None])
    def test_unusable_content(self, server, content):
        server.queue(chat(content))
        with pytest.raises(AIResponseError):
            run(OpenAICompatibleProvider, **self.kwargs)

    def test_needs_model_and_url(self, server):
        with pytest.raises(AIModelNotFoundError):
            run(OpenAICompatibleProvider, base_url="https://llm.example.com/v1", model="")
        with pytest.raises(AIEndpointError):
            run(OpenAICompatibleProvider, base_url="", model="m")


@pytest.mark.parametrize(
    ("reply", "error"),
    [
        ((401, {}), AIAuthenticationError),
        ((403, {}), AIAuthenticationError),
        ((429, {}), AIRateLimitError),
        ((404, {}), AIModelNotFoundError),
        ((500, {}), AIUnavailableError),
        ((503, {}), AIUnavailableError),
        ((302, {}), AIEndpointError),
        (httpx.ReadTimeout("slow"), AIUnavailableError),
        (httpx.ConnectError("down"), AIUnavailableError),
        (chat("{}", finish="length"), AIResponseError),
        (chat("{}", finish="content_filter"), AIRefusalError),
        (chat(None, refusal="no"), AIRefusalError),
        ({"choices": []}, AIResponseError),
    ],
)
def test_error_mapping(server, reply, error):
    server.queue(reply)
    with pytest.raises(error):
        run(OpenAIProvider)


def test_key_never_in_errors_or_logs(server, caplog):
    server.queue((500, {"error": "boom sk-secret-key-0000"}))
    with caplog.at_level(logging.DEBUG), pytest.raises(AIError) as exc:
        run(OpenAIProvider)
    assert "sk-secret" not in str(exc.value) and "sk-secret" not in caplog.text


class TestSSRF:
    @pytest.mark.parametrize(
        "url",
        [
            "http://llm.example.com/v1",
            "ftp://llm.example.com/v1",
            "https://user:pw@llm.example.com/v1",
            "https://llm.example.com/v1?api-version=1",
            "",
            "https://",
        ],
    )
    def test_refused_shapes(self, server, url):
        with pytest.raises(AIEndpointError):
            validate_base_url(url)

    @pytest.mark.parametrize(
        "ip",
        ["127.0.0.1", "10.0.0.5", "192.168.1.1", "172.16.0.9", "169.254.169.254", "100.64.0.1", "0.0.0.0", "224.0.0.1",
         "::1", "fd00:ec2::254", "fe80::1", "::ffff:127.0.0.1", "::ffff:169.254.169.254"],
    )  # fmt: skip
    def test_resolving_to_private_is_refused_at_save_and_request_time(self, server, ip):
        with (
            patch("apps.common.validators.socket.getaddrinfo", return_value=[(2, 1, 6, "", (ip, 443, 0, 0))]),
            pytest.raises(AIEndpointError),
        ):
            validate_base_url("https://llm.example.com/v1")
            with pytest.raises(AIEndpointError):
                run(OpenAICompatibleProvider, base_url="https://llm.example.com/v1", model="m")
        assert not server.requests

    def test_literal_metadata_ip(self, server):
        with (
            patch("apps.common.validators.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("169.254.169.254", 443))]),
            pytest.raises(AIEndpointError),
        ):
            validate_base_url("https://169.254.169.254/latest")

    def test_one_private_answer_among_public_ones_is_refused(self, server):
        answers = [(2, 1, 6, "", (PUBLIC_IP, 443)), (2, 1, 6, "", ("10.0.0.1", 443))]
        with patch("apps.common.validators.socket.getaddrinfo", return_value=answers), pytest.raises(AIEndpointError):
            validate_base_url("https://llm.example.com/v1")

    def test_unresolvable_host(self, server):
        import socket

        with (
            patch("apps.common.validators.socket.getaddrinfo", side_effect=socket.gaierror),
            pytest.raises(AIEndpointError),
        ):
            validate_base_url("https://nope.invalid/v1")

    def test_rebinding_between_save_and_request(self, server):
        # Public when saved...
        assert validate_base_url("https://llm.example.com/v1/") == "https://llm.example.com/v1"
        # ...private by the time a request is made.
        with (
            patch("apps.common.validators.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 443))]),
            pytest.raises(AIEndpointError),
        ):
            run(OpenAICompatibleProvider, base_url="https://llm.example.com/v1", model="m")

    def test_connects_to_the_checked_address_with_tls_name(self, server):
        seen = {}
        original = server.handler

        def spy(request):
            seen["sni"] = request.extensions.get("sni_hostname")
            return original(request)

        server.queue(chat('{"text": "x"}'))
        with patch.object(client, "_http_client", lambda: httpx.Client(transport=httpx.MockTransport(spy))):
            run(OpenAICompatibleProvider, base_url="https://llm.example.com:8443/v1", model="m")
        request = server.requests[0]
        assert (request.url.host, request.url.port) == (PUBLIC_IP, 8443)
        assert request.headers["host"] == "llm.example.com:8443"
        assert seen["sni"] == "llm.example.com"

    def test_private_and_http_allowed_when_setting_on(self, server, settings):
        settings.AI_ALLOW_PRIVATE_BASE_URLS = True
        server.queue(chat('{"text": "local"}'))
        result = run(OpenAICompatibleProvider, base_url="http://localhost:11434/v1", model="llama3")
        assert result.data == {"text": "local"}
        assert str(server.requests[0].url) == "http://localhost:11434/v1/chat/completions"

    def test_ip_validation_helper_still_rejects_for_other_callers(self):
        from apps.common.validators import is_safe_url

        with patch("apps.common.validators.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("100.64.0.1", 443))]):
            assert not is_safe_url("https://x.example.com")


@pytest.mark.django_db
class TestResolution:
    def test_organization_connection_is_used(self, organization):
        OrganizationAIKey.objects.create(
            organization=organization,
            provider="openai_compatible",
            api_key="key-for-compatible",
            base_url="https://llm.example.com/v1",
            model="llama-3",
        )
        provider = resolve_ai(organization).provider
        assert isinstance(provider, OpenAICompatibleProvider)
        assert (provider.base_url, provider.model) == ("https://llm.example.com/v1", "llama-3")

    def test_blank_model_means_provider_default(self, organization):
        OrganizationAIKey.objects.create(organization=organization, provider="gemini", api_key="AIza-key-for-gemini")
        assert resolve_ai(organization).provider.model == GeminiProvider.default_model

    def test_existing_anthropic_rows_keep_working(self, organization, fake_claude):
        # A row as it exists in production: created before base_url / model.
        OrganizationAIKey.objects.create(organization=organization, api_key="sk-ant-existing-key-0000")
        row = OrganizationAIKey.objects.get(organization=organization)
        assert (row.provider, row.base_url, row.model) == ("anthropic", "", "")
        fake_claude.queue({"text": "ok"})
        provider = resolve_ai(organization).provider
        assert provider.name == "anthropic" and provider.model == "claude-opus-5-5"
        assert provider.generate(system="s", prompt="p", schema={"type": "object"}).data == {"text": "ok"}

    def test_check_connection(self, server):
        server.queue(chat('{"ok": true}'))
        check_connection(OpenAIProvider(api_key="sk-secret-key-0000", model="m"))
        assert server.body()["max_completion_tokens"] == 300


@pytest.mark.django_db
class TestSettingsPage:
    url = reverse("ai_writer_org:settings")
    test_url = reverse("ai_writer_org:test")

    def form(self, **overrides):
        data = {"provider": "openai", "api_key": "sk-openai-key-0000-1111", "base_url": "", "model": ""}
        return {**data, **overrides}

    def test_page_lists_providers_presets_and_hides_chatgpt(self, owner_client):
        page = owner_client.get(self.url).content.decode()
        for text in ("OpenAI", "Google Gemini", "Any OpenAI-compatible service", "OpenRouter", "Groq", "Ollama"):
            assert text in page
        assert "coming soon" not in page
        assert "weaker captions" in page

    def test_chatgpt_placeholder_behind_setting(self, owner_client, settings):
        settings.AI_CHATGPT_SIGNIN_ENABLED = True
        assert "Sign in with ChatGPT — coming soon" in owner_client.get(self.url).content.decode()

    def test_hosted_ollama_preset_does_not_point_at_localhost(self, owner_client, settings):
        settings.AI_ALLOW_PRIVATE_BASE_URLS = False
        assert "localhost:11434" not in owner_client.get(self.url).content.decode()
        settings.AI_ALLOW_PRIVATE_BASE_URLS = True
        assert "localhost:11434" in owner_client.get(self.url).content.decode()

    def test_save_runs_the_test_and_stores_on_success(self, owner_client, organization, server):
        server.queue(chat('{"ok": true}'))
        response = owner_client.post(self.url, self.form(model="gpt-x"))
        assert response.status_code == 302
        row = OrganizationAIKey.objects.get(organization=organization)
        assert (row.provider, row.model, row.key_hint) == ("openai", "gpt-x", "1111")
        assert len(server.requests) == 1

    def test_failed_test_does_not_save(self, owner_client, organization, server):
        server.queue((401, {}))
        response = owner_client.post(self.url, self.form())
        assert response.status_code == 200
        page = response.content.decode()
        assert "Not saved" in page and "rejected the API key" in page
        assert "sk-openai-key-0000-1111" not in page
        assert not OrganizationAIKey.objects.exists()

    def test_save_anyway_skips_the_test(self, owner_client, organization, server):
        response = owner_client.post(self.url, self.form(save_anyway="1"))
        assert response.status_code == 302 and not server.requests
        assert OrganizationAIKey.objects.filter(organization=organization).exists()

    def test_odd_answer_can_be_kept_with_save_anyway(self, owner_client, server):
        server.queue(chat("rambling, no json"))
        page = owner_client.post(self.url, self.form()).content.decode()
        assert "Save anyway" in page and not OrganizationAIKey.objects.exists()

    def test_compatible_needs_valid_base_url_and_model(self, owner_client, server):
        base = self.form(provider="openai_compatible", model="llama")
        for bad in ("", "http://llm.example.com/v1", "https://169.254.169.254/v1"):
            with patch(
                "apps.common.validators.socket.getaddrinfo",
                return_value=[(2, 1, 6, "", ("169.254.169.254" if "169" in bad else PUBLIC_IP, 443))],
            ):
                owner_client.post(self.url, {**base, "base_url": bad, "save_anyway": "1"})
        owner_client.post(self.url, {**base, "base_url": "https://llm.example.com/v1", "model": ""})
        assert not OrganizationAIKey.objects.exists()
        owner_client.post(self.url, {**base, "base_url": "https://llm.example.com/v1", "save_anyway": "1"})
        assert OrganizationAIKey.objects.get().base_url == "https://llm.example.com/v1"

    def test_base_url_ignored_for_providers_that_do_not_use_one(self, owner_client, organization):
        owner_client.post(self.url, self.form(base_url="https://evil.example.com", save_anyway="1"))
        assert OrganizationAIKey.objects.get(organization=organization).base_url == ""

    def test_blank_key_keeps_stored_key_for_same_provider(self, owner_client, organization, server):
        OrganizationAIKey.objects.create(
            organization=organization, provider="openai", api_key="sk-openai-stored-key-9999", key_hint="9999"
        )
        server.queue(chat('{"ok": true}'))
        owner_client.post(self.url, self.form(api_key="", model="gpt-y"))
        row = OrganizationAIKey.objects.get()
        assert (row.api_key, row.model) == ("sk-openai-stored-key-9999", "gpt-y")
        assert server.requests[0].headers["authorization"] == "Bearer sk-openai-stored-key-9999"
        # ...but not when switching provider.
        owner_client.post(self.url, self.form(provider="gemini", api_key="", save_anyway="1"))
        assert OrganizationAIKey.objects.get().provider == "openai"

    def test_test_connection_view(self, owner_client, server):
        server.queue(chat('{"ok": true}'))
        ok = owner_client.post(self.test_url, self.form())
        assert ok.status_code == 200 and "Connected" in ok.content.decode()
        server.queue((429, {}))
        busy = owner_client.post(self.test_url, self.form()).content.decode()
        assert "busy" in busy
        bad = owner_client.post(self.test_url, self.form(provider="nope")).content.decode()
        assert "supported AI provider" in bad
        assert not OrganizationAIKey.objects.exists()
        assert "sk-openai-key-0000-1111" not in ok.content.decode() + busy + bad

    def test_key_never_rendered_after_saving(self, owner_client, organization):
        OrganizationAIKey.objects.create(
            organization=organization,
            provider="openai_compatible",
            api_key="super-secret-compat-key",
            key_hint="-key",
            base_url="https://llm.example.com/v1",
            model="llama",
        )
        page = owner_client.get(self.url).content.decode()
        assert "super-secret-compat-key" not in page
        assert "https://llm.example.com/v1" in page

    def test_permissions(self, editor_client, client, server):
        assert editor_client.get(self.url).status_code in (302, 403, 404)
        assert editor_client.post(self.test_url, self.form()).status_code in (302, 403, 404)
        assert client.post(self.test_url, self.form()).status_code == 302
        assert not server.requests

    def test_test_connection_is_post_only(self, owner_client):
        assert owner_client.get(self.test_url).status_code == 405
