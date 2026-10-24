"""Publishing Engine - background worker logic (F-2.4).

This module implements the core publish loop:
1. Poll for PlatformPosts where scheduled_at <= now() and status = 'scheduled'.
2. Transition each due PlatformPost to 'publishing'.
3. Dispatch platform posts in parallel.
4. Handle retries with exponential backoff.
5. Post first comment after 2-minute delay.
6. Publish a thread's later parts, one background task per part.
7. Update per-platform status and log results.

Status is owned entirely by ``PlatformPost`` — the parent ``Post`` exposes an
aggregate ``status`` property derived from its children (see
``apps.composer.status``).
"""

import contextlib
import logging
import math
import os
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta

from background_task import background
from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import F
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.approvals.policy import approval_allows_publish
from apps.autonomy.release import hold_at_send as autonomy_hold_at_send
from apps.autonomy.release import hold_exclusion as autonomy_hold_exclusion
from apps.autonomy.release import is_platform_post_held as is_autonomy_held
from apps.autonomy.release import send_blocker as autonomy_send_blocker
from apps.common.db import in_worker_thread, release_idle_connection
from apps.common.worker import SHUTDOWN_REQUESTED
from apps.composer.models import PlatformPost, PlatformPostPart
from apps.composer.services import (
    media_rules_blocker,
    platform_post_thread_blocker,
    post_media_assets,
    publish_blocker,
    thread_parts_for,
    usable_media_types,
)
from apps.credentials.models import resolve_platform_credentials
from apps.media_library.storage import download_to_path
from apps.social_accounts.error_messages import (
    FIRST_COMMENT_GENERIC_MESSAGE,
    PUBLISH_AMBIGUOUS_MESSAGE,
    PUBLISH_CONFIRM_TIMEOUT_MESSAGE,
    PUBLISH_GENERIC_MESSAGE,
    PUBLISH_INTERRUPTED_MESSAGE,
    PUBLISH_RATE_LIMIT_MESSAGE,
    PUBLISH_UNCONFIRMED_MESSAGE,
    THREAD_PART_AMBIGUOUS_MESSAGE,
    exhausted_publish_message,
    friendly_first_comment_error,
    friendly_publish_error,
    is_credential_rejection,
)
from apps.webhooks import events as webhook_events
from providers import PROVIDER_REGISTRY, VIDEO_POST_TYPES, get_provider
from providers.exceptions import APIError, ProviderError, PublishError, RateLimitError
from providers.types import PostType, PublishContent, PublishResult, PublishState

from .models import PublishLog, RateLimitState

logger = logging.getLogger(__name__)

# Retry backoff schedule (in seconds)
RETRY_BACKOFF = [60, 300, 1800]  # 1min, 5min, 30min


def _resolve_publish_credentials(account):
    """Resolve the credentials dict for publishing on behalf of `account`.

    Combines org-level `PlatformCredential` (with `.env` dominant) with
    per-account federation metadata (Mastodon `instance_url` +
    `MastodonAppRegistration`, Bluesky `pds_url`). Returns a plain dict
    suitable for `get_provider(platform, credentials)`.
    """
    platform = account.platform

    # .env is dominant; admin-entered org credentials are the fallback.
    credentials = resolve_platform_credentials(platform, account.workspace.organization_id, account.workspace_id)

    if platform == "mastodon" and account.instance_url:
        from apps.common.validators import is_safe_url

        if is_safe_url(account.instance_url):
            credentials["instance_url"] = account.instance_url
            if not credentials.get("client_id"):
                from apps.social_accounts.models import MastodonAppRegistration

                try:
                    reg = MastodonAppRegistration.objects.get(instance_url=account.instance_url)
                    credentials["client_id"] = reg.client_id
                    credentials["client_secret"] = reg.client_secret
                except MastodonAppRegistration.DoesNotExist:
                    pass
        else:
            logger.warning(
                "Mastodon instance URL failed SSRF check for account %s",
                account.id,
            )
    elif platform == "bluesky" and account.instance_url:
        from apps.common.validators import is_safe_url

        if is_safe_url(account.instance_url):
            credentials["pds_url"] = account.instance_url
        else:
            logger.warning(
                "Bluesky PDS URL failed SSRF check for account %s",
                account.id,
            )
    elif platform == "facebook":
        credentials["page_id"] = account.account_platform_id
    elif platform in ("instagram", "instagram_login"):
        credentials["ig_user_id"] = account.account_platform_id
        if platform == "instagram":
            credentials["page_id"] = account.webhook_target_id
            credentials["missing_scopes"] = account.missing_scopes
            credentials["messaging_access"] = account.messaging_access
        # The comment poll and the first-comment reconciliation both match our
        # own comments on handle as well as id: Instagram often returns a
        # comment's ``username`` without a ``from`` object, and an unrecognised
        # own comment lands in the inbox as an inbound message from ourselves.
        credentials["account_handle"] = account.account_handle

    # Whatever else a provider keeps per account (a Slack channel) comes from
    # the provider itself rather than from another branch above.
    provider_cls = PROVIDER_REGISTRY.get(platform)
    if provider_cls is not None and provider_cls.account_url_credential and account.instance_url:
        from apps.common.validators import is_safe_url

        # The address was checked when the account was connected; DNS can change since, so check it again.
        if is_safe_url(account.instance_url):
            credentials[provider_cls.account_url_credential] = account.instance_url
        else:
            logger.warning("%s site URL failed SSRF check for account %s", platform, account.id)
    if provider_cls is not None:
        credentials.update(provider_cls.credentials_for_account(account))

    return credentials


MAX_RETRIES = 3

# States that legitimately explain a lost race in ``_fail_permanently``: the row
# was settled by someone else, or put back for another attempt. Anything outside
# this set means the caller was not mid-attempt, which is a bug worth a warning.
TERMINAL_OR_REQUEUED_STATUSES = (
    PlatformPost.Status.FAILED,
    PlatformPost.Status.PUBLISHED,
    PlatformPost.Status.SCHEDULED,
    PlatformPost.Status.ON_HOLD,
)

# How long a row may sit in ``publishing`` before ``confirm_pending_publishes``
# calls it. STALE_PUBLISHING covers a row with no platform handle — the worker
# died between the status flip and the upload, and nothing will ever finish it.
# PUBLISH_CONFIRM covers a row the platform accepted and is still transcoding.
# Read at call time, not import time: the module-level ``getattr(settings, ...)``
# used elsewhere in this file binds once, which makes the value untunable by
# ``override_settings`` and quietly turns any test that tries into a no-op.
DEFAULT_STALE_PUBLISHING_TIMEOUT = 900
DEFAULT_PUBLISH_CONFIRM_TIMEOUT = 1800
# Much longer than the two above, and deliberately so: this one covers "we
# cannot reach the platform to ask", where the post may already be live. Every
# extra minute spent retrying is a minute we might still learn the real answer,
# and the alternative is warning a user about a duplicate we could have ruled
# out. Long enough to ride out a platform outage or a token refresh.
DEFAULT_PUBLISH_UNCONFIRMED_TIMEOUT = 6 * 3600


def _stale_publishing_timeout() -> int:
    return int(getattr(settings, "PUBLISHER_STALE_PUBLISHING_TIMEOUT", DEFAULT_STALE_PUBLISHING_TIMEOUT))


def _publish_confirm_timeout() -> int:
    return int(getattr(settings, "PUBLISHER_PUBLISH_CONFIRM_TIMEOUT", DEFAULT_PUBLISH_CONFIRM_TIMEOUT))


def _publish_unconfirmed_timeout() -> int:
    return int(getattr(settings, "PUBLISHER_UNCONFIRMED_TIMEOUT", DEFAULT_PUBLISH_UNCONFIRMED_TIMEOUT))


# Row cap on the due query — how many PlatformPosts one cycle will look at. Not
# a connection cost: the query runs on the worker's own connection.
DEFAULT_MAX_CONCURRENT_PUBLISHES = 10

# The two below ARE a database budget, not a throughput knob. Django connections
# are thread-local, so every thread this engine spawns opens its own Postgres
# connection: POSTS group threads plus PLATFORM_PUBLISHES publish threads, on
# top of the web dyno's gunicorn threads, against the 20 the whole ROLE gets on
# heroku-postgresql:essential-0. Exceed it and every dyno starts failing to
# connect, which is what took production down on 2026-09-15. Raise these only
# alongside the Postgres plan.
#
# The budget covers the fan-out only. ``_process_retries`` and
# ``confirm_pending_publishes`` also reach providers, but both run sequentially
# on the worker's own single connection, so they cost one between them rather
# than one apiece. Parallelising either means routing it through this budget.
DEFAULT_MAX_CONCURRENT_POSTS = 4
DEFAULT_MAX_CONCURRENT_PLATFORM_PUBLISHES = 6


def _bounded_setting(name: str, default: int) -> int:
    """Read a concurrency ceiling from settings, floored at 1.

    Read at call time for the same reason as the timeouts above, and floored
    because both ceilings size a ``ThreadPoolExecutor``: 0 or a negative is a
    ``ValueError`` at construction, and a 0-sized bound that did construct would
    wedge the cycle forever rather than pause it. Pausing publishing is
    ``heroku ps:scale worker=0``, not a 0 here.
    """
    return max(1, int(getattr(settings, name, default)))


def _max_concurrent_publishes() -> int:
    return _bounded_setting("PUBLISHER_MAX_CONCURRENT_PUBLISHES", DEFAULT_MAX_CONCURRENT_PUBLISHES)


def _max_concurrent_posts() -> int:
    return _bounded_setting("PUBLISHER_MAX_CONCURRENT_POSTS", DEFAULT_MAX_CONCURRENT_POSTS)


def _max_concurrent_platform_publishes() -> int:
    return _bounded_setting("PUBLISHER_MAX_CONCURRENT_PLATFORM_PUBLISHES", DEFAULT_MAX_CONCURRENT_PLATFORM_PUBLISHES)


# First comments retry on their own schedule, separate from the publish retry:
# the post has already gone out, so there is no double-post risk and no reason
# to hurry. Deliberately bounded — django-background-tasks' own retry default is
# 25 attempts, which is not a thing to do to a live Page.
FIRST_COMMENT_MAX_RETRIES = getattr(settings, "PUBLISHER_FIRST_COMMENT_MAX_RETRIES", 3)
FIRST_COMMENT_RETRY_BACKOFF = [120, 600, 1800]  # 2min, 10min, 30min
FirstCommentStatus = PlatformPost.FirstCommentStatus


