"""The media-container flow both Instagram providers publish through.

Instagram publishing is create-a-container, wait for Meta to ingest the media,
then publish the container. The two providers differ only in the host and in how
they address the account, so the parts that carry the failure handling - how long
to wait, reusing a container across retries, the status-read quirk - live here
once instead of drifting apart in two copies.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from urllib.parse import urlsplit

from .exceptions import APIError, PublishError
from .meta_errors import container_status_error, is_read_after_write_quirk
from .types import PublishContent

CONTAINER_POLL_INTERVAL = 2  # seconds
# Meta ingests a photo in seconds but can take minutes over a Reel. Both are
# defaults for the INSTAGRAM_*_CONTAINER_POLL_TIMEOUT settings. The video wait
# has to stay inside PUBLISHER_STALE_PUBLISHING_TIMEOUT (15 minutes), after
# which the sweep treats the attempt as dead.
CONTAINER_POLL_TIMEOUT = 120  # seconds
VIDEO_CONTAINER_POLL_TIMEOUT = 600  # seconds

# A container lives 24 hours; a retry comes minutes later. Six hours matches how
# long the other providers keep an upload that is waiting to be attached.
CONTAINER_REUSE_TTL = 6 * 3600  # seconds

# How many status reads in a row may fail with Meta's code 100 / subcode 33
# before it is believed.
READ_QUIRK_TOLERANCE = 5

_DEAD_STATES = ("ERROR", "EXPIRED")


def _without_signature(value):
    """``value`` with a URL's query string dropped.

    Media URLs are presigned afresh for every attempt, so the signature must not
    decide whether two attempts are building the same container; the path (the
    storage key) does.
    """
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        return urlsplit(value)._replace(query="", fragment="").geturl()
    return value


def _setting(name: str, default: float) -> float:
    from django.conf import settings

    return float(getattr(settings, name, default))


class InstagramContainers:
    """Mixin: ``_prepare_container`` and ``_wait_for_container`` for an Instagram provider."""

    platform_name: str
    _request: Callable

    def _container_status_url(self, container_id: str) -> str:
        raise NotImplementedError

    @staticmethod
    def _container_poll_timeout(*, is_video: bool) -> float:
        if is_video:
            return _setting("INSTAGRAM_VIDEO_CONTAINER_POLL_TIMEOUT", VIDEO_CONTAINER_POLL_TIMEOUT)
        return _setting("INSTAGRAM_CONTAINER_POLL_TIMEOUT", CONTAINER_POLL_TIMEOUT)

    def _prepare_container(
        self,
        access_token: str,
        content: PublishContent,
        slot: str,
        payload: dict,
        create: Callable[[], str],
        *,
        is_video: bool,
    ) -> str:
        """A container for ``payload`` that is ready to publish, reusing an earlier attempt's.

        Creating a container costs an upload on Meta's side, and a Reel that
        outran the wait would otherwise be re-created and re-processed by every
        retry. The id is remembered per publish attempt chain and per payload, so
        an edited post never inherits a container built from its old content.
        ``slot`` tells the containers of one post apart (a carousel's children).
        """
        cache, key = content.upload_cache, self._container_cache_key(access_token, content, slot, payload)
        container_id = cache.get(key) if cache is not None and key is not None else None
        if container_id and self._container_is_reusable(access_token, container_id):
            self._wait_for_container(access_token, container_id, is_video=is_video, should_stop=content.should_stop)
            return container_id

        container_id = create()
        if cache is not None and key is not None:
            cache.set(key, container_id, CONTAINER_REUSE_TTL)
        self._wait_for_container(access_token, container_id, is_video=is_video, should_stop=content.should_stop)
        return container_id

    def _container_cache_key(self, access_token: str, content: PublishContent, slot: str, payload: dict) -> str | None:
        if not content.idempotency_key:
            return None
        account = hashlib.sha256(access_token.encode()).hexdigest()[:32]
        publish = hashlib.sha256(content.idempotency_key.encode()).hexdigest()[:32]
        stable = {k: _without_signature(v) for k, v in payload.items()}
        body = hashlib.sha256(json.dumps(stable, sort_keys=True, default=str).encode()).hexdigest()[:32]
        return f"{self.platform_name}-container:{account}:{publish}:{slot}:{body}"

    def _container_is_reusable(self, access_token: str, container_id: str) -> bool:
        """Whether a remembered container can still be waited on and published."""
        try:
            state = self._container_state(access_token, container_id)
        except APIError:
            # Includes code 100 / subcode 33: on a container old enough to have been remembered
            # that is "gone", not the read-after-create quirk, so build a fresh one.
            return False
        return state not in _DEAD_STATES

    def _container_state(self, access_token: str, container_id: str) -> str:
        data = self._request(
            "GET",
            self._container_status_url(container_id),
            access_token=access_token,
            params={"fields": "status_code,status"},
        ).json()
        return data.get("status_code", "") if isinstance(data, dict) else ""

    def _wait_for_container(
        self,
        access_token: str,
        container_id: str,
        *,
        is_video: bool = False,
        should_stop: Callable[[], bool] | None = None,
    ) -> None:
        """Poll container status until FINISHED or error."""
        quirks = 0

        def finished() -> bool:
            nonlocal quirks
            try:
                data = self._request(
                    "GET",
                    self._container_status_url(container_id),
                    access_token=access_token,
                    params={"fields": "status_code,status"},
                ).json()
            except APIError as exc:
                # Meta sometimes answers a read of a container it has only just
                # created with code 100 / subcode 33 ("object does not exist"):
                # its fault, gone on the next read, and not worth failing the post.
                quirks += 1
                if is_read_after_write_quirk(exc) and quirks <= READ_QUIRK_TOLERANCE:
                    return False
                raise
            quirks = 0
            status = data.get("status_code", "")
            if status in _DEAD_STATES:
                raise container_status_error(
                    str(data.get("status") or status), platform=self.platform_name, raw_response=data
                )
            return status == "FINISHED"

        timeout = self._container_poll_timeout(is_video=is_video)
        self._poll_until(  # type: ignore[attr-defined]
            finished,
            deadline=time.monotonic() + timeout,
            interval=CONTAINER_POLL_INTERVAL,
            should_stop=should_stop,
            on_timeout=lambda: PublishError(
                f"{self.platform_name} container processing timed out", platform=self.platform_name
            ),
        )
