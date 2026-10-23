"""X (Twitter) provider: posts, threads, media and polls through the customer's own X developer app.

Since March 2026 X bills every third-party customer for API use through their own
developer app (pay-per-use), so this provider holds no platform-wide credentials.
The customer enters the OAuth 2.0 Client ID and Client Secret of their own app when
they connect (stored as the organisation's ``PlatformCredential``), registers our
callback URL in that app, and we run Authorization Code + PKCE against it. An account
is one X user who authorised that app.

API reference (checked against the current docs, October 2026):

- OAuth 2.0 Authorization Code with PKCE (authorize URL, scopes, refresh with
  ``offline.access``, a code that expires after 30 seconds):
  https://docs.x.com/resources/fundamentals/authentication/oauth-2-0/authorization-code
- Token exchange, confidential-client Basic auth, and revoke (``POST /2/oauth2/revoke``):
  https://docs.x.com/resources/fundamentals/authentication/oauth-2-0/user-access-token
- Create a Post (``POST /2/tweets``: ``text``, ``media.media_ids`` 1-4, ``reply``,
  ``reply_settings`` ``following`` / ``mentionedUsers``, ``poll`` 2-4 options of 1-25
  characters lasting 5-10,080 minutes; ``quote_tweet_id`` needs an Enterprise plan, so
  quoting is not offered): https://docs.x.com/x-api/posts/create-post
- Delete a Post (``DELETE /2/tweets/{id}``, ``data.deleted``):
  https://docs.x.com/x-api/posts/delete-post
- Media upload: simple ``POST /2/media/upload`` for images and the chunked flow
  (``POST /2/media/upload/initialize``, ``.../{id}/append`` in segments of at most 5 MB,
  ``.../{id}/finalize``, ``GET /2/media/upload?command=STATUS``) for video and GIFs;
  categories ``tweet_image`` (5 MB), ``tweet_gif`` (15 MB), ``tweet_video`` (20 min / 8 GB,
  125 min / 16 GB for Premium); a post carries up to 4 photos, 1 GIF or 1 video:
  https://docs.x.com/x-api/media/quickstart/media-upload-chunked
  https://docs.x.com/x-api/media/upload-media
- ``GET /2/users/me`` (``verified_type``, ``subscription_type``, ``profile_image_url``,
  ``public_metrics``): https://docs.x.com/x-api/users/get-my-user
- ``GET /2/users/search``: https://docs.x.com/x-api/users/search
- Rate limits (``x-rate-limit-reset`` is a Unix timestamp; ``POST /2/tweets`` is 100 per
  15 minutes per user): https://docs.x.com/x-api/fundamentals/rate-limits
- Post length is the twitter-text v3 weighted count (``weighted_length`` below):
  https://docs.x.com/resources/fundamentals/counting-characters
  https://github.com/twitter/twitter-text/blob/master/config/v3.json
- Pricing (pay-per-use, $0.015 per post, $0.20 for a post containing a URL; reads of the
  account's own posts $0.001 each; the docs say requests are blocked when credits run out but
  name no status code): https://docs.x.com/x-api/getting-started/pricing
- Errors: a spent usage cap is the ``https://api.x.com/2/problems/usage-capped`` problem, sent
  with HTTP 429: https://docs.x.com/x-api/fundamentals/response-codes-and-errors
- ``GET /2/users/{id}/tweets`` (``since_id``, ``max_results`` 5-100), which reconciles an
  ambiguous reply: https://docs.x.com/x-api/users/get-posts

Analytics are not implemented: every post-metrics read is billed to the customer
(about $0.005 per post read), which is not a cost to incur on a schedule.
"""

from __future__ import annotations

import base64
import hashlib
import html
import logging
import mimetypes
import os
import re
import time
import unicodedata
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from urllib.parse import quote, urlencode

from .base import SocialProvider
from .exceptions import (
    APIError,
    OAuthError,
    ProviderError,
    PublishError,
    QuotaExceededError,
    RateLimitError,
    TokenExpiredError,
)
from .types import (
    AccountProfile,
    AuthType,
    CommentResult,
    MediaRules,
    MediaType,
    MentionSuggestion,
    OAuthTokens,
    PeakWindow,
    PostSetting,
    PostType,
    PublishContent,
    PublishResult,
    RateLimitConfig,
    ThreadCapability,
    ThreadMode,
    UploadCache,
)

logger = logging.getLogger(__name__)

AUTH_URL = "https://x.com/i/oauth2/authorize"
TOKEN_URL = "https://api.x.com/2/oauth2/token"
REVOKE_URL = "https://api.x.com/2/oauth2/revoke"
API_BASE = "https://api.x.com"

SCOPES = ["tweet.read", "tweet.write", "users.read", "media.write", "offline.access"]

