"""The answer engine follows the organization's own AI connection, whichever provider it is."""

from __future__ import annotations

import pytest

from apps.ai_visibility.engines import enabled_engines
from apps.ai_writer.models import OrganizationAIKey

pytestmark = pytest.mark.django_db


def test_anthropic_connection(organization):
    OrganizationAIKey.objects.create(organization=organization, api_key="sk-ant-existing-key-0000")
    (engine,) = enabled_engines(organization)
    assert engine.name == "anthropic" and engine.label == "Anthropic (Claude) · claude-opus-5-5"


def test_other_providers_are_labelled_with_provider_and_model(organization):
    OrganizationAIKey.objects.create(organization=organization, provider="openai", api_key="sk-openai-key-0000-1111")
    (engine,) = enabled_engines(organization)
    assert (engine.name, engine.label) == ("openai", "OpenAI · gpt-6.1-sol")


def test_organization_chosen_model_is_shown(organization):
    OrganizationAIKey.objects.create(
        organization=organization,
        provider="openai_compatible",
        api_key="key-for-compat",
        base_url="https://llm.example.com/v1",
        model="llama-3",
    )
    (engine,) = enabled_engines(organization)
    assert engine.label == "Any OpenAI-compatible service · llama-3"


def test_no_connection_no_engine(organization, settings):
    settings.AI_PLATFORM_KEY_FOR_CUSTOMERS = False
    assert enabled_engines(organization) == []


@pytest.mark.parametrize("names", [["anthropic"], ["connected"], ["anthropic", "connected"]])
def test_setting_values_old_and_new(organization, settings, names):
    settings.AI_VISIBILITY_ENGINES = names
    OrganizationAIKey.objects.create(organization=organization, provider="gemini", api_key="AIza-key-for-gemini")
    assert [e.name for e in enabled_engines(organization)] == ["gemini"]


def test_unknown_engine_name_enables_nothing(organization, settings):
    settings.AI_VISIBILITY_ENGINES = ["perplexity"]
    OrganizationAIKey.objects.create(organization=organization, api_key="sk-ant-existing-key-0000")
    assert enabled_engines(organization) == []
