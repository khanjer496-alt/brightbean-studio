"""Provider-agnostic text generation for the AI writing assistant.

The rest of the app calls ``resolve_ai(org)`` to learn whether AI is on for
an organization and, if so, which provider and key to use, then
``AIProvider.generate()`` to run one prompt. Nothing outside this module
imports a provider SDK, so adding a provider is one ``AIProvider`` subclass
and one ``AI_PROVIDERS`` entry.

Every provider failure is translated into an ``AIError`` subclass whose
``str()`` is written for the person who pressed the button — the composer and
the API show it as-is. Provider messages are never passed through: they can
echo request details back.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar

from django.conf import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AIError(Exception):
    """An AI request that didn't produce usable text. ``str(exc)`` is user-facing."""

    code = "ai_error"
    # HTTP status the composer and the Agent API answer with.
    status_code = 502

    def __init__(self, message: str = ""):
        super().__init__(message or self.default_message)
        self.message = message or self.default_message

    default_message = "The AI assistant couldn't finish that. Please try again."


class AINotConfiguredError(AIError):
    code = "ai_not_configured"
    status_code = 404
    default_message = "The AI assistant isn't set up for this organization."


class AIAuthenticationError(AIError):
    code = "ai_bad_key"
    default_message = "The AI provider rejected the API key. Check the key under Organization → AI assistant."


class AIRateLimitError(AIError):
    code = "ai_rate_limited"
    status_code = 429
    default_message = "The AI provider is busy right now. Wait a minute and try again."


class AIUnavailableError(AIError):
    code = "ai_unavailable"
    status_code = 503
    default_message = "The AI provider didn't respond in time. Please try again."


class AIRefusalError(AIError):
    code = "ai_refused"
    status_code = 422
    default_message = "The AI assistant declined to write that. Try rephrasing your request."


class AIResponseError(AIError):
    code = "ai_bad_response"
    default_message = "The AI assistant returned something unexpected. Please try again."


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Completion:
    """One answered prompt. ``data`` is the JSON object the schema asked for."""

    data: dict
    model: str
    input_tokens: int
    output_tokens: int


class AIProvider(ABC):
    """One AI vendor. Instantiated per request with the key it should use."""

    # Key in ``AI_PROVIDERS`` and in ``OrganizationAIKey.provider``.
    name: ClassVar[str]
    label: ClassVar[str]
    # Settings attribute holding the platform-wide key.
    platform_key_setting: ClassVar[str]
    # Used when ``settings.AI_MODEL`` names another provider's model, i.e. for
    # an organization's own key on a provider the platform doesn't run.
    default_model: ClassVar[str]

    def __init__(self, *, api_key: str, model: str):
        self.api_key = api_key
        self.model = model

    @abstractmethod
    def generate(self, *, system: str, prompt: str, schema: dict, max_tokens: int = 8000) -> Completion:
        """Run ``prompt`` under ``system`` and return the JSON object ``schema`` describes.

        Raises an ``AIError`` subclass on any failure.
        """


class AnthropicProvider(AIProvider):
    name = "anthropic"
    label = "Anthropic (Claude)"
    platform_key_setting = "ANTHROPIC_API_KEY"
    default_model = "claude-opus-5-5"

    def generate(self, *, system: str, prompt: str, schema: dict, max_tokens: int = 8000) -> Completion:
        import anthropic

        # One retry: the composer is waiting, and the SDK's default (two
        # retries on a 10-minute timeout) could hold the request for half an hour.
        client = anthropic.Anthropic(api_key=self.api_key, timeout=settings.AI_REQUEST_TIMEOUT, max_retries=1)
        try:
            response = client.beta.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                # The system prompt carries the workspace's brand voice, which is
                # stable across a writing session — cache it.
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": prompt}],
                # Captions are short and the instructions explicit; low effort
                # keeps them fast and cheap without hurting quality.
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
                # On a safety decline, let the API retry on a suitable model in
                # the same call instead of failing the user's request.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.AuthenticationError as exc:
            raise AIAuthenticationError() from exc
        except anthropic.PermissionDeniedError as exc:
            raise AIAuthenticationError() from exc
        except anthropic.RateLimitError as exc:
            raise AIRateLimitError() from exc
        except anthropic.APITimeoutError as exc:
            raise AIUnavailableError() from exc
        except anthropic.APIConnectionError as exc:
            raise AIUnavailableError() from exc
        except anthropic.APIStatusError as exc:
            logger.warning("Anthropic API error %s (request %s)", exc.status_code, getattr(exc, "request_id", ""))
            if exc.status_code >= 500:
                raise AIUnavailableError() from exc
            raise AIError() from exc

        if response.stop_reason == "refusal":
            raise AIRefusalError()
        if response.stop_reason == "max_tokens":
            raise AIResponseError("The AI assistant's answer was cut off. Try a shorter request.")
        text = next((block.text for block in response.content if block.type == "text"), "")
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise AIResponseError() from exc
        if not isinstance(data, dict):
            raise AIResponseError()

        usage = response.usage
        input_tokens = (
            (usage.input_tokens or 0)
            + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
            + (getattr(usage, "cache_read_input_tokens", 0) or 0)
        )
        return Completion(
            data=data,
            model=response.model,
            input_tokens=input_tokens,
            output_tokens=usage.output_tokens or 0,
        )


AI_PROVIDERS: dict[str, type[AIProvider]] = {
    "anthropic": AnthropicProvider,
}

PROVIDER_CHOICES = [(name, cls.label) for name, cls in AI_PROVIDERS.items()]


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AIConfig:
    """Which provider and key an organization's requests use."""

    provider: AIProvider
    uses_organization_key: bool


def _model_for(provider_cls: type[AIProvider]) -> str:
    if provider_cls.name == settings.AI_PROVIDER and settings.AI_MODEL:
        return settings.AI_MODEL
    return provider_cls.default_model


def platform_provider_class() -> type[AIProvider] | None:
    return AI_PROVIDERS.get(settings.AI_PROVIDER)


def platform_key_configured() -> bool:
    provider_cls = platform_provider_class()
    return bool(provider_cls and getattr(settings, provider_cls.platform_key_setting, "").strip())


def resolve_ai(organization) -> AIConfig | None:
    """The provider an organization's AI requests go to, or None when AI is off.

    The organization's own key wins over the platform key, so an organization
    that brings a key is billed by its provider and not counted against the
    plan's allowance.
    """
    from .models import OrganizationAIKey

    if organization is not None:
        org_key = OrganizationAIKey.objects.filter(organization=organization).first()
        if org_key and org_key.api_key and org_key.provider in AI_PROVIDERS:
            provider_cls = AI_PROVIDERS[org_key.provider]
            return AIConfig(
                provider=provider_cls(api_key=org_key.api_key, model=_model_for(provider_cls)),
                uses_organization_key=True,
            )

    if not platform_key_configured():
        return None
    provider_cls = platform_provider_class()
    return AIConfig(
        provider=provider_cls(
            api_key=getattr(settings, provider_cls.platform_key_setting).strip(),
            model=_model_for(provider_cls),
        ),
        uses_organization_key=False,
    )


def ai_available(organization) -> bool:
    """Whether to show AI actions to this organization at all."""
    return resolve_ai(organization) is not None
