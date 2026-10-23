"""``/api/v1/queues/*`` — publishing queues and adding a post to the next free slot.

A queue belongs to one social account, so only queues on the key's allowlisted
accounts are visible; anything else is a 404. Adding a post schedules it, so it
needs both ``create_posts`` and ``publish_directly``.
"""

from __future__ import annotations

import uuid

from ninja import Router

from apps.api import ops
from apps.api.routers.common import run_op
from apps.api.schemas import (
    AddToQueueRequest,
    DeletedResponse,
    QueueDetailResponse,
    QueueEntryResponse,
    QueuesListResponse,
)

router = Router(tags=["queues"])


@router.get("/", response=QueuesListResponse, summary="List queues")
def list_queues(request):
    return run_op(request, ops.list_queues, action="queues.list", is_write=False)


@router.get("/{queue_id}", response=QueueDetailResponse, summary="Read a queue with its entries")
def get_queue(request, queue_id: uuid.UUID):
    return run_op(request, ops.get_queue, queue_id, action="queues.read", is_write=False, target_id=queue_id)


@router.post(
    "/{queue_id}/entries",
    response={201: QueueEntryResponse},
    summary="Add a post to a queue",
    description=(
        "Schedules the post into the queue's next free posting slot (`priority: true` takes the earliest slot and "
        "moves queued posts later). Needs `create_posts` and `publish_directly`. 409 when the queue is full."
    ),
)
def add_entry(request, queue_id: uuid.UUID, payload: AddToQueueRequest):
    entry = run_op(
        request,
        ops.add_post_to_queue,
        queue_id,
        payload.post_id,
        payload.priority,
        action="queues.add_post",
        is_write=True,
        target_id=payload.post_id,
        status_code=201,
        idempotent=True,
    )
    return 201, entry


@router.delete(
    "/{queue_id}/entries/{entry_id}",
    response=DeletedResponse,
    summary="Remove a post from a queue",
    description="The post goes back to draft and its slot is left open. Needs `create_posts`.",
)
def remove_entry(request, queue_id: uuid.UUID, entry_id: uuid.UUID):
    return run_op(
        request,
        ops.remove_queue_entry,
        queue_id,
        entry_id,
        action="queues.remove_post",
        is_write=True,
        target_id=entry_id,
    )
