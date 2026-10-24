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

import ipaddress
import json
import logging
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar
from urllib.parse import urlsplit, urlunsplit

import httpx
from django.conf import settings

from apps.common.validators import resolve_public_ip

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
    default_message = (
        "Connect your AI to use this: an organization admin can add your own AI provider key under "
        "Organization settings → AI."
    )


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
    # Set by the OpenAI-style providers on failures worth one more attempt.
    retryable = False


class AIRefusalError(AIError):
    code = "ai_refused"
    status_code = 422
    default_message = "The AI assistant declined to write that. Try rephrasing your request."


class AIDailyCapError(AIError):
    """An organization's own key hit the platform's per-day safety cap (``AI_ORGANIZATION_KEY_DAILY_CAP``)."""

    code = "ai_daily_cap"
    status_code = 429
    default_message = (
        "This organization has reached today's safety limit for AI requests on its own key. It resets at midnight UTC."
    )


class AIEndpointError(AIError):
    """The base URL an organization entered can't be used."""

    code = "ai_bad_endpoint"
    status_code = 400
    default_message = "That AI service address can't be used. Check the base URL under Organization → AI assistant."


class AIModelNotFoundError(AIError):
    code = "ai_bad_model"
    status_code = 400
    default_message = (
        "The AI provider doesn't recognize that model or address. Check the model name and base URL "
        "under Organization → AI assistant."
    )


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

    # Whether the provider is reached at an address the organization supplies,
    # and ready-made addresses for the settings page to offer for it as
    # ``(key, label, base_url, local)`` — ``local`` marks a server that
    # normally runs on the customer's own machine or network.
    needs_base_url: ClassVar[bool] = False
    presets: ClassVar[tuple[tuple[str, str, str, bool], ...]] = ()
    # Shown in the key field of the settings page.
    key_placeholder: ClassVar[str] = ""
    # Shortest string accepted as a key; services that need none (a local model
    # server) accept any short placeholder.
    min_key_length: ClassVar[int] = 20

    def __init__(self, *, api_key: str, model: str, base_url: str = ""):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url

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
    key_placeholder = "sk-ant-…"

    def generate(self, *, system: str, prompt: str, schema: dict, max_tokens: int = 8000) -> Completion:
        import anthropic

        # One retry: the composer is waiting, and the SDK's default (two
        # retries on a 10-minute timeout) could hold the request for half an hour.
        client = anthropic.Anthropic(api_key=self.api_key, timeout=settings.AI_REQUEST_TIMEOUT, max_retries=1)
        try:
            response = client.beta.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                # The system prompt (fixed rules + the workspace's brand voice) is
                # byte-identical across a writing session, and the user's text
                # comes after it, so one breakpoint here caches it. Below the
                # model's minimum cacheable prefix (512 tokens on Opus 5.5) the
                # marker is silently ignored at no cost — that's the case for
                # the bare rules; a brand voice with examples crosses it.
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


# ---------------------------------------------------------------------------
# OpenAI-style providers (Chat Completions over HTTPS)
# ---------------------------------------------------------------------------

# Largest answer body read from a provider; a caption-sized JSON object is a few KB.
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


# Pause before the one retry of a request that failed on the network or with a 5xx.
_RETRY_BACKOFF_SECONDS = 1.0


def _http_client() -> httpx.Client:
    """The client every OpenAI-style call goes through. Never follows redirects."""
    return httpx.Client(timeout=max(settings.AI_REQUEST_TIMEOUT / 2, 1), follow_redirects=False)


def _unavailable(message: str = "") -> AIUnavailableError:
    error = AIUnavailableError(message)
    error.retryable = True
    return error