class _SharedMediaCache:
    """Materialize each of a post's attachments to local disk at most once.

    ``_publish_post_group`` has always claimed to share the media download
    across a post's platforms; it never did — the download lived inside
    ``_dispatch_to_provider``, which runs once per platform, in parallel
    threads. A post going to five platforms downloaded the same video five
    times at once. This is the shared cache that claim assumed.

    Not thread-safe by accident: the platform threads all reach for the same
    asset at the same moment, so the lock is what makes "at most once" true
    rather than "usually once".
    """

    class _Entry:
        """One asset's download slot: its own lock, and the path once it exists."""

        __slots__ = ("lock", "path")

        def __init__(self):
            self.lock = threading.Lock()
            self.path: str | None = None

    def __init__(self):
        self._entries: dict = {}
        # Guards the entry map only. The download itself runs under the entry's
        # own lock so threads wanting *different* assets never wait on each
        # other — and, more importantly, one wedged download cannot block every
        # other platform in the group (the executor then joins on all of them,
        # which would stall the whole worker).
        self._lock = threading.Lock()

    def path_for(self, asset) -> str:
        """Return a local path holding ``asset``'s bytes, downloading if needed."""
        with self._lock:
            entry = self._entries.get(asset.id)
            if entry is None:
                entry = self._entries[asset.id] = self._Entry()

        with entry.lock:
            if entry.path is not None:
                return entry.path
            suffix = os.path.splitext(asset.filename)[1] or ".tmp"
            tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)  # noqa: SIM115
            tmp.close()
            try:
                download_to_path(asset.file, tmp.name)
            except Exception:
                with contextlib.suppress(OSError):
                    os.unlink(tmp.name)
                raise
            entry.path = tmp.name
            return entry.path

    def cleanup(self) -> None:
        with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
        for entry in entries:
            # Take each entry's lock so we never unlink a file a straggling
            # thread is still writing into.
            with entry.lock:
                if entry.path:
                    with contextlib.suppress(OSError):
                        os.unlink(entry.path)
                    entry.path = None


def _first_comment_delay(workspace_id) -> int:
    """Seconds to wait after publishing before posting the first comment.

    Resolved per workspace at call time through the normal settings cascade
    (workspace → org → deployment default), so the
    ``publishing.first_comment_delay_seconds`` knob that already exists in the
    settings UI actually takes effect. ``PUBLISHER_FIRST_COMMENT_DELAY`` is
    passed in as the deployment default rather than read afterwards — the
    cascade's own APP_DEFAULTS floor would otherwise answer for every workspace
    without an override row, and the env var would never be consulted.
    """
    default = getattr(settings, "PUBLISHER_FIRST_COMMENT_DELAY", 120)
    try:
        from apps.settings_manager.helpers import get_setting

        value = get_setting(workspace_id, "publishing.first_comment_delay_seconds", default=default)
        return int(value) if value is not None else default
    except Exception:
        logger.warning("Could not resolve first_comment_delay_seconds; using %ss", default, exc_info=True)
        return default


#: How close to expiry a token has to be before the publish path spends a
#: refresh call on it. Generous because publishing is user-triggered and
#: infrequent. A loop that runs on a schedule needs a much tighter window —
#: see ``apps.analytics.tasks._ANALYTICS_REFRESH_WINDOW``.
_PUBLISH_REFRESH_WINDOW = timedelta(days=7)


def _provider_and_access_token(account):
    """Build the provider for ``account`` and return a usably-fresh token.

    Shared by the publish path and the first-comment task: the comment fires
    minutes after the publish, which is long enough for a token that was fine
    at publish time to have gone stale.

    Best-effort refresh — on failure we keep the old token and let the API call
    surface the real error rather than masking it with a refresh error.
    """
    provider = get_provider(account.platform, _resolve_publish_credentials(account))

    access_token = account.oauth_access_token
    if account.token_expires_within(_PUBLISH_REFRESH_WINDOW) and account.oauth_refresh_token:
        try:
            access_token = account.refresh_oauth_token(provider, enqueue_backfill=False)
            logger.info("Refreshed token for %s", account)
        except Exception:
            logger.exception("Token refresh failed for %s", account)

    return provider, access_token


class _ProviderUploadCache:
    """The :class:`providers.types.UploadCache` the engine hands to providers.

    Backed by the default cache (Redis in production, so it survives a worker
    restart). Best-effort both ways: a cache outage costs a re-upload, never
    a publish.
    """

    PREFIX = "provider-upload:"

    def get(self, key: str) -> str | None:
        try:
            return cache.get(self.PREFIX + key)
        except Exception:
            logger.warning("Provider upload cache read failed", exc_info=True)
            return None

    def set(self, key: str, value: str, timeout: int) -> None:
        try:
            cache.set(self.PREFIX + key, value, timeout)
        except Exception:
            logger.warning("Provider upload cache write failed", exc_info=True)


_provider_upload_cache = _ProviderUploadCache()


@contextlib.contextmanager
def _platform_executor(shared, group_size):
    """Yield the cycle's shared platform pool, or a private one for direct callers.

    A shared pool is not ours to shut down — other groups are still submitting
    to it — so it is yielded as-is and the ``with`` does nothing on exit.
    """
    if shared is not None:
        yield shared
        return

    with ThreadPoolExecutor(max_workers=min(group_size, _max_concurrent_platform_publishes())) as own:
        yield own


def _resolve_extra_asset(platform_post, asset_id, label):
    """Look up a media asset named in ``platform_extra`` (thumbnail, cover).

    The id was written from a composer form, so it is honoured only when the
    asset belongs to the post's workspace or its organization's shared
    library. Anything else would let one tenant publish another tenant's file,
    so it fails the platform post rather than publishing without it.

    Returns ``None`` when the asset no longer exists at all (deleted since the
    post was saved): that keeps the old behaviour of publishing without it.
    """
    from apps.media_library.models import MediaAsset

    try:
        asset_uuid = uuid.UUID(str(asset_id))
    except ValueError:
        logger.warning("%s asset id %r is not a valid id", label, asset_id)
        return None

    workspace = platform_post.post.workspace
    asset = (
        MediaAsset.objects.for_workspace_with_shared(workspace.id, workspace.organization_id)
        .filter(id=asset_uuid)
        .first()
    )
    if asset is not None:
        return asset
    # Existence only, never read: tells a deleted asset apart from one this
    # workspace may not use.
    if MediaAsset.objects.filter(id=asset_uuid).exists():
        raise PublishError(
            f"The {label.lower()} image is not in this workspace's media library.",
            platform=platform_post.social_account.get_platform_display(),
            retryable=False,
        )
    logger.warning("%s asset %s not found", label, asset_id)
    return None


def _public_media_url(asset, app_url: str) -> str:
    """The URL a provider fetches ``asset`` from: presigned, or absolute on local storage."""
    url = asset.file.url
    if url.startswith("/"):
        # Local storage: make absolute using APP_URL
        url = f"{app_url}{url}"
    return url