DEFAULT_MAX_CHARS = 280
PREMIUM_MAX_CHARS = 25000

# https://docs.x.com/x-api/media/quickstart/media-upload-chunked: segments of at most 5 MB.
CHUNK_BYTES = 5 * 1024 * 1024
MAX_IMAGES = 4
MAX_GIF_BYTES = 15 * 1024 * 1024
MAX_IMAGE_BYTES = 5 * 1024 * 1024

# Same inline wait as the other providers' media processing: the publish cycle waits for the
# slowest post. A longer transcode is not lost, the upload is remembered and the retry resumes.
MEDIA_POLL_DEFAULT_INTERVAL = 3  # seconds when X gives no check_after_secs
MEDIA_POLL_MAX_INTERVAL = 15
MEDIA_PROCESSING_TIMEOUT = 180  # seconds
UPLOAD_REUSE_TTL = 6 * 3600  # seconds; X keeps an unattached upload for expires_after_secs (a day)

MEDIA_TIMEOUT_MESSAGE = "X took too long to process the attached media."
MEDIA_FAILED_MESSAGE = "X couldn't process the attached media. Check the file and publish again."

POLL_MAX_OPTIONS = 4
POLL_OPTION_MAX_LENGTH = 25
POLL_OPTION_SEPARATOR = "|"
POLL_DEFAULT_MINUTES = 1440
POLL_MIN_MINUTES = 5
POLL_MAX_MINUTES = 10080

# ``reply_settings`` values offered besides the default of everyone. X also accepts
# ``subscribers``, which only an account selling subscriptions can use.
_RESTRICTED_REPLY_SETTINGS = ("following", "mentionedUsers", "verified")

_X_DOCS = "https://docs.x.com/x-api/media/quickstart/media-upload-chunked"
_IMAGE_RULES = MediaRules(
    source=_X_DOCS,
    max_items=MAX_IMAGES,
    media_types=frozenset({"image"}),
    max_image_bytes=MAX_IMAGE_BYTES,
)
# 20 minutes / 8 GB for a default account, 125 minutes / 16 GB for Premium: the account's tier
# is not known when a post is checked, so the video limits only warn.
_VIDEO_RULES = MediaRules(
    source=_X_DOCS,
    max_items=1,
    media_types=frozenset({"video"}),
    max_duration=20 * 60,
    max_video_bytes=8 * 1024**3,
    advisory=frozenset({"max_duration", "max_video_bytes"}),
)

# Problem ``title``/``type`` fragments X uses for a spent budget (``usage-capped`` arrives with a
# 429, so it is checked before the rate limit), and what to tell the customer.
_BUDGET_MARKERS = ("usagecap", "usage-capped", "creditsdepleted", "credits-depleted")
BUDGET_MESSAGE = (
    "Your X developer app has no API credits or usage left. Add credits in the X Developer Console, then publish again."
)
DUPLICATE_MESSAGE = "X rejected this post as a duplicate of a recent one. Change the text and publish again."


# twitter-text v3 (config/v3.json): code points in these ranges weigh 1, everything else 2;
# a URL weighs 23 whatever its length and an emoji sequence 2.
_LIGHT_RANGES = ((0, 4351), (8192, 8205), (8208, 8223), (8242, 8247))
TRANSFORMED_URL_LENGTH = 23
# What X's link detection treats as a URL: anything with a scheme or ``www.``, and a bare
# domain on a common top-level domain (twitter-text knows every TLD; an unknown one here
# counts by its characters, which can only undercount a rare bare domain). Trailing
# punctuation is not part of the link. Mirrored by ``weightedLength`` in the composer.
_URL_TAIL = r"(?:/\S*[^\s.,!?:;\"')\]}>])?"
_URL_RE = re.compile(
    r"(?<![a-z0-9_@.-])(?:(?:https?://|www\.)\S*[^\s.,!?:;\"')\]}>]"
    r"|(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+"
    r"(?:com|net|org|io|co|ai|dev|app|me|ly|gg|tv|info|biz|edu|gov|xyz|news|blog|shop|link|site|online"
    r"|uk|de|fr|es|it|nl|ca|au|in|jp|br|ru|ch|se|no|us|eu|ae|sa)(?![a-z0-9_])" + _URL_TAIL + ")",
    re.IGNORECASE,
)
# Code points that continue an emoji sequence: variation selectors, keycap, skin tones, tags.
_EMOJI_CONTINUATION = frozenset({0xFE0E, 0xFE0F, 0x20E3}) | frozenset(range(0x1F3FB, 0x1F400))
_ZWJ = 0x200D


def _is_regional_indicator(cp: int) -> bool:
    return 0x1F1E6 <= cp <= 0x1F1FF


