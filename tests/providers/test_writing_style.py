"""``SocialProvider.writing_style`` — per-network house style for the AI assistant."""

from providers import PROVIDER_REGISTRY, writing_style_for


def test_every_provider_declares_a_style():
    for platform, provider_cls in PROVIDER_REGISTRY.items():
        assert provider_cls.writing_style.strip(), platform
        # The limit is the account's to enforce, not the style's to state.
        assert "character" not in provider_cls.writing_style.lower(), platform


def test_lookup():
    assert writing_style_for("linkedin_company") == PROVIDER_REGISTRY["linkedin_company"].writing_style
    assert writing_style_for("not-a-platform") == ""