class PublishEngine:
    """Orchestrates the publishing of scheduled posts."""

    def poll_and_publish(self):
        """Main poll loop - find and publish due platform posts.

        Called every ~15 seconds by the background worker. Groups due
        PlatformPosts by parent Post and publishes each group.
        """
        due_pps = self._get_due_platform_posts()

        # Group by parent post_id
        groups: dict = {}
        for pp in due_pps:
            groups.setdefault(pp.post_id, []).append(pp)

        published_count = 0
        # Two pools, and the dependency runs one way: group threads submit
        # platform work and wait on it, platform threads wait on nothing. That
        # is what makes sharing one platform pool across all groups safe, and
        # sharing it is the whole point — its size IS the connection ceiling, so
        # a pool per group would multiply that ceiling by the number of groups.
        # Entering the platform pool first means it is torn down last, after
        # every group thread that might still be submitting to it.
        #
        # Guarded because min(len(groups), ...) is 0 with nothing due, and
        # ThreadPoolExecutor rejects a zero-sized pool outright.
        if groups:
            with (
                ThreadPoolExecutor(max_workers=_max_concurrent_platform_publishes()) as platform_pool,
                ThreadPoolExecutor(max_workers=min(len(groups), _max_concurrent_posts())) as group_pool,
            ):
                futures = {
                    group_pool.submit(
                        in_worker_thread, self._publish_post_group, pps[0].post, pps, platform_pool
                    ): post_id
                    for post_id, pps in groups.items()
                }
                for future in as_completed(futures):
                    post_id = futures[future]
                    try:
                        # A group that found every child held or already claimed
                        # publishes nothing; counting it would report success for
                        # a cycle that did none, which is the one line an operator
                        # reads to tell whether publishing recovered.
                        if future.result():
                            published_count += 1
                    except Exception:
                        logger.exception("Unexpected error publishing post group %s", post_id)

        # Always process retries, even when no new posts are due
        self._process_retries()

        return published_count

    def _get_due_platform_posts(self):
        """Find PlatformPosts due for publishing, using Coalesce fallback."""
        now = timezone.now()
        return list(
            PlatformPost.objects.filter(
                status=PlatformPost.Status.SCHEDULED,
            )
            .annotate(effective_at=Coalesce("scheduled_at", "post__scheduled_at"))
            .filter(effective_at__lte=now)
            # A row parked on a retry backoff is still SCHEDULED with a
            # scheduled_at in the past, so without this it came straight back on
            # the next 15s tick and the whole backoff schedule was dead code —
            # four attempts inside a minute instead of across half an hour. For
            # a video platform that also meant re-uploading the file four times.
            .exclude(retry_count__gt=0, next_retry_at__gt=now)
            # Never publish a post that has any platform on hold — a client hold
            # parks the whole post out of the publish path even if a sibling
            # platform is already scheduled.
            .exclude(post__platform_posts__status=PlatformPost.Status.ON_HOLD)
            # An autonomous post waits out its undo window, and stays put while
            # a person holds it or the workspace is in a crisis pause.
            .exclude(autonomy_hold_exclusion(now))
            # The example workspace holds sample drafts that must never go out.
            .exclude(post__workspace__sample_marker__isnull=False)
            # A paused account holds its scheduled posts: they stay scheduled and go out, overdue, on resume.
            .exclude(social_account__paused_at__isnull=False)
            .select_related("post__workspace", "social_account")
            .order_by("effective_at")[: _max_concurrent_publishes()]
        )

    def _publish_post_group(self, post, due_pps, platform_pool=None):
        """Publish a group of due PlatformPosts belonging to the same Post.

        Grouping is purely an operational optimization (shared media download,
        shared credential resolution). Status lives on the children — the
        parent Post is not touched.

        ``platform_pool`` is the cycle's shared platform executor, whose size is
        the connection budget. Callers outside the cycle (a direct "publish now",
        a test) pass nothing and get a private pool bounded by the same budget.

        Returns True when this group actually dispatched something, so the cycle
        can count published groups rather than completed futures.
        """
        # Lock and transition each due child from SCHEDULED → PUBLISHING.
        with transaction.atomic():
            # Lock ALL of this post's platform rows (not just the due scheduled
            # ones) so a concurrent client hold serializes against publishing:
            # request_hold()'s UPDATE on an approved sibling blocks on the locked
            # row until we commit, and we re-check on_hold here under the lock.
            locked = list(
                PlatformPost.objects.select_for_update()
                .filter(post_id=post.id)
                .select_related("social_account", "post__workspace")
            )

            if any(pp.status == PlatformPost.Status.ON_HOLD for pp in locked):
                return False

            due_ids = {pp.id for pp in due_pps}
            now = timezone.now()
            platform_posts = [
                pp
                for pp in locked
                if pp.id in due_ids
                and pp.status == PlatformPost.Status.SCHEDULED
                # Re-checked under the lock: an account paused since the query still holds its posts.
                and not pp.social_account.is_paused
                and not is_autonomy_held(pp, now)
                and (
                    (
                        pp.retry_count == 0
                        and (pp.scheduled_at or pp.post.scheduled_at)
                        and (pp.scheduled_at or pp.post.scheduled_at) <= now
                    )
                    # Re-checked under the lock: a stale snapshot must not
                    # bypass a backoff another worker has since set.
                    or (0 < pp.retry_count <= MAX_RETRIES and (pp.next_retry_at is None or pp.next_retry_at <= now))
                )
            ]

            # The autonomy policy is asked again at the moment of sending: a crisis pause or a
            # level lowered since an agent (or an agent's repeating series) queued this holds it
            # back as a draft for a person, rather than letting an old decision send it.
            for pp in list(platform_posts):
                reason = autonomy_send_blocker(pp)
                if reason:
                    from apps.composer.services import transition_platform_post

                    autonomy_hold_at_send(pp, reason)
                    transition_platform_post(pp, "draft")
                    platform_posts.remove(pp)

            if not platform_posts:
                return False

            # ``updated_at`` is explicit because queryset .update() bypasses
            # auto_now, and the confirmation sweep uses it to tell a publish
            # that is merely slow from one whose worker died mid-flight.
            PlatformPost.objects.filter(id__in=[pp.id for pp in platform_posts]).update(
                status=PlatformPost.Status.PUBLISHING,
                updated_at=now,
            )

        # The transition is committed, and what follows is minutes of media
        # download and platform I/O during which this thread asks the database
        # nothing. Holding a connection through that would cost one per
        # in-flight group for the duration of the slowest upload.
        release_idle_connection()

        # Publish in parallel, sharing one download of the post's media. Nothing
        # reads the per-platform return value — the loop below re-reads each row
        # from the DB — so a future's result is only consumed for its exception,
        # which must be logged rather than stashed in a dict no one inspects.
        media_cache = _SharedMediaCache()
        try:
            with _platform_executor(platform_pool, len(platform_posts)) as executor:
                futures = {
                    executor.submit(in_worker_thread, self._publish_platform_post, pp, media_cache=media_cache): pp
                    for pp in platform_posts
                }
                for future in as_completed(futures):
                    pp = futures[future]
                    try:
                        future.result()
                    except Exception:
                        logger.exception("Unexpected error publishing PlatformPost %s", pp.id)
        finally:
            media_cache.cleanup()

        # Reflect the aggregate onto Post.published_at so dashboards that
        # display "last published" don't need to query every child.
        self._sync_parent_published_at(post)

        # Schedule first comments for successful publishes (non-blocking). One
        # query for the group rather than a refresh_from_db() per child, which
        # lands right after the reconnect the release above forces.
        refreshed = PlatformPost.objects.filter(id__in=[pp.id for pp in platform_posts]).select_related(
            "social_account", "post"
        )
        for pp in refreshed:
            self._maybe_schedule_first_comment(pp)
            self._maybe_start_thread(pp)

        return True

    def _maybe_schedule_first_comment(self, platform_post):
        """Queue the first comment for a freshly published post, once.

        Called from both publish paths — a post that only succeeds on retry
        used to get no first comment at all. The PENDING guard keeps a second
        call from queueing a duplicate; the POSTED check inside the task is the
        real backstop against double-commenting.
        """
        if platform_post.status != PlatformPost.Status.PUBLISHED:
            return
        if not platform_post.social_account.supports_first_comment():
            return
        if not platform_post.effective_first_comment:
            return
        if platform_post.first_comment_status in (
            PlatformPost.FirstCommentStatus.PENDING,
            PlatformPost.FirstCommentStatus.POSTED,
        ):
            return

        # Enqueue before marking PENDING: @background writes its Task row
        # synchronously, so a crash between the two leaves a task that will
        # still run (and is idempotent) rather than a row stuck on PENDING
        # with nothing queued to clear it.
        delay = _first_comment_delay(platform_post.post.workspace_id)
        _post_first_comment_task(str(platform_post.id), schedule=delay)
        PlatformPost.objects.filter(pk=platform_post.pk).update(
            first_comment_status=PlatformPost.FirstCommentStatus.PENDING
        )

    def _maybe_start_thread(self, platform_post):
        """Queue the second part of a freshly published thread, once.

        Called from the same three places as ``_maybe_schedule_first_comment``.
        Each part then queues the next itself, after that part's delay, so a
        thread with an hour between parts holds no worker for that hour.
        """
        if platform_post.status != PlatformPost.Status.PUBLISHED:
            return
        if platform_post.thread_status != PlatformPost.ThreadStatus.NONE:
            return
        first = platform_post.post.thread_parts.order_by("position", "created_at").first()
        if first is None:
            return
        # Mark and enqueue in one transaction, as ``resume_thread`` does: the
        # task row lives in the same database, so either the thread is marked
        # posting with its task queued or neither happened. The conditional
        # update makes a second caller (the retry and confirmation paths can
        # both get here) queue nothing.
        with transaction.atomic():
            started = PlatformPost.objects.filter(
                pk=platform_post.pk, thread_status=PlatformPost.ThreadStatus.NONE
            ).update(thread_status=PlatformPost.ThreadStatus.PENDING, thread_error="")
            if started:
                _publish_thread_part_task(str(platform_post.id), str(first.id), schedule=first.delay_seconds)
        if started:
            platform_post.thread_status = PlatformPost.ThreadStatus.PENDING

    def reap_stalled_thread_parts(self):
        """Settle thread parts whose worker died while posting them.

        A part is claimed into ``posting`` just before the platform is called,
        so one still there long after was in flight when the worker stopped.
        Whether it went out is unknown, so it is never re-sent: it becomes
        ``unconfirmed`` and the thread partial, which shows the author where it
        stopped and lets them resume once they have checked.
        """
        stale_before = timezone.now() - timedelta(seconds=_stale_publishing_timeout())
        stalled = PlatformPostPart.objects.filter(
            status=PlatformPostPart.Status.POSTING, updated_at__lt=stale_before
        ).select_related("platform_post__social_account", "platform_post__post__author", "part")
        settled = 0
        for state in stalled:
            claimed = PlatformPostPart.objects.filter(pk=state.pk, status=PlatformPostPart.Status.POSTING).update(
                status=PlatformPostPart.Status.UNCONFIRMED,
                publish_error=THREAD_PART_AMBIGUOUS_MESSAGE,
                updated_at=timezone.now(),
            )
            if claimed:
                _stop_thread(state.platform_post, state.part, THREAD_PART_AMBIGUOUS_MESSAGE)
                settled += 1
        return settled

    def _publish_platform_post(self, platform_post, media_cache=None):
        """Publish a single PlatformPost to its target platform.

        Returns dict: {"success": bool, "platform_post_id": str, "error": str}
        """
        start_time = time.monotonic()
        account = platform_post.social_account

        # Final approval chokepoint. Status is not proof: another route or a
        # direct DB update could accidentally place unreviewed content in the
        # scheduled/publishing state. Approval-required workspaces must have a
        # fingerprint of the exact payload that received final approval.
        if not approval_allows_publish(platform_post):
            self._fail_permanently(
                platform_post,
                "approval proof missing or content changed after approval",
                user_message="This post was not published because its current content has not completed approval.",
                reason="approval proof failed at publish chokepoint",
            )
            return {"success": False, "error": "approval required"}

        # Media a platform's rules say it will refuse (a Reel over its length,
        # an image outside the feed's shapes) fails here with the sentence the
        # composer shows, rather than costing an upload the platform rejects.
        # Only blocking rules count; advisory ones never stop a post. Scheduling
        # checks this too — this catches media changed or a row scheduled
        # some other way since.
        media_reason = media_rules_blocker(
            account, post_media_assets(platform_post.post), platform_extra=platform_post.platform_extra
        )
        if media_reason:
            self._fail_permanently(
                platform_post,
                f"media rules: {media_reason}",
                user_message=media_reason,
                reason="media breaks a blocking platform media rule",
            )
            return {"success": False, "error": media_reason}

        # A thread the platform can't carry fails before its first part goes
        # out — publishing that alone would strand every part after it.
        thread_reason = platform_post_thread_blocker(platform_post)
        if thread_reason:
            self._fail_permanently(
                platform_post,
                f"thread: {thread_reason}",
                user_message=thread_reason,
                reason="thread the platform can't publish",
            )
            return {"success": False, "error": thread_reason}

        # Check rate limits
        rate_state = RateLimitState.objects.filter(
            social_account=account,
            platform=account.platform,
        ).first()

        if rate_state and rate_state.is_rate_limited:
            error_msg = f"Rate limited until {rate_state.window_resets_at}"
            # Our own sentence, not a provider body — but routed through the
            # same parameter so publish_error has exactly one writer.
            self._schedule_retry(platform_post, error_msg, user_message=PUBLISH_RATE_LIMIT_MESSAGE)
            return {"success": False, "error": error_msg}

        remote_succeeded = False
        try:
            # Get the provider for this platform
            result = self._dispatch_to_provider(platform_post, media_cache=media_cache)

            duration_ms = int((time.monotonic() - start_time) * 1000)

            if result["success"]:
                remote_succeeded = True
                platform_post.platform_post_id = result.get("platform_post_id", "")
                response_extra = result.get("response")
                if isinstance(response_extra, dict) and response_extra:
                    platform_post.platform_extra = {
                        **(platform_post.platform_extra or {}),
                        **response_extra,
                    }
                # Captured before the reset below, or every success would log
                # as attempt 1 and the retry history would vanish.
                attempt_number = platform_post.retry_count + 1
                # A successful retry must not leave a stale backoff behind, or
                # the next schedule of this row would be gated on a past failure.
                platform_post.retry_count = 0
                platform_post.next_retry_at = None
                platform_post.publish_error = ""

                if result.get("async_publish"):
                    # The platform only took delivery — it has not published
                    # anything yet. Hold the row in ``publishing`` and let
                    # ``confirm_pending_publishes`` settle it from the platform's
                    # own status endpoint, so a post that TikTok later rejects
                    # doesn't sit in the UI reading "Published".
                    #
                    # ``status`` is assigned explicitly and NOT taken for granted:
                    # this object was loaded before _publish_post_group flipped
                    # the status with a queryset .update(), so its in-memory
                    # ``status`` is still ``scheduled``. Saving without setting it
                    # wrote that stale value back, leaving the row due again and
                    # re-uploading the video on every 15s tick.
                    platform_post.status = PlatformPost.Status.PUBLISHING
                else:
                    platform_post.status = PlatformPost.Status.PUBLISHED
                    platform_post.published_at = timezone.now()
                    platform_post.permalink = str(result.get("url") or "")[:500] or None
                    # Read by the post.published webhook fired from save().
                    setattr(platform_post, webhook_events.PERMALINK_ATTR, result.get("url") or "")
                platform_post.save()

                if not result.get("async_publish"):
                    # A published post leaves the queue: drop the QueueEntry that
                    # held this channel's slot so the queue shows only upcoming posts
                    # and the slot frees up as a gap. Best-effort and isolated: the
                    # publish has already succeeded, so a cleanup failure here must
                    # NOT fall through to the `except` below (which would schedule a
                    # retry and double-post). The PlatformPost + published_at are kept.
                    self._drop_queue_entry(platform_post)

                # One writer for the log and the rate-limit state, whichever path
                # we took — duplicating this block is how the async copy drifted
                # into logging every attempt as number 1.
                PublishLog.objects.create(
                    platform_post=platform_post,
                    attempt_number=attempt_number,
                    status_code=result.get("status_code", 200),
                    response_body=str(result.get("response", ""))[:1000],
                    duration_ms=duration_ms,
                )
                self._update_rate_limit(account, result)

                return result
            else:
                error_msg = result.get("error", "Unknown publish error")
                duration_ms = int((time.monotonic() - start_time) * 1000)

                PublishLog.objects.create(
                    platform_post=platform_post,
                    attempt_number=platform_post.retry_count + 1,
                    status_code=result.get("status_code"),
                    response_body=str(result.get("response", ""))[:1000],
                    error_message=error_msg,
                    duration_ms=duration_ms,
                )

                # A provider that reports failure in the result dict rather than
                # raising gives us no exception to classify, and result["error"]
                # may well be a response body — so this one always gets the
                # generic sentence. The raw text is in the PublishLog row above.
                self._schedule_retry(platform_post, error_msg, user_message=PUBLISH_GENERIC_MESSAGE)
                return result

        except Exception as e:
            if remote_succeeded:
                # The external write already happened. Local persistence/logging
                # errors must never put this post back on the retry queue.
                logger.exception("Local recording failed after remote publish for %s", platform_post.id)
                try:
                    marked_failed = (
                        PlatformPost.objects.filter(pk=platform_post.pk)
                        .exclude(status=PlatformPost.Status.PUBLISHED)
                        .update(status=PlatformPost.Status.FAILED, publish_error=PUBLISH_AMBIGUOUS_MESSAGE)
                    )
                    if marked_failed:
                        # A queryset update reaches no save() hook, so the
                        # failure is reported here, as _fail_permanently does.
                        platform_post.status = PlatformPost.Status.FAILED
                        platform_post.publish_error = PUBLISH_AMBIGUOUS_MESSAGE
                        webhook_events.post_failed(platform_post)
                except Exception:
                    logger.exception(
                        "Could not record ambiguous outcome for %s; operator review required", platform_post.id
                    )
                return result
            duration_ms = int((time.monotonic() - start_time) * 1000)
            error_msg = str(e)

            PublishLog.objects.create(
                platform_post=platform_post,
                attempt_number=platform_post.retry_count + 1,
                error_message=error_msg,
                duration_ms=duration_ms,
            )

            user_message = friendly_publish_error(e)
            if isinstance(e, APIError) and getattr(e, "ambiguous_write", False):
                # A mutating request timed out or returned 5xx. The platform
                # may have accepted the post before the response was lost, so
                # blindly retrying can create a duplicate. Park it as failed
                # for explicit operator review instead.
                self._fail_permanently(
                    platform_post,
                    error_msg,
                    user_message=PUBLISH_AMBIGUOUS_MESSAGE,
                    reason="ambiguous remote write outcome",
                )
            elif getattr(e, "retryable", True) and not _refused_a_live_token(platform_post.social_account, e):
                self._schedule_retry(
                    platform_post,
                    error_msg,
                    user_message=user_message,
                    retry_at=getattr(e, "resets_at", None),
                )
            else:
                self._fail_permanently(platform_post, error_msg, user_message=user_message)
            return {"success": False, "error": error_msg}

    @staticmethod
    def _drop_queue_entry(platform_post):
        """Free the queue slot a now-published post was holding.

        Best-effort on purpose: the post is already live, so a cleanup failure
        must never propagate into a retry (which would double-post).
        """
        try:
            from apps.calendar.models import QueueEntry

            QueueEntry.objects.filter(
                post_id=platform_post.post_id,
                queue__social_account_id=platform_post.social_account_id,
            ).delete()
        except Exception:
            logger.warning(
                "Failed to drop QueueEntry for published PlatformPost %s",
                platform_post.id,
                exc_info=True,
            )

    def _dispatch_to_provider(self, platform_post, media_cache=None):
        """Dispatch to the appropriate platform provider.

        Resolves credentials, refreshes tokens if needed, builds a
        PublishContent payload, and calls provider.publish_post().
        Returns: {"success": bool, "platform_post_id": str, ...}

        ``media_cache`` is the post-level :class:`_SharedMediaCache`; when the
        caller supplies one, the attachment bytes are downloaded once for the
        whole post instead of once per platform, and the caller owns cleanup.
        """
        account = platform_post.social_account
        platform = account.platform
        attachments = list(platform_post.post.media_attachments.select_related("media_asset").order_by("position"))

        # Every scheduled row passes through here, however it was scheduled, so
        # this is where a post the platform can only refuse is stopped for
        # certain — before a token refresh or a download, and with the sentence
        # the scheduling paths show. They check first so the user hears it
        # sooner; this is what keeps a path that forgot from mattering.
        blocker = publish_blocker(
            account,
            media_types=usable_media_types(pm.media_asset for pm in attachments),
            platform_extra=platform_post.platform_extra,
        )
        if blocker:
            raise PublishError(blocker, platform=account.get_platform_display(), retryable=False)

        # Refreshes an expiring token first — covers OAuth2 providers *and*
        # session providers like Bluesky, whose accessJwt expires after only a
        # few hours and must be renewed before each publish.
        provider, access_token = _provider_and_access_token(account)

        # Download media from storage (S3/cloud) to temp files for upload
        # and collect public URLs (presigned R2 / absolute) for providers
        # that require fetchable URLs (Instagram, Threads, Google Business, etc.)
        media_files = []
        media_urls = []
        # Per-item media type, parallel to media_urls. Sniffed from magic bytes
        # at upload, so providers can route on what the file *is* rather than on
        # a storage-key extension copied from the client-declared filename.
        media_types = []
        temp_files = []
        # Owned locally only when the caller didn't hand us a post-level cache
        # (the retry path); ``owns_cache`` decides who cleans it up.
        owns_cache = media_cache is None
        if owns_cache:
            media_cache = _SharedMediaCache()

        # For video-only platforms (YouTube, TikTok), skip non-video attachments
        video_only = set(provider.supported_post_types) <= VIDEO_POST_TYPES
        if video_only:
            attachments = [pm for pm in attachments if pm.media_asset.media_type == "video"]

        # Providers that publish from a hosted URL never read ``media_files``,
        # so pulling the bytes down for them is pure cost — and on a small dyno
        # an expensive one. The loop below still runs for them: it is also what
        # collects media_urls, first_media_type and the video duration.
        needs_local_media = provider.needs_local_media

        first_media_type = None
        primary_video_duration = None
        usable_attachments = 0
        app_url = getattr(settings, "APP_URL", "").rstrip("/")
        try:
            for pm in attachments:
                asset = pm.media_asset
                if not asset.file:
                    continue
                usable_attachments += 1
                # Track the first media type for post type detection
                if first_media_type is None:
                    first_media_type = asset.media_type

                # Capture the first video's duration so providers can enforce
                # platform max-duration limits (e.g. TikTok max_video_post_duration_sec).
                if primary_video_duration is None and asset.media_type == "video":
                    primary_video_duration = asset.duration or None

                # Collect the public/presigned URL for this asset
                media_urls.append(_public_media_url(asset, app_url))
                media_types.append(asset.media_type)

                if needs_local_media:
                    media_files.append(media_cache.path_for(asset))

            # Merge per-platform extras (e.g. YouTube privacy_status, custom
            # tags, thumbnail) on top of the base extra dict.
            extra = {"tags": platform_post.post.tags or []}
            platform_extra = platform_post.platform_extra or {}
            extra.update(platform_extra)

            # Inject page_id for Facebook from the connected account.
            if platform == "facebook" and "page_id" not in extra:
                extra["page_id"] = account.account_platform_id

            # Inject Instagram user ID for Facebook-login Instagram accounts.
            if platform == "instagram" and "ig_user_id" not in extra:
                extra["ig_user_id"] = account.account_platform_id

            # Inject org author URN for LinkedIn Company Page.
            if platform == "linkedin_company" and "author" not in extra:
                extra["author"] = f"urn:li:organization:{account.account_platform_id}"

            # Pop link_url from extra and set on PublishContent directly
            link_url = extra.pop("link_url", None)

            # Resolve thumbnail_asset_id → temp file path for providers that
            # need to upload a custom thumbnail (YouTube).
            thumb_asset_id = extra.pop("thumbnail_asset_id", None)
            if thumb_asset_id:
                thumb_asset = _resolve_extra_asset(platform_post, thumb_asset_id, "Thumbnail")
                if thumb_asset is not None and thumb_asset.file:
                    suffix = os.path.splitext(thumb_asset.filename)[1] or ".jpg"
                    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)  # noqa: SIM115
                    tmp.close()
                    temp_files.append(tmp.name)
                    download_to_path(thumb_asset.file, tmp.name)
                    extra["thumbnail_file"] = tmp.name

            # Resolve cover_image_asset_id → temp file (Pinterest video pins)
            cover_asset_id = extra.pop("cover_image_asset_id", None)
            if cover_asset_id:
                cover_asset = _resolve_extra_asset(platform_post, cover_asset_id, "Cover")
                if cover_asset is not None and cover_asset.file:
                    suffix = os.path.splitext(cover_asset.filename)[1] or ".jpg"
                    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)  # noqa: SIM115
                    tmp.close()
                    temp_files.append(tmp.name)
                    download_to_path(cover_asset.file, tmp.name)
                    extra["cover_image_file"] = tmp.name

            post_type = self._resolve_post_type(
                platform=platform,
                platform_extra=platform_extra,
                # Count attachments, NOT downloaded files: media_files is empty
                # for URL-only providers, and counting it there would silently
                # demote every Instagram/Threads carousel to a single post.
                media_count=usable_attachments,
                first_media_type=first_media_type,
            )

            content = PublishContent(
                text=platform_post.effective_caption or "",
                title=platform_post.effective_title,
                description=platform_post.effective_caption,
                first_comment=platform_post.effective_first_comment,
                media_files=media_files,
                media_urls=media_urls,
                media_types=media_types,
                post_type=post_type,
                extra=extra,
                link_url=link_url,
                video_duration_sec=primary_video_duration,
                # Stable per PlatformPost, so every retry of this row sends the
                # same key; a clone or a new post is a new row and a new key.
                idempotency_key=f"brightbean-{platform_post.id}",
                upload_cache=_provider_upload_cache,
                should_stop=SHUTDOWN_REQUESTED.is_set,
            )

            logger.info(
                "Publishing to %s (account: %s, type: %s, media: %d)",
                platform,
                account.account_name,
                post_type.value,
                len(media_files),
            )
            try:
                result = provider.publish_post(access_token, content)
                return {
                    "success": True,
                    "platform_post_id": result.platform_post_id,
                    "url": result.url,
                    "response": result.extra,
                    # True when platform_post_id is a handle for work still in
                    # progress rather than a published post's id.
                    "async_publish": provider.publish_is_async,
                }
            except ProviderError:
                # Preserve explicit provider classifications (e.g. rejected 429,
                # expired token, known permanent error, or ambiguous API write).
                raise
            except Exception as exc:
                # Once inside publish_post, an unclassified error may follow a
                # committed mutation: JSON decoding, missing response IDs, or
                # even adapting a malformed success result. Retrying is unsafe.
                # Preparation errors above this boundary still use normal retry.
                raise APIError(
                    "The provider publish outcome could not be confirmed.",
                    platform=platform,
                    ambiguous_write=True,
                ) from exc
        finally:
            # Per-child temp files (thumbnail, cover) are ours either way. The
            # shared attachment cache is only ours to drop when we created it.
            for path in temp_files:
                with contextlib.suppress(OSError):
                    os.unlink(path)
            if owns_cache:
                media_cache.cleanup()

    @staticmethod
    def _hint_matches_media(hint: PostType, media_count: int, first_media_type: str | None) -> bool:
        """Whether the current attachments can still satisfy a media-shaped hint.

        The hint is written when the composer form is submitted, but attachments
        are added and removed by endpoints that persist on their own (composer
        remove_media, the media picker), so it can outlive the media it
        described. Trusting it then routes the post to an endpoint its
        attachments do not fit: a Reels or video endpoint handed an image, or a
        video endpoint handed nothing at all.

        Only hints that name a media shape are checked. TEXT, LINK, PIN and the
        rest say nothing about attachments and are left to their callers.
        """
        if hint is PostType.REEL:
            # Every Reels API takes exactly one video.
            return media_count == 1 and first_media_type == "video"
        if hint is PostType.STORY:
            # A Story is made of a photo or video; with none the hint has nothing to publish.
            return media_count >= 1
        if hint is PostType.VIDEO:
            # _publish_video posts media_urls[0] to the video endpoint as-is.
            return first_media_type == "video"
        return True

    @staticmethod
    def _resolve_post_type(
        platform: str,
        platform_extra: dict,
        media_count: int,
        first_media_type: str | None,
    ) -> PostType:
        """Derive the correct PostType from context.

        Priority:
        1. Explicit hint in platform_extra (validated against PostType enum,
           and against the media actually attached right now)
        2. Platform defaults (Pinterest → PIN)
        3. Multi-media on carousel-capable platforms → CAROUSEL
        4. Fallback: video → VIDEO (REEL on Instagram), image → IMAGE, else → TEXT
        """
        # 1. Explicit post_type hint from platform_extra
        hint = platform_extra.get("post_type")
        if hint:
            valid_values = {pt.value for pt in PostType}
            if hint not in valid_values:
                logger.warning("Invalid post_type hint %r, ignoring", hint)
            elif not PublishEngine._hint_matches_media(PostType(hint), media_count, first_media_type):
                logger.warning(
                    "Ignoring stale %s post_type hint: %d attachment(s), first is %r",
                    hint,
                    media_count,
                    first_media_type,
                )
            else:
                return PostType(hint)

        # 2. Platform defaults
        if platform == "pinterest":
            return PostType.PIN

        # 3. Multi-media → CAROUSEL for Instagram/Threads
        if media_count > 1 and platform in (
            "instagram",
            "instagram_login",
            "threads",
        ):
            return PostType.CAROUSEL

        # 4. Fallback based on first media type
        if first_media_type == "document":
            return PostType.DOCUMENT
        if first_media_type == "video":
            # Instagram has no standalone feed video — a lone video is a Reel.
            # Resolving that here keeps the platform rule next to the other
            # platform rules above, rather than leaving each Instagram provider
            # to translate PostType.VIDEO on its own (which instagram_login
            # failed to do, publishing the .mp4 as image_url).
            if platform in ("instagram", "instagram_login"):
                return PostType.REEL
            return PostType.VIDEO
        if first_media_type == "image":
            return PostType.IMAGE
        return PostType.TEXT

    def _fail_permanently(self, platform_post, error_msg, *, user_message, reason="non-retryable"):
        """Mark a post FAILED with no further retries.

        ``error_msg`` is the diagnostic and stays in the log line below and in
        the PublishLog row the caller already wrote. ``publish_error`` holds
        what the composer and the calendar render, so it must never carry a raw
        provider response body — which is why ``user_message`` has no default:
        every caller has to decide, and a forgotten one is a leak.
        """
        publish_error = (user_message or PUBLISH_GENERIC_MESSAGE)[:2000]

        # A targeted UPDATE guarded on the row still being ``publishing``, not
        # ``platform_post.save()``. Every one of the seven paths that reaches
        # here is mid-attempt on a row it claimed into ``publishing``: the
        # publish and retry paths set it before calling, and the confirmation
        # sweep only ever selects rows already in it. So ``publishing`` is not a
        # guess — it is the state this call is entitled to settle, and anything
        # else means somebody else got there first.
        #
        # Narrower than excluding the terminal states, and deliberately so.
        # ``scheduled`` has to be off limits too: a thread that fails slowly can
        # finish after ``_schedule_retry`` has already put the row back for
        # another attempt, and overwriting that would throw away a pending retry
        # and email the author a failure that had not happened yet. ``published``
        # matters for the opposite reason — ``VALID_TRANSITIONS`` gives it no
        # outgoing edges, so a post the platform confirmed is live must never be
        # walked back to ``failed``, told to the author as a failure, and offered
        # a retry that double-posts.
        #
        # A full save() would push the whole stale object back, and an in-memory
        # status check would not notice any of this, because the stale object
        # still says ``publishing``. The row count is the only trustworthy answer
        # to "did I settle this, or did someone else?", and it is what decides
        # whether the author is told.
        #
        # ``updated_at`` is explicit because .update() bypasses auto_now, and the
        # confirmation sweep reads it to tell a slow publish from a dead worker.
        settled_here = PlatformPost.objects.filter(
            id=platform_post.id,
            status=PlatformPost.Status.PUBLISHING,
        ).update(
            status=PlatformPost.Status.FAILED,
            publish_error=publish_error,
            updated_at=timezone.now(),
        )

        logger.warning(
            "PlatformPost %s failed (%s): %s",
            platform_post.id,
            reason,
            error_msg,
        )
        if not settled_here:
            # Someone else moved it on. Re-read rather than leaving the caller
            # holding an object that claims a status and an error the database
            # rejected.
            platform_post.refresh_from_db()
            if platform_post.status in TERMINAL_OR_REQUEUED_STATUSES:
                logger.info(
                    "PlatformPost %s is already %s; not notifying the author again",
                    platform_post.id,
                    platform_post.status,
                )
            else:
                # Not a race we know about — a caller reached here with a row
                # that was never claimed into ``publishing``, which would mean
                # a post silently never failing. Loud on purpose.
                logger.warning(
                    "PlatformPost %s could not be failed: expected 'publishing', found %r",
                    platform_post.id,
                    platform_post.status,
                )
            return

        platform_post.status = PlatformPost.Status.FAILED
        platform_post.publish_error = publish_error
        webhook_events.post_failed(platform_post)
        self._notify_publish_failed(platform_post)

    @staticmethod
    def _notify_publish_failed(platform_post):
        """Tell the post's author their post didn't go out.

        Until now a failed publish was silent unless the user happened to open
        the Publish page — which is how a broken TikTok integration went a full
        day without anyone knowing. Best-effort by construction: the post is
        already marked failed, and a notification problem must not unwind that.
        """
        try:
            from apps.notifications.engine import notify
            from apps.notifications.models import EventType

            author = platform_post.post.author
            if not author:
                return
            platform = platform_post.social_account.get_platform_display()
            notify(
                author,
                EventType.POST_FAILED,
                title=f"{platform} post failed to publish",
                body=platform_post.publish_error,
                data={
                    "post_id": str(platform_post.post_id),
                    "platform_post_id": str(platform_post.id),
                    "platform": platform_post.social_account.platform,
                },
            )
        except Exception:
            logger.exception(
                "Could not notify the author that PlatformPost %s failed",
                platform_post.id,
            )

    def _schedule_retry(self, platform_post, error_msg, *, user_message, retry_at=None):
        """Schedule a retry, honoring a provider's absolute reset time when given."""
        if platform_post.retry_count >= MAX_RETRIES:
            # A "We'll retry shortly" must not outlive the last retry, but a
            # specific message must not be swapped for a vaguer one either —
            # exhausted_publish_message keeps whichever is true.
            self._fail_permanently(
                platform_post,
                error_msg,
                user_message=exhausted_publish_message(user_message),
                reason=f"after {MAX_RETRIES} retries",
            )
            return

        now = timezone.now()
        if retry_at is not None:
            # YouTube's daily quota reset is an absolute Pacific-midnight
            # boundary. Retrying on the normal minute/5-minute/30-minute ladder
            # would exhaust MAX_RETRIES long before that boundary arrives.
            try:
                backoff_seconds = max(1, math.ceil((retry_at - now).total_seconds()))
                next_retry_at = max(retry_at, now + timedelta(seconds=1))
            except (AttributeError, TypeError):
                # A malformed provider value must not break the publish task;
                # fall back to the ordinary retry ladder.
                retry_at = None

        if retry_at is None:
            backoff_seconds = RETRY_BACKOFF[min(platform_post.retry_count, len(RETRY_BACKOFF) - 1)]
            next_retry_at = now + timedelta(seconds=backoff_seconds)

        platform_post.retry_count += 1
        platform_post.next_retry_at = next_retry_at
        # Drop back to SCHEDULED so the next _process_retries tick picks it up
        # once next_retry_at passes.
        platform_post.status = PlatformPost.Status.SCHEDULED
        platform_post.publish_error = (user_message or PUBLISH_GENERIC_MESSAGE)[:2000]
        platform_post.save()

        logger.info(
            "Scheduled retry %d for PlatformPost %s in %d seconds",
            platform_post.retry_count,
            platform_post.id,
            backoff_seconds,
        )

    def _process_retries(self):
        """Process platform posts that are due for retry."""
        now = timezone.now()
        # Mirror the primary due-query's hold guard: a retrying child must not
        # publish while any sibling is on_hold (the exclude covers the query
        # window; the per-row re-check below closes a hold placed after it).
        retry_posts = (
            PlatformPost.objects.filter(
                status=PlatformPost.Status.SCHEDULED,
                retry_count__gt=0,
                retry_count__lte=MAX_RETRIES,
                next_retry_at__lte=now,
            )
            .exclude(post__platform_posts__status=PlatformPost.Status.ON_HOLD)
            .exclude(autonomy_hold_exclusion(now))
            # A paused account holds a retrying post too; it retries on resume.
            .exclude(social_account__paused_at__isnull=False)
            .select_related("social_account", "post")
        )

        for pp in retry_posts:
            if pp.post.platform_posts.filter(status=PlatformPost.Status.ON_HOLD).exists():
                continue
            try:
                # Claim the row with a conditional UPDATE re-checking the same
                # predicate under the database's row lock, so a competing worker
                # (or a stale snapshot of a row whose backoff was since pushed
                # out) cannot dispatch the same retry twice.
                claimed = (
                    PlatformPost.objects.filter(
                        pk=pp.pk,
                        status=PlatformPost.Status.SCHEDULED,
                        retry_count=pp.retry_count,
                        next_retry_at__lte=timezone.now(),
                    )
                    # Re-checked at claim time: a crisis pause or hold placed since the query wins.
                    .exclude(autonomy_hold_exclusion(timezone.now()))
                    .update(status=PlatformPost.Status.PUBLISHING, updated_at=timezone.now())
                )
                if not claimed:
                    continue
                pp.status = PlatformPost.Status.PUBLISHING
                result = self._publish_platform_post(pp)
                if result.get("async_publish"):
                    # Left in ``publishing`` on purpose — the confirmation sweep
                    # owns it from here, including the first comment.
                    continue
                if result.get("success"):
                    self._sync_parent_published_at(pp.post)
                    self._maybe_schedule_first_comment(pp)
                    self._maybe_start_thread(pp)
            except Exception:
                logger.exception("Error retrying PlatformPost %s", pp.id)

    # ------------------------------------------------------------------
    # In-flight publishes
    # ------------------------------------------------------------------

    def confirm_pending_publishes(self):
        """Settle every PlatformPost sitting in ``publishing``.

        Two jobs, one query, because they are the same question asked of
        different rows: *is this publish still alive, and what happened to it?*

        1. **Confirmation.** A provider whose publish API is asynchronous
           (TikTok) has only handed the video over. We ask the platform for the
           real outcome and record it — which is why a video TikTok silently
           refused to process no longer reads "Published" forever.
        2. **Reaping.** Nothing else in the engine ever looks at a ``publishing``
           row again: both the due query and the retry query filter on
           ``scheduled``. So a worker killed mid-publish (a deploy, or the R15
           out-of-memory kill that prompted all this) stranded the row for good,
           in a status the UI renders read-only and un-selectable. Anything with
           no platform handle and no progress inside the timeout is failed here.

        Deliberately never re-publishes. We cannot tell "the platform never saw
        it" from "the platform took it and we died before recording that", and a
        duplicate video on a live account cannot be taken back. ``failed`` is
        editable and retryable in one click; a double post is forever.
        """
        now = timezone.now()
        stale_before = now - timedelta(seconds=_stale_publishing_timeout())
        confirm_before = now - timedelta(seconds=_publish_confirm_timeout())
        unconfirmed_before = now - timedelta(seconds=_publish_unconfirmed_timeout())

        # ``post__author`` is joined because _fail_permanently notifies the
        # author — without it every failure costs an extra query mid-sweep.
        pending = PlatformPost.objects.filter(status=PlatformPost.Status.PUBLISHING).select_related(
            "social_account", "post__workspace", "post__author"
        )

        settled = 0
        for pp in pending:
            try:
                if self._confirm_one_publish(
                    pp,
                    stale_before=stale_before,
                    confirm_before=confirm_before,
                    unconfirmed_before=unconfirmed_before,
                ):
                    settled += 1
            except Exception:
                logger.exception("Error confirming PlatformPost %s", pp.id)
        return settled

    def _confirm_one_publish(self, platform_post, *, stale_before, confirm_before, unconfirmed_before) -> bool:
        """Settle one in-flight row. Returns True when it reached a terminal status."""
        handle = platform_post.platform_post_id
        account = platform_post.social_account

        if not handle:
            # Never got as far as recording a platform handle. Nothing can
            # finish this row but us, and only once we're sure it isn't simply
            # still uploading.
            if platform_post.updated_at < stale_before:
                self._fail_permanently(
                    platform_post,
                    "Worker stopped mid-publish; no platform handle was recorded.",
                    user_message=PUBLISH_INTERRUPTED_MESSAGE,
                    reason="interrupted",
                )
                return True
            return False

        try:
            provider, access_token = _provider_and_access_token(account)
        except Exception:
            logger.exception("Could not build a provider to confirm PlatformPost %s", platform_post.id)
            return False

        if not provider.publish_is_async:
            # A synchronous provider has no status endpoint to ask, so we cannot
            # confirm anything. A handle is NOT proof this attempt succeeded: it
            # survives a transition back to ``scheduled``, so it may belong to an
            # earlier publish of the same row. Claiming success here would tell
            # the user a post went live when it never left the worker.
            if platform_post.updated_at < stale_before:
                self._fail_permanently(
                    platform_post,
                    f"{account.platform} cannot be asked about an in-flight publish.",
                    user_message=PUBLISH_INTERRUPTED_MESSAGE,
                    reason="interrupted",
                )
                return True
            return False

        try:
            status = provider.check_publish_status(access_token, handle)
        except NotImplementedError:
            # The provider declared its publish asynchronous but gave us no way
            # to confirm it, so we know strictly nothing about the outcome.
            # Treat it like an unreachable platform rather than a success.
            logger.warning(
                "%s declares publish_is_async but implements no check_publish_status",
                account.platform,
            )
            if platform_post.updated_at < unconfirmed_before:
                self._give_up_unconfirmed(
                    platform_post,
                    f"{account.platform} cannot report publish status.",
                )
                return True
            return False
        except Exception as exc:
            # We could not ASK. That is not an answer: the platform already
            # accepted the upload, so the post may well be live. Keep
            # reconciling on the (much longer) unconfirmed budget rather than
            # calling it failed after the processing timeout — a platform
            # outage or an expired token must not turn into a user being told
            # to publish a video that is already on their profile.
            logger.warning(
                "Could not read publish status for PlatformPost %s: %s",
                platform_post.id,
                exc,
            )
            if platform_post.updated_at < unconfirmed_before:
                self._give_up_unconfirmed(platform_post, f"Gave up confirming the publish: {exc}")
                return True
            return False

        if status.state is PublishState.COMPLETE:
            self._mark_confirmed_published(platform_post, status.platform_post_id or handle)
            return True

        if status.state in (PublishState.FAILED, PublishState.INBOX):
            self._fail_permanently(
                platform_post,
                status.error or f"{account.platform} reported {status.state.value}",
                # The platform's own sentence: these are written for the user
                # (which video setting to change, which app to open), not a
                # response body, so they are worth showing verbatim.
                user_message=status.error or PUBLISH_GENERIC_MESSAGE,
                reason=status.state.value,
            )
            return True

        # Still processing. Normal for the first minutes after an upload.
        if platform_post.updated_at < confirm_before:
            self._fail_permanently(
                platform_post,
                f"{account.platform} never finished processing the upload.",
                user_message=PUBLISH_CONFIRM_TIMEOUT_MESSAGE,
                reason="confirm timeout",
            )
            return True
        return False

    def _give_up_unconfirmed(self, platform_post, error_msg: str):
        """Stop reconciling a publish whose outcome we never learned.

        It still has to leave ``publishing`` — that status is a dead end in the
        UI, which is the bug this sweep exists to prevent — so ``failed`` is the
        only terminal state available. What must NOT happen is telling the user
        to publish again: the upload was accepted, so the post may be live, and
        a duplicate video cannot be taken back.

        ``platform_post_id`` is deliberately left on the row. It is the only
        handle that can reconcile this post later, and it is cleared only when
        the user explicitly re-schedules — the one moment it must not be reused.
        """
        self._fail_permanently(
            platform_post,
            error_msg,
            user_message=PUBLISH_UNCONFIRMED_MESSAGE,
            reason="unconfirmed",
        )

    def _mark_confirmed_published(self, platform_post, platform_post_id: str):
        """Finish a publish the platform has confirmed, exactly as the sync path does."""
        platform_post.platform_post_id = platform_post_id
        platform_post.status = PlatformPost.Status.PUBLISHED
        platform_post.published_at = timezone.now()
        platform_post.publish_error = ""
        platform_post.save()

        self._drop_queue_entry(platform_post)
        self._sync_parent_published_at(platform_post.post)
        self._maybe_schedule_first_comment(platform_post)
        self._maybe_start_thread(platform_post)
        logger.info("Confirmed publish of PlatformPost %s as %s", platform_post.id, platform_post_id)

    def _update_rate_limit(self, account, result):
        """Update rate limit state from API response headers."""
        remaining = result.get("rate_limit_remaining")
        resets_at = result.get("rate_limit_resets_at")

        if remaining is not None:
            RateLimitState.objects.update_or_create(
                social_account=account,
                platform=account.platform,
                defaults={
                    "requests_remaining": remaining,
                    "window_resets_at": resets_at,
                },
            )

    def _sync_parent_published_at(self, post):
        """Reflect the latest child published_at onto the parent Post.

        Status itself lives entirely on PlatformPost now (Post.status is a
        derived property), but we still maintain ``Post.published_at`` so
        dashboards/lists that show "last published" don't have to aggregate
        children at read time.
        """
        latest = max(
            (pp.published_at for pp in post.platform_posts.all() if pp.published_at),
            default=None,
        )
        if latest and post.published_at != latest:
            post.published_at = latest
            post.save(update_fields=["published_at", "updated_at"])