def _is_emoji_continuation(cp: int) -> bool:
    return cp in _EMOJI_CONTINUATION or 0xE0020 <= cp <= 0xE007F


def _weigh(text: str) -> int:
    """Weighted length of ``text`` holding no URL: an emoji sequence counts 2, others by range."""
    cps = [ord(ch) for ch in text]
    total = 0
    i = 0
    while i < len(cps):
        cp = cps[i]
        following = cps[i + 1] if i + 1 < len(cps) else None
        if _is_regional_indicator(cp) and following is not None and _is_regional_indicator(following):
            total += 2  # a flag
            i += 2
            continue
        if 0x1F000 <= cp <= 0x1FAFF or 0x2190 <= cp <= 0x2BFF or following in (0xFE0F, 0x20E3):
            total += 2  # an emoji, with its modifiers and any ZWJ-joined parts
            i += 1
            while i < len(cps):
                if _is_emoji_continuation(cps[i]):
                    i += 1
                elif cps[i] == _ZWJ and i + 1 < len(cps):
                    i += 2
                else:
                    break
            continue
        total += 1 if any(start <= cp <= end for start, end in _LIGHT_RANGES) else 2
        i += 1
    return total


def weighted_length(text: str) -> int:
    """``text``'s length as X counts it against the 280 (or Premium 25,000) limit."""
    text = unicodedata.normalize("NFC", text or "")
    total = 0
    position = 0
    for match in _URL_RE.finditer(text):
        total += _weigh(text[position : match.start()]) + TRANSFORMED_URL_LENGTH
        position = match.end()
    return total + _weigh(text[position:])


def _comparable_text(text: str) -> str:
    """``text`` as X echoes it back, reduced to what survives: no links, unescaped, single spaces."""
    text = html.unescape(unicodedata.normalize("NFC", text or ""))
    return " ".join(_URL_RE.sub(" ", text).split())


def _is_budget_problem(status: int, body: dict) -> bool:
    if status == 402:
        return True
    marker = f"{body.get('title', '')} {body.get('type', '')} {body.get('reason', '')}".lower().replace(" ", "")
    return any(token in marker for token in _BUDGET_MARKERS)


def parse_poll_options(raw: str | None) -> list[str]:
    """``"Yes | No"`` as ``["Yes", "No"]``: the options a poll setting holds, blanks dropped."""
    return [part.strip() for part in (raw or "").split(POLL_OPTION_SEPARATOR) if part.strip()]


