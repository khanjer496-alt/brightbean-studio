"""Social platform provider registry.

Maps PlatformCredential.Platform enum values to provider classes.
Use get_provider() to instantiate a provider with app credentials.
"""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING

from .bluesky import BlueskyProvider
from .devto import DevtoProvider
from .discord import DiscordProvider
from .facebook import FacebookProvider
from .farcaster import FarcasterProvider
from .google_business import GoogleBusinessProvider
from .instagram import InstagramProvider
from .instagram_login import InstagramLoginProvider
from .lemmy import LemmyProvider
from .linkedin import LINKEDIN_RESERVED_CHARS
from .linkedin_company import LinkedInCompanyProvider
from .linkedin_personal import LinkedInPersonalProvider
from .mastodon import MastodonProvider
from .nostr import NostrProvider
from .pinterest import PinterestProvider
from .reddit import RedditProvider
from .slack import SlackProvider
from .telegram import TelegramProvider
from .threads import ThreadsProvider
from .tiktok import TikTokProvider
from .tumblr import TumblrProvider
from .types import MediaRules, PeakWindow, PlugCapability, PostSetting, PostType, ThreadCapability
from .x import XProvider
from .youtube import YouTubeProvider

if TYPE_CHECKING:
    from .base import SocialProvider

PROVIDER_REGISTRY: dict[str, type[SocialProvider]] = {
    "facebook": FacebookProvider,
    "instagram": InstagramProvider,
    "instagram_login": InstagramLoginProvider,
    "linkedin_personal": LinkedInPersonalProvider,
    "linkedin_company": LinkedInCompanyProvider,
    "tiktok": TikTokProvider,
    "youtube": YouTubeProvider,
    "pinterest": PinterestProvider,
    "threads": ThreadsProvider,
    "bluesky": BlueskyProvider,
    "google_business": GoogleBusinessProvider,
    "mastodon": MastodonProvider,
    "devto": DevtoProvider,
    "telegram": TelegramProvider,
    "discord": DiscordProvider,
    "slack": SlackProvider,
    "reddit": RedditProvider,
    "x": XProvider,
    "tumblr": TumblrProvider,
    "lemmy": LemmyProvider,
    "nostr": NostrProvider,
    "farcaster": FarcasterProvider,
}

# Characters a platform escapes in the caption it publishes. Each one costs two
# characters on the wire, so a limit has to be counted against the escaped text
# rather than the text the user typed.
CAPTION_ESCAPED_CHARS: dict[str, str] = {
    # LinkedIn's little-text commentary; the backslash escapes itself too.
    "linkedin_personal": "\\" + LINKEDIN_RESERVED_CHARS,
    "linkedin_company": "\\" + LINKEDIN_RESERVED_CHARS,
}


def caption_wire_length(platform: str, text: str) -> int:
    """Length of ``text`` as ``platform`` counts it, after any escaping.

    The composer's counter and the published payload have to agree: a LinkedIn
    caption of 2,990 characters holding 20 parentheses arrives as 3,010 and is
    rejected, even though the user was shown a green counter.
    ``providers.linkedin.escape_commentary`` is the transform this mirrors;
    ``tests/providers/test_caption_wire_length.py`` holds the two together.
    A platform that counts some characters as more than one says so through its
    provider's ``caption_length`` (``caption_counting`` tells the composer).
    """
    escaped = CAPTION_ESCAPED_CHARS.get(platform)
    if escaped:
        return len(text) + sum(text.count(ch) for ch in escaped)
    # A platform that weighs characters (X) counts with its own ``caption_length``.
    provider_cls = PROVIDER_REGISTRY.get(platform)
    return provider_cls.caption_length(text) if provider_cls else len(text)


VIDEO_POST_TYPES = frozenset({PostType.VIDEO, PostType.SHORT})


@functools.cache
def is_video_only(platform: str) -> bool:
    """Whether ``platform`` can publish nothing but video.

    The publisher drops every non-video attachment for these platforms (the
    same ``VIDEO_POST_TYPES`` test in ``PublishEngine._dispatch_to_provider``),
    so a post without a video reaches them with nothing to upload and can only
    fail. Cached because ``supported_post_types`` is fixed per provider class,
    and this is asked for every account an API response or scheduling check
    touches.
    """
    provider_cls = PROVIDER_REGISTRY.get(platform)
    if provider_cls is None:
        return False
    return set(provider_cls().supported_post_types) <= VIDEO_POST_TYPES