def _refused_a_live_token(account, exc) -> bool:
    """Whether the platform refused a token we know has not expired.

    No retry fixes that: the grant was revoked, or never covered what the post
    needs. Pinterest's missing boards:write was retried for an hour and a half
    before anyone was told. An expired token is different — the refresh before
    the next attempt may yet go through — and so is one whose expiry we never
    recorded, because some platforms' tokens expire without telling us when.
    Both keep their retries.
    """
    if not is_credential_rejection(exc):
        return False
    # Read the token back rather than trusting the copy this attempt loaded:
    # another process may have rotated it mid-publish, and then the refusal was
    # for a token nobody uses any more. The retry reloads the account and
    # publishes with the new one.
    from apps.social_accounts.models import SocialAccount

    # Best-effort: this runs inside the publish path's except block, so a
    # failed read must not escape it and strand the row in ``publishing``.
    # Not knowing is the case that keeps its retries.
    try:
        stored = (
            SocialAccount.objects.filter(pk=account.pk).values_list("oauth_access_token", "token_expires_at").first()
        )
    except Exception:
        logger.warning("Could not re-read the token for %s; keeping the retry", account.pk, exc_info=True)
        return False
    if stored is None:
        return False
    token, expires_at = stored
    if token != account.oauth_access_token:
        return False
    return expires_at is not None and expires_at > timezone.now()


