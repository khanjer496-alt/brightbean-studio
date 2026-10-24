"""``/api/v1/feeds/*`` — RSS auto-posting.

Follow a feed, then have its new items posted to chosen accounts as the link, as an AI-written post in the
brand voice, or with the article's image, to the next queue slot, after a delay, or as drafts. Only items
published after auto-posting is switched on are used. Thin adapters over :mod:`apps.api.ops`, shared with the
MCP tools. Reading needs ``create_posts``; everything else ``publish_directly``. Posts are made as the
"system" actor, so the autonomy policy, the approval workflow and the plan's limits apply, and AI use counts
toward the AI allowance.
"""

from __future__ import annotations

import uuid

from ninja import Router

from apps.api import ops
from apps.api.routers.common import run_op
from apps.api.schemas import (
    AddFeedRequest,
    CheckFeedResponse,
    DeletedResponse,
    FeedDetailResponse,
    FeedResponse,
    FeedsListResponse,
    UpdateFeedAutopostRequest,
)

router = Router(tags=["feeds"])


@router.get("/", response=FeedsListResponse, summary="List followed feeds and their auto-posting settings")
def list_feeds(request):
    return run_op(request, ops.list_feeds, action="feeds.list", is_write=False)


@router.get("/{feed_id}", response=FeedDetailResponse, summary="Read a feed with what happened to its items")
def get_feed(request, feed_id: uuid.UUID):
    return run_op(request, ops.get_feed, feed_id, action="feeds.read", is_write=False, target_id=feed_id)


@router.post("/", response={201: FeedResponse}, summary="Follow a feed")
def add_feed(request, payload: AddFeedRequest):
    feed = run_op(
        request, ops.add_feed, payload, action="feeds.add", is_write=True, target_id=lambda r: r.id, status_code=201
    )
    return 201, feed


@router.patch("/{feed_id}", response=FeedDetailResponse, summary="Switch auto-posting on or change it")
def update_feed(request, feed_id: uuid.UUID, payload: UpdateFeedAutopostRequest):
    return run_op(
        request, ops.update_feed_autopost, feed_id, payload, action="feeds.update", is_write=True, target_id=feed_id
    )


@router.post("/{feed_id}/check", response=CheckFeedResponse, summary="Read the feed now")
def check_feed(request, feed_id: uuid.UUID):
    return run_op(request, ops.check_feed, feed_id, action="feeds.check", is_write=True, target_id=feed_id)


@router.delete("/{feed_id}/autopost", response=DeletedResponse, summary="Stop auto-posting a feed")
def stop_feed(request, feed_id: uuid.UUID):
    return run_op(request, ops.stop_feed_autopost, feed_id, action="feeds.stop", is_write=True, target_id=feed_id)