def _code_challenge(code_verifier: str) -> str:
    """The S256 PKCE challenge: base64url (no padding) of the verifier's SHA-256 (RFC 7636)."""
    digest = hashlib.sha256(code_verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


class XProvider(SocialProvider):
    """X provider: OAuth 2.0 + PKCE on the customer's own app; posts, threads, media, polls."""

    profile_url_template = "https://x.com/{handle}"

    uses_pkce = True
    user_supplied_credentials = True
    connect_url_name = "connect_x"
    supports_connection_link = False
    revokes_grant_on_disconnect = True
    mention_lookup = True
    caption_counting = "weighted"

    @classmethod
    def permalink_for(cls, platform_post_id: str, account) -> str:
        post_id = (platform_post_id or "").strip()
        return f"https://x.com/i/web/status/{post_id}" if re.fullmatch(r"[0-9]{1,30}", post_id) else ""

    media_rules = {PostType.IMAGE: _IMAGE_RULES, PostType.VIDEO: _VIDEO_RULES}

    # Source: https://sproutsocial.com/insights/best-times-to-post-on-social-media/ (published 2026-03-31; checked 2026-10-24):
    # Mon 2-3pm, 5pm; Tue-Thu 12-6pm. Hours are the audience's local time; industry averages, not your audience.
    best_time_source = "Sprout Social study, Mar 2026"
    best_time_windows = (
        PeakWindow(days=(1, 2, 3), hours=(12, 13, 14)),
        PeakWindow(days=(1, 2, 3), hours=(15, 16, 17)),
        PeakWindow(days=(0,), hours=(14, 17)),
    )

    writing_style = (
        "Short and punchy: lead with the point, plain words, at most one or two hashtags. "
        "Longer ideas read best as a thread of self-contained posts."
    )

    # Each part is a reply to the one before it (``reply.in_reply_to_tweet_id``).
    thread = ThreadCapability(mode=ThreadMode.REPLY_CHAIN, max_parts=25, media_per_part=True)

    post_settings = (
        PostSetting(
            key="reply_settings",
            label="Who can reply",
            kind="select",
            choices=(
                ("everyone", "Everyone"),
                ("following", "Accounts you follow"),
                ("mentionedUsers", "Only accounts you mention"),
                ("verified", "Verified accounts"),
            ),
            help="Leave empty for everyone.",
        ),
        PostSetting(
            key="poll_options",
            label="Poll options",
            kind="text",
            max_length=POLL_MAX_OPTIONS * POLL_OPTION_MAX_LENGTH + POLL_MAX_OPTIONS - 1,
            placeholder="Yes | No | Maybe",
            help=(
                f"Optional poll: 2-4 options separated by {POLL_OPTION_SEPARATOR}, each up to "
                f"{POLL_OPTION_MAX_LENGTH} characters. A poll can't be combined with media."
            ),
        ),
        PostSetting(
            key="poll_duration",
            label="Poll length",
            kind="select",
            choices=(
                ("60", "1 hour"),
                ("360", "6 hours"),
                ("1440", "1 day"),
                ("4320", "3 days"),
                ("10080", "7 days"),
            ),
            help="How long the poll stays open. Defaults to 1 day.",
        ),
    )

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    @property
    def platform_name(self) -> str:
        return "X"

    @property
    def auth_type(self) -> AuthType:
        return AuthType.OAUTH2

    @property
    def max_caption_length(self) -> int:
        return DEFAULT_MAX_CHARS

    @classmethod
    def caption_length(cls, text: str) -> int:
        return weighted_length(text)

    @classmethod
    def char_limit_for_account(cls, account) -> int | None:
        """25,000 characters for a Premium account, else the default.

        The tier is read when the account connects or reconnects, not on token refresh: that
        would be a billed ``users/me`` read every two hours on the customer's app for a flag
        that rarely changes. An account that upgrades reconnects to get the longer limit; one
        that lapses gets X's own "too long" rejection, which the post shows.
        """
        return PREMIUM_MAX_CHARS if (account.platform_data or {}).get("premium") else None

    @property
    def supported_post_types(self) -> list[PostType]:
        return [PostType.TEXT, PostType.IMAGE, PostType.VIDEO]

    @property
    def supported_media_types(self) -> list[MediaType]:
        return [MediaType.JPEG, MediaType.PNG, MediaType.GIF, MediaType.WEBP, MediaType.MP4, MediaType.MOV]

    @property
    def required_scopes(self) -> list[str]:
        return list(SCOPES)

    @property
    def rate_limits(self) -> RateLimitConfig:
        # POST /2/tweets: 100 per 15 minutes per user. Each post is also billed to the customer.
        return RateLimitConfig(requests_per_hour=400, requests_per_day=9600, publish_per_day=100)

    # ------------------------------------------------------------------
    # Requests
    # ------------------------------------------------------------------

    def _client_auth(self) -> dict:
        raw = f"{self.credentials['client_id']}:{self.credentials['client_secret']}".encode()
        return {"Authorization": f"Basic {base64.b64encode(raw).decode()}"}

    def _api(self, method: str, path: str, access_token: str, **kwargs):
        return self._request(method, f"{API_BASE}{path}", access_token=access_token, **kwargs)

    def _error_for_response(self, response):
        """Map an HTTP failure.

        429 with ``x-rate-limit-reset`` (a Unix timestamp) becomes a ``QuotaExceededError`` carrying
        the reset time, so the engine retries after the window instead of on its own ladder. A spent
        budget (HTTP 402 or a usage-cap problem) and X's own rejections are permanent; 401 keeps its
        retries because the token is refreshed before the next attempt; 5xx stays retryable, and the
        base ``_request`` marks a mutating one ambiguous.
        """
        body = self._safe_json(response)
        body = body if isinstance(body, dict) else {}
        status = response.status_code
        detail = str(body.get("detail") or body.get("title") or body.get("error_description") or "")[:200]

        if _is_budget_problem(status, body):
            return PublishError(BUDGET_MESSAGE, platform=self.platform_name, raw_response=body, retryable=False)
        if status == 429:
            reset = response.headers.get("x-rate-limit-reset")
            try:
                resets_at = datetime.fromtimestamp(int(reset), UTC) if reset else None
            except (TypeError, ValueError, OverflowError, OSError):
                resets_at = None
            message = f"Rate limit exceeded for {self.platform_name}"
            if resets_at is not None:
                return QuotaExceededError(
                    message,
                    resets_at=resets_at,
                    status_code=429,
                    platform=self.platform_name,
                    raw_response=body,
                )
            return RateLimitError(message, platform=self.platform_name, raw_response=body)
        if status == 401:
            return APIError(
                f"{self.platform_name} API error 401: {detail}",
                status_code=401,
                platform=self.platform_name,
                raw_response=body,
            )
        if "duplicate" in detail.lower():
            return PublishError(DUPLICATE_MESSAGE, platform=self.platform_name, raw_response=body, retryable=False)
        if 400 <= status < 500:
            # X's own sentence ("Your Post text exceeds ...") says more than a generic rewrite could.
            rejected = PublishError(
                f"X rejected the request: {detail}" if detail else f"X rejected the request ({status}).",
                platform=self.platform_name,
                raw_response=body,
                retryable=False,
            )
            rejected.status_code = status
            return rejected
        return APIError(
            f"{self.platform_name} API error {status}: {detail}",
            status_code=status,
            platform=self.platform_name,
            raw_response=body,
        )

    # ------------------------------------------------------------------
    # OAuth
    # ------------------------------------------------------------------

    def get_auth_url(self, redirect_uri: str, state: str, code_verifier: str | None = None) -> str:
        if not code_verifier:
            raise OAuthError("X needs a PKCE code verifier.", platform=self.platform_name)
        params = {
            "response_type": "code",
            "client_id": self.credentials["client_id"],
            "redirect_uri": redirect_uri,
            "scope": " ".join(self.required_scopes),
            "state": state,
            "code_challenge": _code_challenge(code_verifier),
            "code_challenge_method": "S256",
        }
        # Scopes are space separated; the docs show them as %20.
        return f"{AUTH_URL}?{urlencode(params, quote_via=quote)}"

    def _token_request(self, data: dict) -> dict:
        try:
            body = self._request("POST", TOKEN_URL, headers=self._client_auth(), data=data).json()
        except RateLimitError:
            raise
        except ProviderError as exc:
            error = exc.raw_response.get("error") if isinstance(exc.raw_response, dict) else None
            if error == "invalid_grant":
                raise TokenExpiredError(
                    "X rejected the refresh token. Reconnect the account.",
                    status_code=401,
                    platform=self.platform_name,
                    raw_response=exc.raw_response,
                    retryable=False,
                ) from exc
            raise OAuthError(
                f"X token request failed ({error or getattr(exc, 'status_code', 'error')})",
                platform=self.platform_name,
                raw_response=exc.raw_response,
            ) from exc
        if "access_token" not in body:
            raise OAuthError(
                f"X token request failed ({body.get('error', 'unknown')})",
                platform=self.platform_name,
                raw_response=body,
            )
        return body

    def exchange_code(self, code: str, redirect_uri: str, code_verifier: str | None = None) -> OAuthTokens:
        data = self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier or "",
            }
        )
        if not data.get("refresh_token"):
            raise OAuthError(
                "X didn't return a refresh token, so the connection would end after two hours. "
                "Check that your X app requests offline.access, then connect again.",
                platform=self.platform_name,
                raw_response=data,
            )
        granted = set((data.get("scope") or "").split())
        missing = [scope for scope in SCOPES if scope != "offline.access" and scope not in granted]
        if granted and missing:
            raise OAuthError(
                f"X didn't grant: {', '.join(missing)}. Reconnect and approve every permission.",
                platform=self.platform_name,
                raw_response=data,
            )
        return OAuthTokens(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            expires_in=data.get("expires_in"),
            scope=data.get("scope"),
            raw_response=data,
        )

    def refresh_token(self, refresh_token: str) -> OAuthTokens:
        """Trade the refresh token for a new access token.

        X rotates the refresh token on every use, so the new one is returned and persisted by
        ``SocialAccount.refresh_oauth_token``, which serialises refreshes with a row lock; a second
        concurrent refresh would otherwise spend a refresh token that is already used.
        """
        data = self._token_request({"grant_type": "refresh_token", "refresh_token": refresh_token})
        return OAuthTokens(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token") or refresh_token,
            expires_in=data.get("expires_in"),
            scope=data.get("scope"),
            raw_response=data,
        )

    def revoke_token(self, access_token: str) -> bool:
        try:
            self._request("POST", REVOKE_URL, headers=self._client_auth(), data={"token": access_token})
            return True
        except Exception:
            logger.warning("Failed to revoke X token", exc_info=True)
            return False

    # ------------------------------------------------------------------
    # Profile
    # ------------------------------------------------------------------

    def get_profile(self, access_token: str) -> AccountProfile:
        params = {"user.fields": "profile_image_url,public_metrics,verified_type,subscription_type"}
        user = self._api("GET", "/2/users/me", access_token, params=params).json()["data"]
        premium = user.get("subscription_type") in ("Premium", "PremiumPlus") or user.get("verified_type") == "blue"
        return AccountProfile(
            platform_id=str(user["id"]),
            name=user.get("name") or user["username"],
            handle=user["username"],
            avatar_url=user.get("profile_image_url") or None,
            follower_count=int((user.get("public_metrics") or {}).get("followers_count") or 0),
            # Kept on the account (``SocialAccount.platform_data``): Premium raises the post length.
            extra={"account_data": {"premium": premium}},
        )

    # ------------------------------------------------------------------
    # Post settings
    # ------------------------------------------------------------------

    @classmethod
    def validate_post_settings(cls, settings: dict) -> str:
        settings = settings or {}
        error = super().validate_post_settings(settings)
        if error:
            return error
        raw = settings.get("poll_options")
        if raw:
            options = parse_poll_options(raw)
            if not 2 <= len(options) <= POLL_MAX_OPTIONS:
                return f"A poll needs 2 to {POLL_MAX_OPTIONS} options separated by {POLL_OPTION_SEPARATOR}."
            if any(len(option) > POLL_OPTION_MAX_LENGTH for option in options):
                return f"Each poll option can be at most {POLL_OPTION_MAX_LENGTH} characters."
        return ""

    @staticmethod
    def _poll_minutes(extra: dict) -> int:
        try:
            minutes = int(extra.get("poll_duration") or POLL_DEFAULT_MINUTES)
        except (TypeError, ValueError):
            minutes = POLL_DEFAULT_MINUTES
        return min(max(minutes, POLL_MIN_MINUTES), POLL_MAX_MINUTES)

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    def publish_post(self, access_token: str, content: PublishContent) -> PublishResult:
        """Upload any media, then create the post (a reply when ``in_reply_to_tweet_id`` is set)."""
        extra = content.extra
        error = self.validate_post_settings(extra)
        if error:
            raise PublishError(error, platform=self.platform_name, retryable=False)
        poll_options = parse_poll_options(extra.get("poll_options"))
        media = self._plan_media(content)
        if poll_options and media:
            raise PublishError(
                "X doesn't allow a poll together with media. Remove one of them.",
                platform=self.platform_name,
                retryable=False,
            )
        if not (content.text or "").strip() and not media and not poll_options:
            raise PublishError("X needs some text or media to post.", platform=self.platform_name, retryable=False)

        media_ids = self._upload_media(access_token, content, media)

        body: dict = {}
        if content.text:
            body["text"] = content.text
        if media_ids:
            body["media"] = {"media_ids": media_ids}
        if poll_options:
            body["poll"] = {"options": poll_options, "duration_minutes": self._poll_minutes(extra)}
        if extra.get("reply_settings") in _RESTRICTED_REPLY_SETTINGS:
            body["reply_settings"] = extra["reply_settings"]
        if extra.get("in_reply_to_tweet_id"):
            body["reply"] = {"in_reply_to_tweet_id": str(extra["in_reply_to_tweet_id"])}

        # No idempotency key exists on this endpoint. A timeout or 5xx here is flagged
        # ambiguous_write by ``_request`` and the engine will not retry it blindly.
        data = self._api("POST", "/2/tweets", access_token, json=body).json().get("data") or {}
        tweet_id = str(data.get("id") or "")
        if not tweet_id:
            raise APIError(
                "X accepted the post but returned no id",
                platform=self.platform_name,
                ambiguous_write=True,
            )
        return PublishResult(
            platform_post_id=tweet_id,
            url=f"https://x.com/i/web/status/{tweet_id}",
            extra={"tweet_id": tweet_id},
        )

    def publish_thread_part(
        self,
        access_token: str,
        content: PublishContent,
        *,
        root: PublishResult,
        parent: PublishResult,
    ) -> PublishResult:
        """Publish a thread part as a reply to the part before it (never carrying the first post's poll)."""
        extra = {k: v for k, v in content.extra.items() if k not in ("poll_options", "poll_duration")}
        extra["in_reply_to_tweet_id"] = parent.platform_post_id
        return self.publish_post(access_token, replace(content, extra=extra))

    def publish_comment(self, access_token: str, post_id: str, text: str) -> CommentResult:
        """Reply to a post (the first comment)."""
        body = {"text": text, "reply": {"in_reply_to_tweet_id": str(post_id)}}
        data = self._api("POST", "/2/tweets", access_token, json=body).json().get("data") or {}
        if not data.get("id"):
            raise APIError(
                "X accepted the reply but returned no id",
                platform=self.platform_name,
                ambiguous_write=True,
            )
        return CommentResult(platform_comment_id=str(data["id"]), extra={"tweet_id": str(data["id"])})

    def find_thread_part(
        self,
        access_token: str,
        *,
        root: PublishResult,
        parent: PublishResult,
        text: str,
    ) -> str | None:
        """A part an ambiguous attempt published after all: the account's own post replying to ``parent``."""
        return self._find_own_post_since(access_token, parent.platform_post_id, text)

    def find_own_comment(self, access_token: str, post_id: str, text: str) -> str | None:
        """A first comment (a reply to ``post_id``) an ambiguous attempt published after all."""
        return self._find_own_post_since(access_token, post_id, text)

    def _find_own_post_since(self, access_token: str, after_id: str, text: str) -> str | None:
        """The id of the account's post newer than ``after_id`` whose text is ``text``, else None.

        Reads the account's own timeline from ``after_id`` on (owned reads, about $0.001 a
        post, plus one ``users/me``), so it only runs when a reply's first attempt timed out.
        Only the default ``id`` and ``text`` fields are asked for. X rewrites links to t.co,
        escapes ``&<>`` and shortens a long post's ``text``, so links are dropped and a
        shortened text matches as a prefix. A post the timeline does not show yet reads as
        missing; the retry then posts again and X rejects the duplicate rather than doubling it.
        """
        me = self._api("GET", "/2/users/me", access_token).json()["data"]["id"]
        posts = (
            self._api(
                "GET",
                f"/2/users/{me}/tweets",
                access_token,
                params={"since_id": str(after_id), "max_results": 20},
            )
            .json()
            .get("data")
            or []
        )
        wanted = _comparable_text(text)
        for post in posts:
            found = _comparable_text(post.get("text") or "")
            shortened = found.endswith("\u2026") and len(found) > 1 and wanted.startswith(found[:-1].rstrip())
            if found == wanted or shortened:
                return str(post["id"])
        return None

    def delete_post(self, access_token: str, post_id: str) -> bool:
        """Delete one of the account's posts (``DELETE /2/tweets/{id}``); one already gone counts as deleted."""
        try:
            data = self._api("DELETE", f"/2/tweets/{post_id}", access_token).json().get("data") or {}
        except PublishError as exc:
            if getattr(exc, "status_code", None) == 404:
                return True
            raise
        return bool(data.get("deleted"))

    # ------------------------------------------------------------------
    # Mentions
    # ------------------------------------------------------------------

    def search_mentions(self, access_token: str, query: str, limit: int = 8) -> list[MentionSuggestion]:
        """Accounts matching ``query`` from ``GET /2/users/search`` (within the customer's app access)."""
        data = (
            self._api(
                "GET",
                "/2/users/search",
                access_token,
                params={"query": query, "max_results": max(1, min(limit, 100)), "user.fields": "profile_image_url"},
            )
            .json()
            .get("data")
            or []
        )
        return [
            MentionSuggestion(
                handle=user["username"], name=user.get("name") or "", avatar_url=user.get("profile_image_url") or ""
            )
            for user in data
            if user.get("username")
        ]

    # ------------------------------------------------------------------
    # Media
    # ------------------------------------------------------------------

    def _plan_media(self, content: PublishContent) -> list[tuple[str, str]]:
        """``(path, kind)`` for each attachment, checked against what one X post can carry."""
        plan: list[tuple[str, str]] = []
        for index, path in enumerate(content.media_files or []):
            declared = content.media_types[index] if index < len(content.media_types) else ""
            if declared:
                kind = declared if declared in ("video", "gif") else "image"
            else:
                kind = "video" if content.is_video(index) else "gif" if path.lower().endswith(".gif") else "image"
            plan.append((path, kind))
        kinds = {kind for _path, kind in plan}
        if not plan:
            return plan
        problem = ""
        if kinds == {"image"}:
            if len(plan) > MAX_IMAGES:
                problem = f"X takes at most {MAX_IMAGES} images in one post."
        elif len(plan) > 1:
            problem = "X takes up to 4 images, or a single GIF or video. Mixed or multiple GIF/video isn't allowed."
        elif plan[0][1] == "gif" and os.path.getsize(plan[0][0]) > MAX_GIF_BYTES:
            problem = "X takes GIFs up to 15 MB."
        if problem:
            raise PublishError(problem, platform=self.platform_name, retryable=False)
        return plan

    def _upload_media(self, access_token: str, content: PublishContent, plan: list[tuple[str, str]]) -> list[str]:
        """Media ids for ``plan``, uploading (and waiting for processing) as needed.

        Nothing is posted yet, so a lost response here is safe to retry: the ambiguity flag
        ``_request`` sets on a mutating call is cleared, or the engine would park the post.
        """
        ids: list[str] = []
        deadline = time.monotonic() + MEDIA_PROCESSING_TIMEOUT
        try:
            for position, (path, kind) in enumerate(plan):
                digest = self._file_digest(path)
                key = self._upload_cache_key(access_token, content.idempotency_key, position, digest)
                media_id = self._cached_upload(access_token, key, content.upload_cache, deadline, content)
                if media_id is None:
                    media_id, processing = self._upload_file(access_token, path, kind)
                    if content.upload_cache is not None and key is not None:
                        content.upload_cache.set(key, media_id, UPLOAD_REUSE_TTL)
                    if processing is not None:
                        self._wait_for_media(access_token, media_id, processing, deadline, content)
                ids.append(media_id)
        except APIError as exc:
            exc.ambiguous_write = False
            raise
        return ids

    @staticmethod
    def _file_digest(path: str) -> str:
        with open(path, "rb") as f:
            return hashlib.file_digest(f, "sha256").hexdigest()

    def _upload_cache_key(self, access_token: str, publish_key: str | None, position: int, digest: str) -> str | None:
        """Where an upload is remembered; scoped to one publish and one slot, and to the account (token hashed)."""
        if not publish_key:
            return None
        account = hashlib.sha256(access_token.encode()).hexdigest()[:32]
        publish = hashlib.sha256(publish_key.encode()).hexdigest()[:32]
        return f"x-media:{account}:{publish}:{position}:{digest}"

    def _cached_upload(
        self,
        access_token: str,
        key: str | None,
        cache: UploadCache | None,
        deadline: float,
        content: PublishContent,
    ) -> str | None:
        """A media id an earlier attempt uploaded, once X confirms it is (or becomes) usable."""
        media_id = cache.get(key) if cache is not None and key is not None else None
        if not media_id:
            return None
        try:
            processing = self._media_status(access_token, media_id)
        except PublishError:
            # A 4xx: the upload expired or was attached to a post already. Upload it again.
            return None
        if processing is not None:
            self._wait_for_media(access_token, media_id, processing, deadline, content)
        return media_id

    def _upload_file(self, access_token: str, path: str, kind: str) -> tuple[str, dict | None]:
        """Upload one file. Returns its media id and X's ``processing_info`` when it still has to be waited on."""
        category = f"tweet_{kind}"
        if kind == "image":
            with open(path, "rb") as f:
                body = self._api(
                    "POST",
                    "/2/media/upload",
                    access_token,
                    data={"media_category": category},
                    files={"media": (os.path.basename(path), f, mimetypes.guess_type(path)[0] or "image/jpeg")},
                ).json()
            data = body.get("data") or {}
            return str(data["id"]), data.get("processing_info")

        total = os.path.getsize(path)
        media_type = mimetypes.guess_type(path)[0] or ("image/gif" if kind == "gif" else "video/mp4")
        init = self._api(
            "POST",
            "/2/media/upload/initialize",
            access_token,
            json={"media_type": media_type, "total_bytes": total, "media_category": category},
        ).json()
        media_id = str(init["data"]["id"])
        with open(path, "rb") as f:
            for segment, chunk in enumerate(iter(partial(f.read, CHUNK_BYTES), b"")):
                self._api(
                    "POST",
                    f"/2/media/upload/{media_id}/append",
                    access_token,
                    data={"segment_index": str(segment)},
                    files={"media": ("chunk", chunk, "application/octet-stream")},
                )
        final = self._api("POST", f"/2/media/upload/{media_id}/finalize", access_token).json()
        return media_id, (final.get("data") or {}).get("processing_info")

    def _media_status(self, access_token: str, media_id: str) -> dict | None:
        """``processing_info`` for an upload (``GET /2/media/upload?command=STATUS``), None when it is done."""
        body = self._api(
            "GET", "/2/media/upload", access_token, params={"command": "STATUS", "media_id": media_id}
        ).json()
        return (body.get("data") or body).get("processing_info")

    def _wait_for_media(
        self, access_token: str, media_id: str, processing: dict, deadline: float, content: PublishContent
    ) -> None:
        """Poll the upload's status until X reports it succeeded, honouring ``check_after_secs``."""
        state = {"info": processing}

        def settled() -> bool:
            info = state["info"]
            if not info or info.get("state") == "succeeded":
                return True
            if info.get("state") == "failed":
                raise PublishError(
                    MEDIA_FAILED_MESSAGE,
                    platform=self.platform_name,
                    raw_response=info,
                    retryable=False,
                )
            time.sleep(self._poll_interval(info))
            state["info"] = self._media_status(access_token, media_id)
            return False

        self._poll_until(
            settled,
            deadline=deadline,
            interval=0,
            on_timeout=lambda: PublishError(MEDIA_TIMEOUT_MESSAGE, platform=self.platform_name),
            should_stop=content.should_stop,
        )

    @staticmethod
    def _poll_interval(info: dict) -> float:
        try:
            wait = float(info.get("check_after_secs") or MEDIA_POLL_DEFAULT_INTERVAL)
        except (TypeError, ValueError):
            wait = MEDIA_POLL_DEFAULT_INTERVAL
        return min(max(wait, 1), MEDIA_POLL_MAX_INTERVAL)