def _is_ambiguous_submission_failure(exc) -> bool:
    """True when the comment may or may not have been created on the platform.

    A clean 4xx is the platform refusing the request outright, so nothing was
    created and retrying is safe. A timeout, a dropped connection, or a 5xx
    leaves it unknown — ``FacebookProvider.publish_comment`` already declines to
    retry a second target for this reason, and re-queueing the whole task would
    reintroduce exactly the double-comment it avoids.
    """
    if isinstance(exc, RateLimitError):
        # 429 is a rejection before the write, not an ambiguous outcome.
        return False
    status = getattr(exc, "status_code", None)
    return status is None or status >= 500


def _is_retryable_first_comment_failure(exc) -> bool:
    """Whether re-sending this exact comment could plausibly land differently.

    A provider that already called the error permanent is believed. Past that: a
    rate limit is a "not now", and a 5xx or a transport error is an unknown
    outcome (the caller gates those on reconciliation). A clean 4xx is the
    platform refusing *this request*, and re-sending it unchanged earns the same
    refusal — Instagram made that concrete by answering a comment POST carrying
    an unsupported ``fields`` param with a 400 while creating the comment
    anyway, so three retries left four comments on a live account.

    401 is the exception: ``_provider_and_access_token`` refreshes an expiring
    token between attempts, so the next attempt really is a different request.
    403 is not — Meta uses it for missing scopes and revoked grants, which need
    a reconnect rather than a backoff.
    """
    if not getattr(exc, "retryable", True):
        return False
    if isinstance(exc, RateLimitError):
        return True
    status = getattr(exc, "status_code", None)
    if status is None or status >= 500:
        return True
    return status == 401


