"""``/api/v1/repurpose/jobs/*`` — turn one source into threads, posts, carousels, scripts and more.

Thin adapters over the shared operations in :mod:`apps.api.ops`, the same ones
the MCP tools call. Creating a job reserves its AI requests up front and
answers 202 with the pending job; read it until ``succeeded``, then accept the
outputs you want. Accepting only ever creates drafts, ideas and hashtag sets.
Needs ``create_posts``; every account must be on the key's allowlist.
"""

from __future__ import annotations

import uuid

from ninja import Router

from apps.api import ops
from apps.api.routers.common import run_op
from apps.api.schemas import AcceptRepurposeOutputsRequest, CreateRepurposeJobRequest, RepurposeJobResponse

router = Router(tags=["repurpose"])


@router.post("/jobs", response={202: RepurposeJobResponse}, summary="Start a repurposing job")
def create_job(request, payload: CreateRepurposeJobRequest):
    job = run_op(
        request,
        ops.create_repurpose_job,
        source_type=payload.source_type,
        url=payload.url,
        text=payload.text,
        post_id=payload.post_id,
        media_asset_id=payload.media_asset_id,
        outputs=[o.model_dump() for o in payload.outputs],
        action="repurpose.create",
        is_write=True,
        target_id=lambda result: result.id,
        status_code=202,
        idempotent=True,
    )
    return 202, job


@router.get("/jobs/{job_id}", response=RepurposeJobResponse, summary="Read a job's status and outputs")
def get_job(request, job_id: uuid.UUID):
    return run_op(request, ops.get_repurpose_job, job_id, action="repurpose.read", is_write=False, target_id=job_id)


@router.post("/jobs/{job_id}/cancel", response=RepurposeJobResponse, summary="Cancel a job")
def cancel_job(request, job_id: uuid.UUID):
    return run_op(request, ops.cancel_repurpose_job, job_id, action="repurpose.cancel", is_write=True, target_id=job_id)


@router.post("/jobs/{job_id}/accept", response=RepurposeJobResponse, summary="Save chosen outputs as drafts")
def accept_outputs(request, job_id: uuid.UUID, payload: AcceptRepurposeOutputsRequest):
    return run_op(
        request,
        ops.accept_repurpose_outputs,
        job_id,
        output_ids=payload.output_ids,
        action="repurpose.accept",
        is_write=True,
        target_id=job_id,
    )