def _pinned_target(url: str) -> tuple[str, dict, dict]:
    """Check that ``url`` may be called and say how to reach exactly what was checked.

    Returns ``(url, headers, extensions)`` to hand to httpx. The base URL is
    customer-supplied, so unless ``AI_ALLOW_PRIVATE_BASE_URLS`` is on it must be
    https and resolve only to public addresses (no private, loopback,
    link-local or cloud-metadata ranges — see ``is_blocked_ip``). Resolution
    happens here, on every request rather than only when the URL is saved, and
    the request is then sent to the address that was checked (Host header and
    TLS server name kept), so a hostname can't answer publicly to the check
    and privately to the connection.

    Raises ``AIEndpointError`` for any URL that fails.
    """
    parts = urlsplit(url)
    try:
        port = parts.port
        host = parts.hostname
    except ValueError as exc:
        raise AIEndpointError() from exc
    if parts.scheme not in ("http", "https") or not host or parts.username or parts.password or parts.fragment:
        raise AIEndpointError()
    if settings.AI_ALLOW_PRIVATE_BASE_URLS:
        return url, {}, {}
    if parts.scheme != "https":
        raise AIEndpointError("The AI service address must start with https://.")
    ip = resolve_public_ip(url)
    if ip is None:
        raise AIEndpointError(
            "That AI service address isn't reachable from here. It has to be a public https:// address; "
            "private and local addresses are not allowed."
        )
    netloc = f"[{ip}]" if ":" in ip else ip
    if port:
        netloc = f"{netloc}:{port}"
    host_header = f"[{host}]" if ":" in host else host
    headers = {"Host": f"{host_header}:{port}" if port else host_header}
    extensions = {}
    try:
        ipaddress.ip_address(host)
    except ValueError:
        # A name: verify the certificate against it, not against the pinned address.
        extensions["sni_hostname"] = host
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, "")), headers, extensions


def validate_base_url(url: str) -> str:
    """The normalized base URL, or ``AIEndpointError`` when it can't be used. Run when settings are saved."""
    url = (url or "").strip().rstrip("/")
    if not url:
        raise AIEndpointError("Enter the base URL of the AI service.")
    if urlsplit(url).query:
        raise AIEndpointError("Enter the base URL without a query string (no ?api-version=…).")
    _pinned_target(url)
    return url


def _extract_json(text: str) -> dict:
    """The JSON object in a model's answer, tolerating a code fence or chatter around it."""
    text = _FENCE_RE.sub("", text.strip())
    candidates = [text]
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    raise AIResponseError()