def _can_reconcile_comments(provider) -> bool:
    """Whether this provider can check for an already-posted comment.

    The base implementation raises, so identity against it is what distinguishes
    a real lookup from the no-op default — same test as ``_supports_webhooks``.
    """
    from providers.base import SocialProvider

    implementation = getattr(type(provider), "find_own_comment", None)
    return implementation is not None and implementation is not SocialProvider.find_own_comment


def _mark_first_comment_posted(platform_post, comment_id: str) -> None:
    """Record a first comment that exists on the platform.

    Nothing here may raise: the comment is already live, and an escaping
    exception would let django-background-tasks re-run the task (its default is
    25 attempts) against a row still marked PENDING, posting a second one.
    """
    try:
        PlatformPost.objects.filter(pk=platform_post.pk).update(
            first_comment_status=FirstCommentStatus.POSTED,
            first_comment_id=str(comment_id or "")[:255],
            first_comment_error="",
            first_comment_posted_at=timezone.now(),
        )
    except Exception:
        logger.exception(
            "First comment for PlatformPost %s is POSTED on the platform but could not be recorded. "
            "The row still reads pending; do not re-run this task or it will comment twice.",
            platform_post.id,
        )


def _record_first_comment_failure(
    platform_post,
    error_msg: str,
    *,
    retryable: bool,
    user_message: str = "",
    retry_after: int | None = None,
    retry_at=None,
    unexpected: bool = False,
):
    """Persist a first-comment failure, and re-queue it when that can help.

    Every exit records state. The old version logged and returned, so a comment
    that 4xx'd left the post looking completely successful — the failure was
    only recoverable from worker logs.

    ``error_msg`` is the diagnostic: raw provider text, logged, never stored.
    ``user_message`` is what the composer and the calendar render, so it must
    stay free of response bodies — the Graph payload that motivated this split
    reached the UI as a wall of OAuthException and fbtrace noise. The log line
    below is the only place the raw text survives, so it always emits.
    """
    stored = (user_message or FIRST_COMMENT_GENERIC_MESSAGE)[:2000]
    exhausted = platform_post.first_comment_retry_count >= FIRST_COMMENT_MAX_RETRIES

    if not retryable or exhausted:
        reason = "non-retryable" if not retryable else f"after {FIRST_COMMENT_MAX_RETRIES} retries"
        # An unexpected exception is a bug in this code path, not a platform
        # rejection — log the stack, or it is indistinguishable from a Graph 4xx.
        log = logger.exception if unexpected else logger.warning
        log(
            "First comment for PlatformPost %s failed (%s): %s",
            platform_post.id,
            reason,
            error_msg,
        )
        PlatformPost.objects.filter(pk=platform_post.pk).update(
            first_comment_status=FirstCommentStatus.FAILED,
            first_comment_error=stored,
        )
        return

    if retry_at is not None:
        try:
            # The task queue accepts a delay, so convert the provider's absolute
            # reset boundary without rounding down and waking just before it.
            backoff = max(1, math.ceil((retry_at - timezone.now()).total_seconds()))
        except (AttributeError, TypeError):
            retry_at = None

    if retry_at is None:
        index = min(platform_post.first_comment_retry_count, len(FIRST_COMMENT_RETRY_BACKOFF) - 1)
        backoff = retry_after or FIRST_COMMENT_RETRY_BACKOFF[index]

    if unexpected:
        logger.exception("Unexpected error posting first comment for PlatformPost %s", platform_post.id)

    # F() so two tasks racing on the same row cannot both write the same count
    # and slip past FIRST_COMMENT_MAX_RETRIES.
    PlatformPost.objects.filter(pk=platform_post.pk).update(
        first_comment_status=FirstCommentStatus.PENDING,
        first_comment_error=stored,
        first_comment_retry_count=F("first_comment_retry_count") + 1,
    )
    _post_first_comment_task(str(platform_post.id), schedule=backoff)
    logger.info(
        "Retrying first comment for PlatformPost %s in %ss: %s",
        platform_post.id,
        backoff,
        error_msg,
    )


