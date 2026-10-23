"""Provider resolution (platform key vs bring-your-own) and SDK error translation."""

from __future__ import annotations

import anthropic
import httpx2
import pytest
from django.db import connection

from apps.ai_writer.client import (
    AIAuthenticationError,
    AIRateLimitError,
    AIRefusalError,
    AIResponseError,
    AIUnavailableError,
    ai_available,
    resolve_ai,
)
from apps.ai_writer.models import OrganizationAIKey

from .fakes import message

_REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def _status_error(cls, status):
    return cls("boom", response=httpx2.Response(status, request=_REQUEST), body=None)


def _generate(organization):
    return resolve_ai(organization).provider.generate(system="s", prompt="p", schema={"type": "object"})


@pytest.mark.django_db
class TestResolution:
    def test_off_without_any_key(self, organization):
        assert resolve_ai(organization) is None
        assert not ai_available(organization)

    def test_platform_key(self, platform_key, organization):
        config = resolve_ai(organization)
        assert config.uses_organization_key is False
        assert config.provider.api_key == "sk-ant-platform-key-for-tests"
        assert config.provider.model == "claude-opus-5-5"

    def test_organization_key_wins_over_platform_key(self, platform_key, organization, fake_claude):
        OrganizationAIKey.objects.create(organization=organization, api_key="sk-ant-org-own-key-0000", key_hint="0000")
        config = resolve_ai(organization)
        assert config.uses_organization_key is True
        fake_claude.queue({"text": "hi"})
        _generate(organization)
        assert fake_claude.clients[-1]["api_key"] == "sk-ant-org-own-key-0000"

    def test_organization_key_alone_turns_ai_on(self, organization):
        OrganizationAIKey.objects.create(organization=organization, api_key="sk-ant-org-own-key-0000")
        assert ai_available(organization)

    def test_organization_key_is_encrypted_at_rest(self, organization):
        OrganizationAIKey.objects.create(organization=organization, api_key="sk-ant-org-own-key-0000")
        with connection.cursor() as cursor:
            cursor.execute("SELECT api_key FROM ai_writer_organization_key")
            (raw,) = cursor.fetchone()
        assert "sk-ant" not in raw
        assert OrganizationAIKey.objects.get().api_key == "sk-ant-org-own-key-0000"

    def test_model_setting(self, platform_key, organization):
        platform_key.AI_MODEL = "claude-sonnet-5-5"
        assert resolve_ai(organization).provider.model == "claude-sonnet-5-5"


@pytest.mark.django_db
class TestAnthropicProvider:
    def test_request_shape(self, platform_key, organization, fake_claude):
        fake_claude.queue({"text": "hi"})
        completion = _generate(organization)
        call = fake_claude.calls[0]
        assert call["model"] == "claude-opus-5-5"
        assert call["output_config"]["effort"] == "low"
        assert call["output_config"]["format"]["type"] == "json_schema"
        assert call["fallbacks"] == "default"
        assert call["betas"] == ["server-side-fallback-2026-07-01"]
        assert "thinking" not in call  # Opus 5.5 rejects disabled thinking
        assert fake_claude.clients[0]["max_retries"] == 1
        assert completion.data == {"text": "hi"}
        assert (completion.input_tokens, completion.output_tokens) == (120, 40)

    @pytest.mark.parametrize(
        ("raised", "expected"),
        [
            (_status_error(anthropic.RateLimitError, 429), AIRateLimitError),
            (_status_error(anthropic.AuthenticationError, 401), AIAuthenticationError),
            (_status_error(anthropic.InternalServerError, 500), AIUnavailableError),
            (anthropic.APITimeoutError(request=_REQUEST), AIUnavailableError),
            (anthropic.APIConnectionError(request=_REQUEST), AIUnavailableError),
        ],
    )
    def test_sdk_errors_become_user_facing_errors(self, platform_key, organization, fake_claude, raised, expected):
        fake_claude.queue(raised)
        with pytest.raises(expected) as exc_info:
            _generate(organization)
        # The provider's own message never reaches the user.
        assert "boom" not in exc_info.value.message

    def test_refusal(self, platform_key, organization, fake_claude):
        fake_claude.queue(message({}, stop_reason="refusal"))
        with pytest.raises(AIRefusalError):
            _generate(organization)

    def test_truncated_or_invalid_json(self, platform_key, organization, fake_claude):
        fake_claude.queue(message(text="{not json"), message({"text": "x"}, stop_reason="max_tokens"))
        with pytest.raises(AIResponseError):
            _generate(organization)
        with pytest.raises(AIResponseError):
            _generate(organization)
