"""Abstract base class for social platform providers."""

from __future__ import annotations

import logging
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import IO, ClassVar

import httpx

from .exceptions import APIError, ProviderError, PublishError, RateLimitError
from .types import (
    AccountMetrics,
    AccountProfile,
    AuthType,
    CommentResult,
    Demographics,
    InboxMessage,
    MediaRules,
    MediaType,
    MentionSuggestion,
    OAuthTokens,
    PostMetrics,
    PostSetting,
    PostType,
    PublishContent,
    PublishResult,
    PublishStatus,
    RateLimitConfig,
    ReplyResult,
    ThreadCapability,
    ThreadMode,
)

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30.0

_PROFILE_HANDLE = re.compile(r"[A-Za-z0-9._-]{1,100}")

# Shown to the user as-is (a PublishError message passes through). Retryable,
# so it must not promise anything that stops being true once retries run out.
INTERRUPTED_WAIT_MESSAGE = "Publishing was interrupted by a restart while the platform was still processing the media."


class SocialProvider(ABC):
    """Abstract base class that all social platform providers must implement.

    Each provider is instantiated with app-level credentials (client_id,
    client_secret, etc.) from PlatformCredential or environment variables.
    Per-user OAuth tokens are passed as method arguments.
    """

    # What this instance has spent since the counter was last reset, for
    # platforms that meter a budget worth tracking. Zero everywhere else, and on
    # any call that does not touch it — so a caller can always read it, and
    # reads nothing alarming from a provider that has no such budget.
    #
    # It exists because the cost of a call is not visible from its result: a
    # comment poll that returns three messages may have bought one page or five,
    # and only the provider knows which. YouTube is the case that needs it —
    # 10,000 Data API units a day across every connected channel, spent a page
    # at a time — and without this the first sign of an exhausted budget is a
    # user reporting their accounts have gone dead. See apps.inbox.tasks.
    #
    # Accumulates rather than being reset per call, because one logical poll can
    # be several calls: the YouTube inbox retries once after an auth failure,
    # and a per-call counter would report only the retry while the units the
    # first attempt bought vanished from the tally. Callers reset it once per
    # unit of work via :meth:`reset_quota_counter`.
    last_call_quota_units: int = 0

    def __init__(self, credentials: dict | None = None):
        self.credentials = credentials or {}

    def reset_quota_counter(self) -> None:
        """Start a fresh tally for the next unit of work on this instance."""
        self.last_call_quota_units = 0

    # ------------------------------------------------------------------
    # Class-level metadata (abstract properties)
    # ------------------------------------------------------------------

    @property
    @abstractmethod
    def platform_name(self) -> str:
        """Human-readable platform name (e.g. 'Facebook')."""

    @property
    @abstractmethod
    def auth_type(self) -> AuthType:
        """Authentication type this provider uses."""

    @property
    @abstractmethod
    def max_caption_length(self) -> int:
        """Maximum character length for post text."""

    @property
    @abstractmethod
    def supported_post_types(self) -> list[PostType]:
        """Post types this platform supports."""

    @property
    @abstractmethod
    def supported_media_types(self) -> list[MediaType]:
        """Media types this platform accepts."""

    @property
    @abstractmethod
    def required_scopes(self) -> list[str]:
        """OAuth scopes required for full functionality."""

    @property
    def analytics_only_scopes(self) -> list[str]:
        """OAuth scopes that are ONLY needed for the analytics feature.

        These are conditionally excluded from the OAuth init flow when the
        platform's analytics is disabled in ``AnalyticsPlatformConfig`` — so
        a self-hoster whose Facebook / TikTok / Google app hasn't yet been
        approved for the analytics scope can still connect accounts for
        publishing. Honored by ``facebook``, ``tiktok`` and ``youtube``.

        NOT honored by ``instagram_login``, which lists
        ``instagram_business_manage_insights`` in ``required_scopes``
        unconditionally (as ``instagram`` has always done with
        ``instagram_manage_insights``): the grant is frozen at connect time
        while this toggle can flip afterwards, so deferring the scope minted
        tokens that could never read insights once analytics was switched on.
        The cost of that choice is that an Instagram app without the
        permission added under *Permissions and features* now sees it on the
        authorize URL — see the Instagram (Direct) setup steps in the README.

        Providers without analytics-specific scopes return the default `[]`.
        """
        return []

    # OAuth init can flip this to False to omit ``analytics_only_scopes``
    # from the requested scope list. Default True keeps backward compat.
    include_analytics_scopes: bool = True

    # OAuth providers that require PKCE flip this to True. The connect view then
    # generates a code_verifier, stashes it in the session, sends the derived
    # code_challenge on the authorize URL, and replays the verifier on token
    # exchange. Providers that don't set this are never passed a code_verifier.
    uses_pkce: bool = False

    # True when the platform only *accepts* the post here and finishes it
    # asynchronously, so a successful ``publish_post`` means "handed over", not
    # "live". The engine keeps these rows in ``publishing`` and settles them from
    # ``check_publish_status`` instead of marking them published on upload.
    publish_is_async: bool = False

    # False when the provider publishes purely from ``PublishContent.media_urls``
    # and never reads ``media_files``. The engine skips the (expensive) download
    # to local disk for those. Defaults True so a new provider keeps working
    # until it has been checked.
    needs_local_media: bool = True

    # True when ``get_account_metrics`` actually filters by the ``date_range``
    # argument. Providers whose stats endpoint returns only lifetime totals
    # (TikTok ``/v2/user/info/``) should set this to False so the sync layer
    # doesn't replay the same cumulative values into multiple historical
    # date rows on first sync.
    account_metrics_supports_date_range: bool = True

    # Media limits per post type, checked before a post is scheduled (see
    # ``MediaRules``). A post type with no entry is not checked, so a provider
    # declares only what its platform documents.
    media_rules: ClassVar[dict[PostType, MediaRules]] = {}

    # How a post reads on this platform, for the AI writing assistant
    # (``apps.ai_writer``) when it adapts a caption to each network. One or two
    # sentences of house style — length, register, hashtag and emoji habits —
    # never the character limit, which the assistant reads from the account and
    # enforces itself. Empty means "no particular style": the caption is only
    # fitted to the limit.
    writing_style: ClassVar[str] = ""

    # False when the platform publishes no media at all (Reddit here): a post
    # carrying any is refused before scheduling, with a sentence naming the
    # platform, instead of publishing without it.
    accepts_media: ClassVar[bool] = True

    # How this platform carries the later parts of a thread (see
    # ``ThreadCapability``). The default takes no thread, so a provider has to
    # opt in — and a post with parts is refused for it before scheduling
    # rather than published as its first part alone.
    thread: ClassVar[ThreadCapability] = ThreadCapability()

    # True when the platform has a directory the composer can search while a
    # user types an "@mention", and ``search_mentions`` is implemented. Read off
    # the class (``mention_lookup_for``) so the composer decides whether to
    # offer suggestions without building a provider. Anything False leaves "@"
    # as plain text.
    mention_lookup: ClassVar[bool] = False

    # Per-post settings the platform needs beyond the caption (a subreddit, a
    # flair, a channel). The composer renders them, ``validate_post_settings``
    # gates scheduling, and the API/MCP list them on the account. Empty for
    # platforms that need nothing more.
    post_settings: ClassVar[tuple[PostSetting, ...]] = ()

    # Named URL (in ``social_accounts``) of a connect form that replaces the
    # OAuth redirect: set by platforms whose connection is not an authorization
    # code flow (Telegram asks for the channel). Empty means standard OAuth.
    connect_url_name: ClassVar[str] = ""

    # True when one OAuth grant can serve several destinations (Discord and
    # Slack channels). The callback then asks ``list_destinations`` and lets the
    # user pick which become accounts, like Facebook Pages.
    has_destination_picker: ClassVar[bool] = False

    # True when disconnect should hand ``revoke_token`` the refresh token rather
    # than the access token: it outlives the access token and ends the grant.
    revocation_uses_refresh_token: ClassVar[bool] = False

    # Shown when the destination picker finds nothing to offer, and what to do about it.
    no_destinations_hint: ClassVar[str] = ""

    # True when each customer registers their own developer app and enters its
    # credentials while connecting (X bills API use to the app owner). The platform
    # then counts as configured without app credentials in the environment, and
    # ``connect_url_name`` is the form that collects them.
    user_supplied_credentials: ClassVar[bool] = False

    # How a caption counts against the limit: "characters" (one each), or "weighted" for a
    # platform that overrides ``caption_length`` with twitter-text's weighted count (URLs
    # count as 23, CJK and emoji as 2), which the composer's live counter mirrors.
    caption_counting: ClassVar[str] = "characters"

    # False when the platform can't be connected through a client connection
    # link (``apps.onboarding``), whose page only runs the plain OAuth flow.
    supports_connection_link: ClassVar[bool] = True

    # True when disconnecting calls ``revoke_token`` against a revocation
    # endpoint that ends only this account's grant. Left False wherever one
    # grant is shared by several accounts (every Slack channel of a workspace).
    revokes_grant_on_disconnect: ClassVar[bool] = False

    # How many post ids a single ``get_post_metrics_batch`` call may carry. 1 —
    # the default — means the platform has no batch endpoint, so the analytics
    # sync keeps its one-call-per-post loop and one bad id can't abort the rest
    # of the account. Raise it only for an endpoint that genuinely takes a list
    # (YouTube ``videos.list`` takes 50 ids for the same 1 quota unit). Must
    # never be 0: the chunking loop would not advance.
    post_metrics_batch_size: int = 1

    # True when the platform shows an account's posts as a grid people judge at a glance
    # (Instagram), so the app offers a grid preview that places scheduled posts among the
    # published ones.
    supports_grid_preview: ClassVar[bool] = False

    # Where an account's public profile lives, with ``{handle}`` filled from the account's
    # handle (a leading "@" dropped). Empty when the platform has no such page, or its address
    # needs more than the handle (``profile_url`` overrides then).
    profile_url_template: ClassVar[str] = ""

    @property
    def rate_limits(self) -> RateLimitConfig:
        """Platform rate limit configuration."""
        return RateLimitConfig()

    @classmethod
    def credentials_for_account(cls, account) -> dict:
        """Per-account values the provider needs next to the app credentials.

        ``account`` is the ``SocialAccount``. Destination platforms (a Discord
        channel, a Telegram chat) keep their target in the account row, and this
        is how it reaches the provider without the publisher knowing the
        platform. The default adds nothing.
        """
        return {}

    @classmethod
    def profile_url(cls, account) -> str:
        """The account's public profile address (https), or "" when it can't be built.

        Used wherever the app links out to the account (link-in-bio social icons). The handle
        is checked against a conservative pattern, because it ends up in a public page's links.
        """
        handle = (account.account_handle or "").strip().lstrip("@")
        if not cls.profile_url_template or not _PROFILE_HANDLE.fullmatch(handle):
            return ""
        return cls.profile_url_template.format(handle=handle)

    @classmethod
    def caption_length(cls, text: str) -> int:
        """``text``'s length as the platform counts it against the caption limit."""
        return len(text)

    @classmethod
    def char_limit_for_account(cls, account) -> int | None:
        """This account's caption limit when it differs from ``max_caption_length``, else None.

        For a limit the account's tier sets (X Premium), read from ``account.platform_data``.
        """
        return None

    @classmethod
    def validate_post_settings(cls, settings: dict) -> str:
        """Why ``settings`` can't publish, as a sentence for the user, or "".

        Checked wherever a post is scheduled and again at publish time. The
        default enforces ``post_settings``: a required field must be filled and
        a field must fit its ``max_length`` and static ``choices``. Platforms
        with rules across fields (a link post needs a URL) extend it.
        """
        settings = settings or {}
        for field in cls.post_settings:
            value = settings.get(field.key)
            blank = value is None or value == ""
            if field.required and blank:
                return f"{field.label} is required."
            if blank:
                continue
            if field.max_length and isinstance(value, str) and len(value) > field.max_length:
                return f"{field.label} can be at most {field.max_length} characters."
            if field.choices and value not in {choice for choice, _label in field.choices}:
                return f"{field.label} has an unsupported value."
        return ""

    def get_setting_options(self, access_token: str, key: str, depends_value: str = "") -> list[dict]:
        """Live ``{"id", "name"}`` options for the ``post_settings`` field ``key``."""
        raise NotImplementedError(f"{self.platform_name} has no live options for {key}")

    def list_destinations(self, tokens: OAuthTokens) -> list[dict]:
        """Destinations one grant can post to, for ``has_destination_picker``.

        Each dict is a picker row: ``id``, ``name``, optional ``handle``,
        ``picture`` and ``access_token`` (the token that account posts with,
        defaulting to the grant's own).
        """
        raise NotImplementedError(f"{self.platform_name} has no destination picker")

    # ------------------------------------------------------------------
    # OAuth methods (override for OAuth providers)
    # ------------------------------------------------------------------

    def get_auth_url(self, redirect_uri: str, state: str, code_verifier: str | None = None) -> str:
        """Generate the OAuth authorization URL.

        ``code_verifier`` is only supplied for providers that set
        ``uses_pkce = True``; PKCE providers derive the ``code_challenge`` from it.
        """
        raise NotImplementedError(f"{self.platform_name} does not implement get_auth_url")

    def exchange_code(self, code: str, redirect_uri: str, code_verifier: str | None = None) -> OAuthTokens:
        """Exchange an authorization code for access tokens.

        ``code_verifier`` is only supplied for providers that set
        ``uses_pkce = True``; it must be replayed on the PKCE token exchange.
        """
        raise NotImplementedError(f"{self.platform_name} does not implement exchange_code")

    def refresh_token(self, refresh_token: str) -> OAuthTokens:
        """Refresh an expired access token."""
        raise NotImplementedError(f"{self.platform_name} does not implement refresh_token")

    # ------------------------------------------------------------------
    # Profile (abstract - every provider must implement)
    # ------------------------------------------------------------------

    @abstractmethod
    def get_profile(self, access_token: str) -> AccountProfile:
        """Fetch the authenticated account's profile information."""

    # ------------------------------------------------------------------
    # Publishing (abstract - every provider must implement)
    # ------------------------------------------------------------------

    @abstractmethod
    def publish_post(self, access_token: str, content: PublishContent) -> PublishResult:
        """Publish content to the platform."""

    def delete_post(self, access_token: str, post_id: str) -> bool:
        """Delete a post this account published. Returns True when the platform removed it."""
        raise NotImplementedError(f"{self.platform_name} does not support deleting posts")

    def check_publish_status(self, access_token: str, handle: str) -> PublishStatus:
        """Ask the platform what became of an asynchronous publish.

        ``handle`` is whatever ``publish_post`` returned as
        ``platform_post_id`` for an ``publish_is_async`` provider (for TikTok, a
        Content Posting API ``publish_id``). Only implemented where
        ``publish_is_async`` is True.
        """
        raise NotImplementedError(f"{self.platform_name} does not report publish status")

    def publish_comment(self, access_token: str, post_id: str, text: str) -> CommentResult:
        """Post a comment on an existing post (e.g. first comment)."""
        raise NotImplementedError(f"{self.platform_name} does not support comments")

    def publish_thread_part(
        self,
        access_token: str,
        content: PublishContent,
        *,
        root: PublishResult,
        parent: PublishResult,
    ) -> PublishResult:
        """Publish one later part of a thread.

        ``root`` is the thread's first post and ``parent`` the part just before
        this one (the root itself for the second part); each carries the id and
        response ``extra`` recorded when it was published. A ``COMMENTS``
        platform posts the part's text as a comment on the root through
        ``publish_comment``, the same call the first comment uses. A
        ``REPLY_CHAIN`` provider overrides this to reply to ``parent``.
        """
        if self.thread.mode is ThreadMode.COMMENTS:
            result = self.publish_comment(access_token, root.platform_post_id, content.text)
            return PublishResult(platform_post_id=result.platform_comment_id, extra=result.extra)
        raise NotImplementedError(f"{self.platform_name} does not publish threads")

    def search_mentions(self, access_token: str, query: str, limit: int = 8) -> list[MentionSuggestion]:
        """Accounts whose handle or name matches ``query``, best match first.

        Only called for a provider that sets ``mention_lookup``. Uses the
        connected account's own credentials; a failure raises the provider's
        usual error and the caller treats it as "no suggestions".
        """
        raise NotImplementedError(f"{self.platform_name} does not look up mentions")

    def find_thread_part(
        self,
        access_token: str,
        *,
        root: PublishResult,
        parent: PublishResult,
        text: str,
    ) -> str | None:
        """Return the id of a part this account already published with ``text``.

        The thread counterpart of ``find_own_comment``, for retrying a part
        whose first attempt failed ambiguously. A ``COMMENTS`` platform answers
        through ``find_own_comment`` on the root; anything that cannot answer
        raises, and the caller must then treat an ambiguous failure as terminal
        rather than risk posting the part twice.
        """
        if self.thread.mode is ThreadMode.COMMENTS:
            return self.find_own_comment(access_token, root.platform_post_id, text)
        raise NotImplementedError(f"{self.platform_name} cannot look up its own thread parts")

    # ------------------------------------------------------------------
    # Analytics (optional - override per provider)
    # ------------------------------------------------------------------

    def get_post_metrics(self, access_token: str, post_id: str) -> PostMetrics:
        """Fetch engagement metrics for a specific post."""
        raise NotImplementedError(f"{self.platform_name} does not support post metrics")

    def get_post_metrics_batch(self, access_token: str, post_ids: list[str]) -> dict[str, PostMetrics]:
        """Metrics for several posts in as few API calls as the platform allows.

        Ids the platform omits — deleted, private, never existed — are ABSENT
        from the result. Callers must read that as "no data for this id", never
        as zeros, or a deleted video quietly overwrites its own history with a
        flat line.

        Unlike :meth:`get_post_metrics`, a failure here fails the whole batch.
        That is what a real batched endpoint does, and pretending otherwise
        would hide a quota or auth error behind a partial result.

        The default walks ``get_post_metrics`` so the interface stays total for
        every provider; only platforms that set ``post_metrics_batch_size`` above
        1 should override it.
        """
        return {post_id: self.get_post_metrics(access_token, post_id) for post_id in post_ids}

    def get_account_metrics(self, access_token: str, date_range: tuple[datetime, datetime]) -> AccountMetrics:
        """Fetch account-level metrics for a date range."""
        raise NotImplementedError(f"{self.platform_name} does not support account metrics")

    def get_audience_demographics(self, access_token: str) -> Demographics:
        """Fetch audience demographic data."""
        raise NotImplementedError(f"{self.platform_name} does not support demographics")

    # ------------------------------------------------------------------
    # Inbox (optional - override per provider)
    # ------------------------------------------------------------------

    def get_messages(self, access_token: str, since: datetime | None = None) -> list[InboxMessage]:
        """Fetch inbox messages (comments, mentions, DMs)."""
        raise NotImplementedError(f"{self.platform_name} does not support inbox")

    def reply_to_message(
        self,
        access_token: str,
        message_id: str,
        text: str,
        extra: dict | None = None,
        *,
        human_agent: bool = False,
    ) -> ReplyResult:
        """Reply to a direct message / conversation.

        ``human_agent`` asks the provider to mark the reply as written by a
        person rather than a bot. Meta requires this for replies sent more than
        24 hours after the incoming message; providers without the concept
        ignore it.
        """
        raise NotImplementedError(f"{self.platform_name} does not support message replies")

    def reply_to_comment(self, access_token: str, comment_id: str, text: str, extra: dict | None = None) -> ReplyResult:
        """Reply to a comment or mention.

        Separate from ``reply_to_message`` because platforms answer comments on
        a different edge than conversations — replying to a comment through the
        messaging endpoint silently fails.
        """
        raise NotImplementedError(f"{self.platform_name} does not support comment replies")

    # ------------------------------------------------------------------
    # Webhooks (optional - override per provider)
    # ------------------------------------------------------------------

    def subscribe_webhooks(self, access_token: str, account_id: str) -> bool:
        """Subscribe this app to the account's webhook notifications.

        Called when a user connects an account. Without it the platform never
        pushes comments, mentions or messages to us and the inbox stays empty
        for anything we cannot poll. Returns True when the subscription is
        active.
        """
        return False

    def unsubscribe_webhooks(self, access_token: str, account_id: str) -> bool:
        """Remove this app's webhook subscription. Called on disconnect."""
        return False

    def find_own_comment(self, access_token: str, post_id: str, text: str) -> str | None:
        """Return the id of a comment this account already posted with ``text``.

        Reconciliation hook for retrying a comment whose first attempt failed
        ambiguously (timeout, 5xx): the platform may have created it anyway, and
        a blind retry would double-comment. Providers that cannot answer this
        leave the default, and callers must then treat an ambiguous failure as
        terminal rather than risk the duplicate.
        """
        raise NotImplementedError(f"{self.platform_name} cannot look up its own comments")

    def get_webhook_subscriptions(self, access_token: str, account_id: str) -> list[dict]:
        """Read back which apps are subscribed to this account, and to what.

        Diagnostic counterpart to ``subscribe_webhooks``: a subscription that
        was refused or silently dropped is otherwise invisible.
        """
        raise NotImplementedError(f"{self.platform_name} cannot report webhook subscriptions")

    def debug_token(self, access_token: str) -> dict:
        """Inspect an access token (validity, expiry, granted scopes)."""
        raise NotImplementedError(f"{self.platform_name} does not support token inspection")

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    def get_granted_scopes(self, access_token: str) -> set[str] | None:
        """Which of the requested scopes the platform actually granted.

        Meta silently drops permissions it has not approved, or that the user
        declined, rather than failing the grant — so a connection reports
        healthy and only breaks later, at publish or insights time, with an
        opaque platform error. Asking up front turns that into something we can
        name while the user is still looking at the screen.

        Returns ``None`` when the platform offers no way to ask, which callers
        must treat as "unknown", never as "nothing granted".
        """
        return None

    def revoke_token(self, access_token: str) -> bool:
        """Revoke an OAuth token. Returns True if successful."""
        return False

    def validate_token(self, access_token: str) -> bool:
        """Quick health check - try get_profile and see if it works."""
        try:
            self.get_profile(access_token)
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # HTTP helper
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        url: str,
        *,
        access_token: str | None = None,
        headers: dict | None = None,
        params: dict | None = None,
        json: dict | None = None,
        data: dict | bytes | Iterable[bytes] | IO[bytes] | None = None,
        files: dict | None = None,
        timeout: float = REQUEST_TIMEOUT,
    ) -> httpx.Response:
        """Make an HTTP request with standard error handling.

        Raises APIError on 4xx/5xx, RateLimitError on 429.
        """
        req_headers = {}
        if access_token:
            req_headers["Authorization"] = f"Bearer {access_token}"
        if headers:
            req_headers.update(headers)

        method_upper = method.upper()
        is_mutating = method_upper not in {"GET", "HEAD", "OPTIONS"}
        try:
            with httpx.Client(timeout=timeout) as client:
                # httpx uses `content` for a request body, `data` for form mappings.
                # A file object or byte iterator goes to `content` too, and httpx
                # streams it rather than materializing it — which is the only way a
                # 60 MB video upload doesn't cost 60 MB of RSS. httpx derives
                # Content-Length from a real file object, and an explicit
                # Content-Length passed by the caller still wins (so no stray
                # Transfer-Encoding: chunked on APIs that reject it, like TikTok's).
                request_kwargs: dict = {
                    "headers": req_headers,
                    "params": params,
                    "json": json,
                    "files": files,
                }
                if isinstance(data, dict) or data is None:
                    request_kwargs["data"] = data
                else:
                    request_kwargs["content"] = data
                response = client.request(method, url, **request_kwargs)
        except httpx.TransportError as exc:
            # A timeout/disconnect after a POST/PUT/PATCH/DELETE leaves the
            # remote outcome unknown: the platform may have committed before
            # the response was lost. Preserve that fact for the publish engine
            # instead of treating it as an ordinary transient failure.
            raise APIError(
                f"{self.platform_name} request failed before a response was received",
                status_code=None,
                platform=self.platform_name,
                ambiguous_write=is_mutating,
            ) from exc

        if response.status_code >= 400:
            error = self._error_for_response(response)
            if is_mutating and response.status_code >= 500 and isinstance(error, APIError):
                # A 5xx response to a mutating request does not prove the
                # platform rolled the write back. Treat it as ambiguous so a
                # retry cannot silently duplicate a live post.
                error.ambiguous_write = True
            raise error

        return response

    def _poll_until(
        self,
        check: Callable[[], bool],
        *,
        deadline: float,
        interval: float,
        on_timeout: Callable[[], Exception],
        should_stop: Callable[[], bool] | None = None,
    ) -> None:
        """Call ``check`` until it returns True, sleeping ``interval`` in between.

        For platforms that process media after accepting it. ``check`` raises
        for a terminal failure; ``deadline`` is a ``time.monotonic()`` value so
        several waits can share one budget. When the worker is stopping, the
        wait ends with a retryable error so the engine reschedules the attempt
        before the platform's shutdown grace period runs out. That is checked
        before every call to ``check``, including the first: ``check`` usually
        makes a request, and one started after SIGTERM can by itself outlast
        the grace period.
        """
        while True:
            if should_stop is not None and should_stop():
                raise PublishError(INTERRUPTED_WAIT_MESSAGE, platform=self.platform_name)
            if check():
                return
            if time.monotonic() >= deadline:
                raise on_timeout()
            time.sleep(interval)

    def _error_for_response(self, response: httpx.Response) -> ProviderError:
        """Map an error response to the exception this provider wants raised.

        Overriding this is how a provider teaches the stack to tell its error
        shapes apart — a Google 403 spent on quota is a different fact from a
        403 refused on scope, and only the provider can read the difference out
        of the body.

        Contract an override MUST preserve, because
        ``apps.social_accounts.error_messages`` and the publish engine's retry
        gates route on it: HTTP 429 raises a :class:`RateLimitError`, and every
        other 4xx/5xx raises an :class:`APIError` carrying ``status_code``.
        Anything reclassified has to stay a subclass of one of those two.
        """
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            logger.error("%s API request was rate limited (HTTP 429)", self.platform_name)
            return RateLimitError(
                f"Rate limit exceeded for {self.platform_name}: {response.text[:500]}",
                retry_after=int(retry_after) if retry_after else None,
                platform=self.platform_name,
                raw_response=self._safe_json(response),
            )

        return APIError(
            f"{self.platform_name} API error {response.status_code}: {response.text[:500]}",
            status_code=response.status_code,
            platform=self.platform_name,
            raw_response=self._safe_json(response),
        )

    @staticmethod
    def _safe_json(response: httpx.Response) -> dict:
        """Try to parse response as JSON, return empty dict on failure."""
        try:
            return response.json()
        except Exception:
            return {}