@background(schedule=0)
def _post_first_comment_task(platform_post_id):
    """Post the first comment as a background task (avoids blocking the publisher thread)."""
    try:
        platform_post = PlatformPost.objects.select_related("social_account__workspace__organization").get(
            pk=platform_post_id
        )
    except PlatformPost.DoesNotExist:
        logger.warning("PlatformPost %s not found for first comment.", platform_post_id)
        return

    # Idempotency: two publish paths and a retry queue can each land here for
    # the same row. Commenting twice on a live post is not recoverable.
    if platform_post.first_comment_status == FirstCommentStatus.POSTED:
        logger.info("First comment for PlatformPost %s already posted; skipping.", platform_post.id)
        return

    comment_text = platform_post.effective_first_comment
    if not comment_text:
        return

    if not platform_post.platform_post_id:
        _record_first_comment_failure(
            platform_post,
            "No platform post id recorded; nothing to comment on.",
            retryable=False,
            user_message="The post's id on the platform wasn't recorded, so the first comment couldn't be added.",
        )
        return

    account = platform_post.social_account
    try:
        provider, access_token = _provider_and_access_token(account)
    except Exception as exc:
        _record_first_comment_failure(
            platform_post,
            str(exc),
            retryable=getattr(exc, "retryable", True),
            user_message=friendly_first_comment_error(exc),
            retry_after=getattr(exc, "retry_after", None),
            retry_at=getattr(exc, "resets_at", None),
            unexpected=not isinstance(exc, ProviderError),
        )
        return

    # This is a retry, so a previous attempt may have created the comment before
    # failing (timeout, 5xx). Check before posting a second one.
    if platform_post.first_comment_retry_count:
        try:
            existing = provider.find_own_comment(access_token, platform_post.platform_post_id, comment_text)
        except NotImplementedError:
            existing = None
        except Exception as exc:
            # Whether the comment landed is unknown, so this attempt stops
            # without posting — but it must still be recorded and re-queued, or
            # the row sits PENDING forever with no task behind it and the post
            # reads as fully successful. The retry budget bounds it: a lookup
            # that never recovers ends FAILED and visible, never duplicated.
            logger.exception(
                "Could not check for an existing first comment on PlatformPost %s; skipping this attempt "
                "rather than risking a duplicate.",
                platform_post.id,
            )
            _record_first_comment_failure(
                platform_post,
                f"Could not check for an existing first comment: {exc}",
                retryable=True,
                user_message=friendly_first_comment_error(exc),
                retry_after=getattr(exc, "retry_after", None),
                retry_at=getattr(exc, "resets_at", None),
            )
            return
        if existing:
            logger.info("First comment for PlatformPost %s was already posted as %s", platform_post.id, existing)
            _mark_first_comment_posted(platform_post, existing)
            return

    try:
        result = provider.publish_comment(
            access_token=access_token,
            post_id=platform_post.platform_post_id,
            text=comment_text,
        )
    except NotImplementedError:
        logger.info("First comment not supported for %s", account.platform)
        return
    except Exception as exc:
        # An ambiguous failure is only safe to retry when we can first check
        # whether the comment landed; otherwise stop, because a duplicate
        # comment on a live post cannot be taken back.
        ambiguous = _is_ambiguous_submission_failure(exc)
        retryable = _is_retryable_first_comment_failure(exc)
        if ambiguous and not _can_reconcile_comments(provider):
            retryable = False
        _record_first_comment_failure(
            platform_post,
            str(exc),
            retryable=retryable,
            user_message=friendly_first_comment_error(exc),
            # RateLimitError is a sibling of APIError, not a subclass, so branch
            # on the attributes rather than on the exception type.
            retry_after=getattr(exc, "retry_after", None),
            retry_at=getattr(exc, "resets_at", None),
            # Provider errors are self-explanatory; anything else is a bug here
            # and needs the stack to be diagnosable.
            unexpected=not isinstance(exc, ProviderError),
        )
        return

    _mark_first_comment_posted(platform_post, getattr(result, "platform_comment_id", ""))
    logger.info("Posted first comment for PlatformPost %s", platform_post.id)


# ---------------------------------------------------------------------------
# Threads
# ---------------------------------------------------------------------------

# Each part retries on its own budget. The parts before it are live, so a
# failure here never touches them — it only decides whether this part gets
# another go or the thread stops for the author to resume.
THREAD_PART_MAX_RETRIES = getattr(settings, "PUBLISHER_THREAD_PART_MAX_RETRIES", 3)
THREAD_PART_RETRY_BACKOFF = [60, 300, 1800]  # 1min, 5min, 30min
PartStatus = PlatformPostPart.Status
ThreadStatus = PlatformPost.ThreadStatus

# Keys of ``platform_extra`` that describe the first post rather than the
# account, so a later part must not inherit them.
_FIRST_POST_ONLY_EXTRA = ("post_type", "link_url", "thumbnail_asset_id", "cover_image_asset_id")


def _can_reconcile_thread_parts(provider) -> bool:
    """Whether this provider can check for an already-published thread part.

    Same identity test as ``_can_reconcile_comments``: a provider answers when
    it overrides ``find_thread_part``, or — for a comments-mode thread, where
    the base implementation asks ``find_own_comment`` — when it can find its
    own comments.
    """
    from providers.base import SocialProvider
    from providers.types import ThreadMode

    implementation = getattr(type(provider), "find_thread_part", None)
    if implementation is not None and implementation is not SocialProvider.find_thread_part:
        return True
    return provider.thread.mode is ThreadMode.COMMENTS and _can_reconcile_comments(provider)


def _thread_part_states(platform_post, parts) -> dict:
    """The ``PlatformPostPart`` for each of ``parts`` on this account, created as needed."""
    states = {state.part_id: state for state in platform_post.part_states.all()}
    for part in parts:
        if part.id not in states:
            states[part.id], _ = PlatformPostPart.objects.get_or_create(platform_post=platform_post, part=part)
    return states


def _published_ref(state) -> PublishResult:
    """A published part as the ``parent`` the next part replies to."""
    return PublishResult(platform_post_id=state.platform_part_id, url=state.url or None, extra=state.response or {})


def _stop_thread(platform_post, part, user_message: str) -> None:
    """Mark the thread partial — the first post and some parts are live — and tell the author.

    Best-effort like ``_notify_publish_failed``: the part's own row already
    records what happened, and nothing here may raise into the task runner.
    """
    position = part.position + 1 if part is not None else None
    stored = (f"The thread stopped at part {position}. {user_message}" if position else user_message)[:2000]
    try:
        PlatformPost.objects.filter(pk=platform_post.pk).update(
            thread_status=ThreadStatus.PARTIAL,
            thread_error=stored,
        )
    except Exception:
        logger.exception("Could not record the stopped thread on PlatformPost %s", platform_post.id)
        return
    platform_post.thread_status = ThreadStatus.PARTIAL
    platform_post.thread_error = stored
    webhook_events.post_partially_published(platform_post)
    try:
        from apps.notifications.engine import notify
        from apps.notifications.models import EventType

        author = platform_post.post.author
        if not author:
            return
        platform = platform_post.social_account.get_platform_display()
        notify(
            author,
            EventType.POST_FAILED,
            title=f"{platform} thread was only partly published",
            body=stored,
            data={
                "post_id": str(platform_post.post_id),
                "platform_post_id": str(platform_post.id),
                "platform": platform_post.social_account.platform,
            },
        )
    except Exception:
        logger.exception("Could not notify the author that the thread on PlatformPost %s stopped", platform_post.id)


def _mark_thread_part_published(state, result) -> None:
    """Record a part that is live on the platform. Never raises, like ``_mark_first_comment_posted``."""
    try:
        PlatformPostPart.objects.filter(pk=state.pk).update(
            status=PartStatus.PUBLISHED,
            platform_part_id=str(result.platform_post_id or "")[:255],
            url=str(result.url or "")[:500],
            response=result.extra if isinstance(result.extra, dict) else {},
            publish_error="",
            published_at=timezone.now(),
            updated_at=timezone.now(),
        )
    except Exception:
        logger.exception(
            "Thread part %s is PUBLISHED on the platform but could not be recorded. "
            "It still reads posting; the stalled-part sweep will mark it unconfirmed rather than re-send it.",
            state.pk,
        )


def _continue_thread(platform_post, parts, index) -> None:
    """Queue the part after ``parts[index]``, after its delay, or finish the thread."""
    if index + 1 < len(parts):
        following = parts[index + 1]
        _publish_thread_part_task(str(platform_post.id), str(following.id), schedule=following.delay_seconds)
        return
    PlatformPost.objects.filter(pk=platform_post.pk).update(thread_status=ThreadStatus.COMPLETE, thread_error="")
    logger.info("Published every part of the thread on PlatformPost %s", platform_post.id)