@functools.cache
def requires_media(platform: str) -> bool:
    """Whether ``platform`` can't publish text on its own.

    True wherever the provider supports no ``TEXT`` post: Instagram and
    Pinterest refuse a post with nothing attached, and the video-only
    platforms are a stricter case of the same. Derived from the provider, like
    ``is_video_only``, so a new platform is covered without a list to update.
    """
    provider_cls = PROVIDER_REGISTRY.get(platform)
    if provider_cls is None:
        return False
    return PostType.TEXT not in provider_cls().supported_post_types


def media_rules_for(platform: str, post_type: PostType) -> MediaRules | None:
    """The media limits ``platform`` declares for ``post_type``, or None.

    Read off the provider class (``SocialProvider.media_rules``), so checking
    a post never builds a provider or touches credentials.
    """
    provider_cls = PROVIDER_REGISTRY.get(platform)
    if provider_cls is None:
        return None
    return provider_cls.media_rules.get(post_type)


def writing_style_for(platform: str) -> str:
    """How a post reads on ``platform``, or "" when it declares no style.

    Read off the provider class (``SocialProvider.writing_style``) like
    ``media_rules_for``, so the AI writing assistant can adapt a caption per
    network without building a provider or touching credentials.
    """
    provider_cls = PROVIDER_REGISTRY.get(platform)
    if provider_cls is None:
        return ""
    return provider_cls.writing_style


# Used for a network that declares no windows of its own, or whose studies
# could not be verified (YouTube, Bluesky, Mastodon as of 2026-10-24): weekday
# late morning to early afternoon, where the 2026 Sprout Social
# (https://sproutsocial.com/insights/best-times-to-post-on-social-media/) and
# Buffer (https://buffer.com/resources/best-time-to-post-social-media/) studies
# overlap across networks. Deliberately broad and labelled "general" in the UI.
DEFAULT_BEST_TIME_WINDOWS: tuple[PeakWindow, ...] = (
    PeakWindow(days=(1, 2, 3), hours=(10, 11, 12, 13)),
    PeakWindow(days=(0, 4), hours=(11, 12)),
)


def best_time_windows_for(platform: str) -> tuple[PeakWindow, ...]:
    """When ``platform``'s audience is most active, best first (industry averages).

    Read off the provider class (``SocialProvider.best_time_windows``) like
    ``writing_style_for``; a network that declares none gets
    ``DEFAULT_BEST_TIME_WINDOWS``.
    """
    provider_cls = PROVIDER_REGISTRY.get(platform)
    windows = provider_cls.best_time_windows if provider_cls is not None else ()
    return windows or DEFAULT_BEST_TIME_WINDOWS


def best_time_source_for(platform: str) -> str:
    """The study ``platform``'s windows come from, or "" when it uses the general defaults."""
    provider_cls = PROVIDER_REGISTRY.get(platform)
    if provider_cls is None or not provider_cls.best_time_windows:
        return ""
    return provider_cls.best_time_source


def thread_capability_for(platform: str) -> ThreadCapability:
    """How ``platform`` carries a thread (``SocialProvider.thread``).

    Read off the provider class like ``media_rules_for``, so the composer and
    the API can check a thread without building a provider. An unknown
    platform takes no thread.
    """
    provider_cls = PROVIDER_REGISTRY.get(platform)
    if provider_cls is None:
        return ThreadCapability()
    return provider_cls.thread


def plug_capability_for(platform: str) -> PlugCapability:
    """What engagement automations may do on ``platform`` (``SocialProvider.plugs``).

    Read off the provider class like ``thread_capability_for``, so the rule editor,
    the checker and the API decide without building a provider or touching
    credentials. An unknown platform takes none.
    """
    provider_cls = PROVIDER_REGISTRY.get(platform)
    if provider_cls is None:
        return PlugCapability()
    return provider_cls.plugs