class OpenAIStyleProvider(AIProvider):
    """A service that speaks OpenAI's Chat Completions API."""

    default_base_url: ClassVar[str] = ""
    # Ways to ask for JSON, best first: "json_schema" (structured outputs),
    # "json_object" (valid JSON, schema described in the prompt), "text"
    # (prompt only, JSON pulled out of the answer). A service that rejects one
    # with a 400/422 is retried with the next.
    response_modes: ClassVar[tuple[str, ...]] = ("json_schema",)
    # OpenAI renamed it; the services that copy the API mostly still use max_tokens.
    max_tokens_param: ClassVar[str] = "max_tokens"

    def _endpoint(self) -> str:
        base = (self.base_url or self.default_base_url).strip().rstrip("/")
        if not base:
            raise AIEndpointError("Enter the base URL of the AI service.")
        return f"{base}/chat/completions"

    def generate(self, *, system: str, prompt: str, schema: dict, max_tokens: int = 8000) -> Completion:
        endpoint = self._endpoint()
        for index, mode in enumerate(self.response_modes):
            payload = self._payload(mode, system, prompt, schema, max_tokens)
            try:
                body = self._post(endpoint, payload)
            except _FormatRejectedError:
                if index == len(self.response_modes) - 1:
                    raise AIError() from None
                continue
            return self._completion(body, schema)
        raise AIError()  # pragma: no cover - response_modes is never empty

    def _payload(self, mode: str, system: str, prompt: str, schema: dict, max_tokens: int) -> dict:
        if mode != "json_schema":
            system = (
                f"{system}\n\nReply with a single JSON object and nothing else, matching this JSON Schema:\n"
                f"{json.dumps(schema)}"
            )
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            self.max_tokens_param: max_tokens,
        }
        if mode == "json_schema":
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "response", "schema": schema, "strict": False},
            }
        elif mode == "json_object":
            payload["response_format"] = {"type": "json_object"}
        return payload

    def _post(self, endpoint: str, payload: dict) -> dict:
        """One request, retried once after a timeout, a transport error, a 408 or a 5xx (not after a 4xx).

        The address is checked again for the retry. Each attempt gets half of
        ``AI_REQUEST_TIMEOUT``, so the composer never waits longer than that in total.
        """
        try:
            return self._post_once(endpoint, payload)
        except AIUnavailableError as exc:
            if not exc.retryable:
                raise
            time.sleep(_RETRY_BACKOFF_SECONDS)
            return self._post_once(endpoint, payload)

    def _post_once(self, endpoint: str, payload: dict) -> dict:
        url, headers, extensions = _pinned_target(endpoint)
        headers = {
            **headers,
            "Authorization": f"Bearer {self.api_key}",
            # Azure OpenAI takes its key here; every other service ignores it.
            "api-key": self.api_key,
            "Content-Type": "application/json",
        }
        try:
            with (
                _http_client() as client,
                client.stream("POST", url, headers=headers, json=payload, extensions=extensions) as response,
            ):
                status = response.status_code
                content = self._read_capped(response) if status < 300 else b""
        except httpx.TimeoutException as exc:
            raise _unavailable() from exc
        except httpx.HTTPError as exc:
            raise _unavailable("The AI service couldn't be reached. Check the base URL and try again.") from exc

        if status in (301, 302, 303, 307, 308):
            raise AIEndpointError("The AI service address redirected somewhere else. Use its final address.")
        if status in (401, 403):
            raise AIAuthenticationError()
        if status == 429:
            raise AIRateLimitError()
        if status == 404:
            raise AIModelNotFoundError()
        if status == 408 or status >= 500:
            raise _unavailable()
        if status in (400, 422):
            raise _FormatRejectedError()
        if status >= 400:
            logger.warning("%s API error %s", self.name, status)
            raise AIError()
        try:
            body = json.loads(content)
        except ValueError as exc:
            raise AIResponseError() from exc
        if not isinstance(body, dict):
            raise AIResponseError()
        return body

    @staticmethod
    def _read_capped(response: httpx.Response) -> bytes:
        """The body, read in chunks and abandoned once it passes ``_MAX_RESPONSE_BYTES``."""
        buf = bytearray()
        for chunk in response.iter_bytes():
            buf += chunk
            if len(buf) > _MAX_RESPONSE_BYTES:
                raise AIResponseError()
        return bytes(buf)

    def _completion(self, body: dict, schema: dict) -> Completion:
        try:
            choice = body["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise AIResponseError() from exc
        if message.get("refusal") or choice.get("finish_reason") == "content_filter":
            raise AIRefusalError()
        if choice.get("finish_reason") == "length":
            raise AIResponseError("The AI assistant's answer was cut off. Try a shorter request.")
        content = message.get("content")
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        if not isinstance(content, str):
            raise AIResponseError()
        data = _extract_json(content)
        required = schema.get("required") or []
        if any(key not in data for key in required):
            raise AIResponseError()
        usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        return Completion(
            data=data,
            model=str(body.get("model") or self.model),
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
        )


class _FormatRejectedError(Exception):
    """The service answered 400/422: probably it doesn't take the requested ``response_format``."""


class OpenAIProvider(OpenAIStyleProvider):
    name = "openai"
    label = "OpenAI"
    platform_key_setting = "OPENAI_API_KEY"
    default_model = "gpt-6.1-sol"
    default_base_url = "https://api.openai.com/v1"
    max_tokens_param = "max_completion_tokens"
    key_placeholder = "sk-…"


class GeminiProvider(OpenAIStyleProvider):
    """Google Gemini through its OpenAI-compatible endpoint."""

    name = "gemini"
    label = "Google Gemini"
    platform_key_setting = "GEMINI_API_KEY"
    default_model = "gemini-3.8-flash"
    default_base_url = "https://generativelanguage.googleapis.com/v1beta/openai"
    # Gemini accepts a narrower set of JSON Schema than OpenAI; fall back if it objects.
    response_modes = ("json_schema", "json_object", "text")
    key_placeholder = "AIza…"


class OpenAICompatibleProvider(OpenAIStyleProvider):
    """Any service that copies OpenAI's Chat Completions API, at an address the organization gives."""

    name = "openai_compatible"
    label = "Any OpenAI-compatible service"
    # No platform-wide key: the address is always the organization's own.
    platform_key_setting = ""
    default_model = ""
    needs_base_url = True
    response_modes = ("json_schema", "json_object", "text")
    key_placeholder = "Your service's API key"
    min_key_length = 4
    presets = (
        ("openrouter", "OpenRouter", "https://openrouter.ai/api/v1", False),
        ("groq", "Groq", "https://api.groq.com/openai/v1", False),
        ("deepseek", "DeepSeek", "https://api.deepseek.com", False),
        ("mistral", "Mistral", "https://api.mistral.ai/v1", False),
        ("together", "Together AI", "https://api.together.xyz/v1", False),
        ("ollama", "Ollama", "http://localhost:11434/v1", True),
    )

    def _endpoint(self) -> str:
        if not self.model:
            raise AIModelNotFoundError("Enter the model name your AI service should use.")
        return super()._endpoint()


AI_PROVIDERS: dict[str, type[AIProvider]] = {
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
    "gemini": GeminiProvider,
    "openai_compatible": OpenAICompatibleProvider,
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


def platform_key_for_customers() -> bool:
    """Whether organizations without their own AI connection fall back to the platform key.

    Off on the hosted service (``AI_PLATFORM_KEY_FOR_CUSTOMERS``): every AI
    feature runs on the organization's own connection, never on ours.
    Self-hosted and dev installs turn it on to share one key.
    """
    return settings.AI_PLATFORM_KEY_FOR_CUSTOMERS and platform_key_configured()


def resolve_ai(organization) -> AIConfig | None:
    """The provider an organization's AI requests go to, or None when AI is off.

    The organization's own key always wins. The platform key is used only
    where ``AI_PLATFORM_KEY_FOR_CUSTOMERS`` allows it; otherwise an
    organization without its own connection has no AI, and every AI surface
    shows "Connect your AI" and every AI job falls back or skips.
    """
    from .models import OrganizationAIKey

    if organization is not None:
        try:
            org_key = OrganizationAIKey.objects.filter(organization=organization).first()
        except ValueError:
            # Undecryptable (e.g. after a SECRET_KEY rotation): don't break the
            # composer; the admin can replace or remove it on the settings page.
            logger.error("Organization %s has an AI key that can't be decrypted", organization.pk)
            org_key = None
        if org_key and org_key.api_key and org_key.provider in AI_PROVIDERS:
            provider_cls = AI_PROVIDERS[org_key.provider]
            return AIConfig(
                provider=provider_cls(
                    api_key=org_key.api_key,
                    model=org_key.model or _model_for(provider_cls),
                    base_url=org_key.base_url,
                ),
                uses_organization_key=True,
            )

    if not platform_key_for_customers():
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


def check_connection(provider: AIProvider) -> None:
    """One tiny request, to prove the key, model and address work. Raises an ``AIError`` if not."""
    provider.generate(
        system="You are a connection check. Reply with JSON only.",
        prompt='Reply with {"ok": true}.',
        schema={
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        },
        max_tokens=300,
    )