def _hold_back_part(platform_post, state, part) -> None:
    """Park a part whose write may have landed as ``unconfirmed`` and stop the thread there."""
    PlatformPostPart.objects.filter(pk=state.pk).update(
        status=PartStatus.UNCONFIRMED,
        publish_error=THREAD_PART_AMBIGUOUS_MESSAGE,
        updated_at=timezone.now(),
    )
    _stop_thread(platform_post, part, THREAD_PART_AMBIGUOUS_MESSAGE)


def _record_thread_part_failure(platform_post, state, part, exc, *, ambiguous: bool, can_reconcile: bool) -> None:
    """Persist a part's failure, and re-queue it when another attempt can help.

    The same three outcomes as a publish: a retry on the backoff ladder, a
    permanent failure, or — when the write may have landed and the provider
    can't check — ``unconfirmed``, which is never re-sent on its own.
    """
    user_message = friendly_publish_error(exc)
    if ambiguous and not can_reconcile:
        logger.warning("Thread part %s outcome is unknown; stopping the thread: %s", state.pk, exc)
        _hold_back_part(platform_post, state, part)
        return

    retryable = ambiguous or _is_retryable_first_comment_failure(exc)
    if retryable and state.retry_count < THREAD_PART_MAX_RETRIES:
        retry_at = getattr(exc, "resets_at", None)
        backoff = None
        if retry_at is not None:
            try:
                backoff = max(1, math.ceil((retry_at - timezone.now()).total_seconds()))
            except (AttributeError, TypeError):
                backoff = None
        if backoff is None:
            index = min(state.retry_count, len(THREAD_PART_RETRY_BACKOFF) - 1)
            backoff = getattr(exc, "retry_after", None) or THREAD_PART_RETRY_BACKOFF[index]
        PlatformPostPart.objects.filter(pk=state.pk).update(
            status=PartStatus.PENDING,
            publish_error=user_message[:2000],
            retry_count=F("retry_count") + 1,
            updated_at=timezone.now(),
        )
        _publish_thread_part_task(str(platform_post.id), str(part.id), schedule=backoff)
        logger.info("Retrying thread part %s in %ss: %s", state.pk, backoff, exc)
        return

    if ambiguous:
        # Out of attempts while the outcome is still unknown: the same
        # held-back state as a write nobody can check.
        _hold_back_part(platform_post, state, part)
        return

    stored = exhausted_publish_message(user_message) if retryable else user_message
    logger.warning("Thread part %s failed: %s", state.pk, exc)
    PlatformPostPart.objects.filter(pk=state.pk).update(
        status=PartStatus.FAILED,
        publish_error=stored[:2000],
        updated_at=timezone.now(),
    )
    _stop_thread(platform_post, part, stored)


def _thread_part_content(platform_post, provider, part, caption, assets, media_cache) -> PublishContent:
    """The ``PublishContent`` for one later part, built like the first post's."""
    if not provider.thread.media_per_part:
        assets = []
    if set(provider.supported_post_types) <= VIDEO_POST_TYPES:
        assets = [asset for asset in assets if asset.media_type == "video"]
    app_url = getattr(settings, "APP_URL", "").rstrip("/")
    media_urls = [_public_media_url(asset, app_url) for asset in assets]
    media_files = [media_cache.path_for(asset) for asset in assets] if provider.needs_local_media else []
    extra = {"tags": platform_post.post.tags or []}
    extra.update(
        {key: value for key, value in (platform_post.platform_extra or {}).items() if key not in _FIRST_POST_ONLY_EXTRA}
    )
    first_type = assets[0].media_type if assets else None
    return PublishContent(
        text=caption or "",
        media_files=media_files,
        media_urls=media_urls,
        media_types=[asset.media_type for asset in assets],
        post_type=PublishEngine._resolve_post_type(
            platform=platform_post.social_account.platform,
            platform_extra={},
            media_count=len(assets),
            first_media_type=first_type,
        ),
        extra=extra,
        video_duration_sec=next((a.duration or None for a in assets if a.media_type == "video"), None),
        # One key per part per account, so a provider honouring idempotency
        # (Mastodon) can't publish the same part twice across retries.
        idempotency_key=f"brightbean-{platform_post.id}-part-{part.id}",
        upload_cache=_provider_upload_cache,
        should_stop=SHUTDOWN_REQUESTED.is_set,
    )


@background(schedule=0)
def _publish_thread_part_task(platform_post_id, part_id):
    """Publish one later part of a thread, then queue the next after its delay.

    One task per part rather than one loop per thread: a delay between parts
    is the next task's schedule, so no worker sleeps through it, and a part
    that fails retries on its own without re-running the parts before it.
    The part is claimed (``pending`` → ``posting``) before the platform is
    called, so a duplicate task for the same part does nothing.
    """
    platform_post = (
        PlatformPost.objects.select_related("social_account__workspace__organization", "post__author")
        .filter(pk=platform_post_id)
        .first()
    )
    if platform_post is None:
        logger.warning("PlatformPost %s not found for thread part %s.", platform_post_id, part_id)
        return
    parts_with_content = thread_parts_for(platform_post)
    parts = [part for part, _caption, _assets in parts_with_content]
    index = next((i for i, part in enumerate(parts) if str(part.id) == str(part_id)), None)
    if index is None:
        logger.info("Thread part %s is no longer on PlatformPost %s; nothing to publish.", part_id, platform_post.id)
        return
    part, caption, assets = parts_with_content[index]

    if platform_post.status != PlatformPost.Status.PUBLISHED or not platform_post.platform_post_id:
        _stop_thread(
            platform_post, part, "The first post's id on the platform wasn't recorded, so the thread couldn't continue."
        )
        return

    states = _thread_part_states(platform_post, parts)
    if any(states[earlier.id].status != PartStatus.PUBLISHED for earlier in parts[:index]):
        # Only the part right after the live ones may go out; anything else
        # is a stale task from before a resume.
        logger.info("Skipping thread part %s: an earlier part is not live.", part.id)
        return
    state = states[part.id]
    # A thread an automation started stops between parts when crisis pause is switched on or
    # its publishing level is lowered: the live parts stay, the rest wait for a person to resume.
    if state.status == PartStatus.PENDING and platform_post.social_account.is_paused:
        _stop_thread(
            platform_post,
            part,
            f"Publishing to {platform_post.social_account.display_name} is paused. Resume the thread after you "
            "resume the account.",
        )
        return
    reason = autonomy_send_blocker(platform_post) if state.status == PartStatus.PENDING else ""
    if reason:
        autonomy_hold_at_send(platform_post, reason, what="thread")
        _stop_thread(platform_post, part, f"It was paused because {reason}. Resume it when you're ready.")
        return
    claimed = PlatformPostPart.objects.filter(pk=state.pk, status=PartStatus.PENDING).update(
        status=PartStatus.POSTING, updated_at=timezone.now()
    )
    if not claimed:
        logger.info("Thread part %s is already %s; skipping.", state.pk, state.status)
        return

    # No ``extra`` for the root: ``platform_extra`` is the author's input, not
    # the platform's response, so a reply chain that needs more than the id
    # (Bluesky's cid) looks the root up itself.
    root = PublishResult(platform_post_id=platform_post.platform_post_id)
    parent = _published_ref(states[parts[index - 1].id]) if index else root

    try:
        provider, access_token = _provider_and_access_token(platform_post.social_account)
    except Exception as exc:
        _record_thread_part_failure(platform_post, state, part, exc, ambiguous=False, can_reconcile=False)
        return
    can_reconcile = _can_reconcile_thread_parts(provider)

    # A retry may follow an attempt that published before failing. Ask first.
    if state.retry_count and can_reconcile:
        try:
            existing = provider.find_thread_part(access_token, root=root, parent=parent, text=caption or "")
        except Exception as exc:
            logger.warning("Could not check for an existing thread part %s; not re-sending it yet.", state.pk)
            _record_thread_part_failure(platform_post, state, part, exc, ambiguous=True, can_reconcile=True)
            return
        if existing:
            logger.info("Thread part %s was already published as %s", state.pk, existing)
            _mark_thread_part_published(state, PublishResult(platform_post_id=existing))
            _continue_thread(platform_post, parts, index)
            return

    media_cache = _SharedMediaCache()
    try:
        try:
            content = _thread_part_content(platform_post, provider, part, caption, assets, media_cache)
        except Exception as exc:
            # Nothing was sent yet, so this is an ordinary retryable failure.
            _record_thread_part_failure(platform_post, state, part, exc, ambiguous=False, can_reconcile=can_reconcile)
            return
        try:
            result = provider.publish_thread_part(access_token, content, root=root, parent=parent)
        except ProviderError as exc:
            _record_thread_part_failure(
                platform_post,
                state,
                part,
                exc,
                ambiguous=bool(getattr(exc, "ambiguous_write", False)),
                can_reconcile=can_reconcile,
            )
            return
        except Exception as exc:
            # As in _dispatch_to_provider: once inside the provider call, an
            # unclassified error may follow a committed write.
            logger.exception("Unexpected error publishing thread part %s", state.pk)
            _record_thread_part_failure(platform_post, state, part, exc, ambiguous=True, can_reconcile=can_reconcile)
            return
    finally:
        media_cache.cleanup()

    _mark_thread_part_published(state, result)
    logger.info("Published thread part %s on PlatformPost %s", part.id, platform_post.id)
    _continue_thread(platform_post, parts, index)


def resume_thread(platform_post, *, confirm_unconfirmed: bool = False) -> None:
    """Pick a partially published thread back up from the first part that isn't live.

    The parts already live are never touched. The stopped part is put back to
    ``pending`` with a fresh retry budget. An ``unconfirmed`` part may already
    be live, so re-sending it needs ``confirm_unconfirmed`` — the author
    saying they checked the platform and it isn't there.
    Raises ``ValueError`` when the thread isn't stopped part-way, or the
    stopped part is unconfirmed and that wasn't confirmed.
    """
    if platform_post.thread_status != ThreadStatus.PARTIAL:
        raise ValueError("This thread isn't stopped part-way, so there is nothing to resume.")
    parts = [part for part, _caption, _assets in thread_parts_for(platform_post)]
    states = _thread_part_states(platform_post, parts)
    stopped = next((part for part in parts if states[part.id].status != PartStatus.PUBLISHED), None)
    if stopped is not None and states[stopped.id].status == PartStatus.UNCONFIRMED and not confirm_unconfirmed:
        raise ValueError(
            f"Part {stopped.position + 1} may already be live on "
            f"{platform_post.social_account.get_platform_display()}. Check the thread there, then confirm "
            "it isn't posted before resuming — otherwise it would be published twice."
        )
    if stopped is None:
        PlatformPost.objects.filter(pk=platform_post.pk).update(thread_status=ThreadStatus.COMPLETE, thread_error="")
        return
    with transaction.atomic():
        claimed = PlatformPost.objects.filter(pk=platform_post.pk, thread_status=ThreadStatus.PARTIAL).update(
            thread_status=ThreadStatus.PENDING, thread_error=""
        )
        if not claimed:
            raise ValueError("This thread is already being resumed.")
        PlatformPostPart.objects.filter(pk=states[stopped.id].pk).exclude(status=PartStatus.POSTING).update(
            status=PartStatus.PENDING, retry_count=0, publish_error="", updated_at=timezone.now()
        )
        _publish_thread_part_task(str(platform_post.id), str(stopped.id), schedule=0)
    platform_post.thread_status = ThreadStatus.PENDING
    platform_post.thread_error = ""