def mention_lookup_for(platform: str) -> bool:
    """Whether ``platform`` can suggest accounts to @mention (``SocialProvider.mention_lookup``).

    Read off the provider class like ``thread_capability_for``, so the composer
    can decide whether to offer suggestions without building a provider. An
    unknown platform has no lookup.
    """
    provider_cls = PROVIDER_REGISTRY.get(platform)
    if provider_cls is None:
        return False
    return provider_cls.mention_lookup


def accepts_media_for(platform: str) -> bool:
    """Whether ``platform`` publishes media at all (``SocialProvider.accepts_media``).

    Read off the provider class like ``requires_media``; an unknown platform
    is left to say no for itself.
    """
    provider_cls = PROVIDER_REGISTRY.get(platform)
    return provider_cls is None or provider_cls.accepts_media


def publishes_documents_for(platform: str) -> bool:
    """Whether ``platform`` publishes an attached document (``SocialProvider.publishes_documents``).

    Read off the provider class like ``accepts_media_for``. Every provider declares it (False unless its
    network has a documents API this provider implements); an unknown platform publishes none.
    """
    provider_cls = PROVIDER_REGISTRY.get(platform)
    return provider_cls is not None and provider_cls.publishes_documents


def post_settings_for(platform: str) -> tuple[PostSetting, ...]:
    """The per-post settings ``platform`` declares (``SocialProvider.post_settings``).

    Read off the provider class like ``media_rules_for``, so the composer and
    the API can describe and check them without building a provider.
    """
    provider_cls = PROVIDER_REGISTRY.get(platform)
    if provider_cls is None:
        return ()
    return provider_cls.post_settings


def account_default_keys_for(platform: str) -> tuple[str, ...]:
    """The ``platform_extra`` keys an account set may hold as ``platform``'s per-account defaults."""
    provider_cls = PROVIDER_REGISTRY.get(platform)
    if provider_cls is None:
        return ()
    return (*(field.key for field in provider_cls.post_settings), *provider_cls.account_default_keys)


def account_default_choices_for(platform: str) -> dict[str, tuple]:
    """The values each of ``platform``'s account-set defaults may hold, for the keys that aren't free text.

    A declared checkbox takes ``(True, False)``, a declared select with fixed choices takes those, and the
    provider's ``account_default_choices`` cover its other default keys.
    """
    provider_cls = PROVIDER_REGISTRY.get(platform)
    if provider_cls is None:
        return {}
    choices: dict[str, tuple] = {}
    for field in provider_cls.post_settings:
        if field.kind == "checkbox":
            choices[field.key] = (True, False)
        elif field.kind == "select" and field.choices and not field.options_from_provider:
            choices[field.key] = tuple(value for value, _label in field.choices)
    return {**choices, **provider_cls.account_default_choices}


def post_settings_error_for(platform: str, settings: dict | None) -> str:
    """Why ``settings`` can't publish to ``platform`` (a sentence), or "".

    ``SocialProvider.validate_post_settings`` for platforms that declare
    ``post_settings``; platforms that declare none have nothing to refuse.
    """
    provider_cls = PROVIDER_REGISTRY.get(platform)
    if provider_cls is None or not provider_cls.post_settings:
        return ""
    return provider_cls.validate_post_settings(settings or {})


def get_provider(platform: str, credentials: dict | None = None) -> SocialProvider:
    """Instantiate and return a provider for the given platform.

    Args:
        platform: A PlatformCredential.Platform value (e.g. "facebook").
        credentials: Platform app credentials (client_id, client_secret, etc.)
                     from PlatformCredential or settings.PLATFORM_CREDENTIALS_FROM_ENV.
                     If None, falls back to env credentials from
                     ``settings.PLATFORM_CREDENTIALS_FROM_ENV``.

    Raises:
        ValueError: If no provider is registered for the given platform.
    """
    provider_cls = PROVIDER_REGISTRY.get(platform)
    if provider_cls is None:
        raise ValueError(f"No provider registered for platform: {platform}")
    if credentials is None:
        from django.conf import settings

        env_creds = getattr(settings, "PLATFORM_CREDENTIALS_FROM_ENV", {})
        credentials = env_creds.get(platform, {})
    return provider_cls(credentials=credentials)
