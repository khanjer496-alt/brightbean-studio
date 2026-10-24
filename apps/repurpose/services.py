"""The repurposing run: reserve, read the source, generate, review, accept.

``create_job`` is the one entry point for the web page, the Agent API and the
MCP tools. It checks the plan's allowance for *every* provider call the run
could make before it reads the source or queues anything; ``execute_job``
(a background task) then makes the calls, one unit at a time, so a cancel or
the token cap stops it between calls. Each call is logged in the AI audit log
(``AIRequestLog``, action ``repurpose_<kind>``) by the shared ``ai_writer`` runner.

Nothing here publishes. ``accept_outputs`` turns chosen outputs into draft
posts (one per account, thread parts included) or ideas, and submits drafts
to the approval workflow when the workspace has one.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.utils import timezone

from apps.ai_writer import services as ai
from apps.ai_writer.client import AIError
from apps.ai_writer.services import AIContext

from . import carousel, hooks, outputs, sources
from .models import RepurposeJob, RepurposeOutput

logger = logging.getLogger(__name__)

Kind = RepurposeOutput.Kind
MAX_ACTIVE_JOBS = 3
# Rough characters per token and prompt overhead, for refusing a run that can't fit the token cap.
CHARS_PER_TOKEN = 3
PROMPT_OVERHEAD_TOKENS = 1_200


class JobStateError(ValueError):
    """The job is in a state that doesn't allow this (already finished, nothing to accept)."""


# ---------------------------------------------------------------------------
# Creating a job
# ---------------------------------------------------------------------------


def create_job(
    workspace,
    user,
    *,
    read_source: Callable[[], sources.Source],
    requests: list[outputs.Request],
    origin: str = "web",
    actor=None,
) -> tuple[RepurposeJob, dict | None]:
    """Reserve the run's AI calls, read the source, and queue the job.

    ``read_source`` runs only after the reservation succeeds, so a run the plan
    can't afford never fetches a page. An agent ``actor`` goes through the
    autonomy policy for drafting first (see :mod:`.hooks`); the second value is
    the notice it gets back (None for a person). Raises ``ValueError`` (fix the
    request), ``PlanLimitError`` / ``AIError`` (allowance, no AI configured) and
    ``AutonomyRefusalError`` (the policy refuses; already logged).
    """
    from .tasks import execute_repurpose_job

    units, max_calls = outputs.plan(requests)
    if not units:
        raise ValueError("Choose at least one output.")
    if max_calls > settings.REPURPOSE_MAX_CALLS_PER_RUN:
        raise ValueError(
            f"That would need up to {max_calls} AI requests; one run is limited to "
            f"{settings.REPURPOSE_MAX_CALLS_PER_RUN}. Choose fewer outputs or accounts."
        )
    expire_stale_jobs(workspace)
    if (
        RepurposeJob.objects.filter(workspace=workspace, status__in=RepurposeJob.ACTIVE_STATUSES).count()
        >= MAX_ACTIVE_JOBS
    ):
        raise ValueError("Several repurposing runs are still going. Wait for one to finish, or cancel one.")

    accounts = list({a.id: a for r in requests for a in r.accounts}.values())
    gates = hooks.before_create(workspace, actor, accounts, origin=origin)

    ctx = AIContext(workspace=workspace, user=user, source=origin)
    run = ai._start(ctx, reserve=max_calls)  # the up-front reservation
    source = read_source()
    if source.pdf is None:
        _check_estimate(len(units), source.text)

    job = RepurposeJob(
        workspace=workspace,
        created_by=user if getattr(user, "is_authenticated", False) else None,
        source_type=source.type,
        source_url=source.url,
        source_label=source.label,
        source_text=source.text,
        source_post=source.post,
        outputs_requested=[r.as_json() for r in requests],
        target_account_ids=sorted({str(a.id) for r in requests for a in r.accounts}),
        origin=origin,
        model=run.config.provider.model,
        used_organization_key=run.config.uses_organization_key,
        calls_reserved=max_calls,
        token_cap=settings.REPURPOSE_MAX_TOKENS_PER_RUN,
    )
    if source.pdf is not None:
        # Parsed by the background run, never in the request.
        job.source_file.save(f"{job.id}.pdf", ContentFile(source.pdf), save=False)
    job.save()
    note = hooks.record_decisions(gates, source_text=job.source_name)
    transaction.on_commit(lambda: execute_repurpose_job(str(job.id)))
    return job, note


# A run makes at most REPURPOSE_MAX_CALLS_PER_RUN calls of a few seconds to a minute
# each; one still "running" after this lost its worker and would hold a slot forever.
STALE_AFTER = timedelta(minutes=45)


def expire_stale_jobs(workspace) -> None:
    RepurposeJob.objects.filter(
        workspace=workspace, status=RepurposeJob.Status.RUNNING, started_at__lt=timezone.now() - STALE_AFTER
    ).update(
        status=RepurposeJob.Status.FAILED,
        finished_at=timezone.now(),
        error="This run stopped unexpectedly. Outputs it wrote are kept; start it again for the rest.",
    )


def _check_estimate(unit_count: int, text: str) -> None:
    estimate = unit_count * (len(text) // CHARS_PER_TOKEN + PROMPT_OVERHEAD_TOKENS)
    if estimate > settings.REPURPOSE_MAX_TOKENS_PER_RUN:
        raise ValueError("That source is too long for that many outputs. Choose fewer outputs, or a shorter source.")


# ---------------------------------------------------------------------------
# Running it
# ---------------------------------------------------------------------------


def _cancel_requested(job_id) -> bool:
    return RepurposeJob.objects.filter(pk=job_id, cancel_requested=True).exists()


def execute_job(job_id) -> RepurposeJob | None:
    """Make the job's provider calls. A job that isn't queued (cancelled, already taken) is left alone."""
    claimed = RepurposeJob.objects.filter(pk=job_id, status=RepurposeJob.Status.QUEUED).update(
        status=RepurposeJob.Status.RUNNING, started_at=timezone.now()
    )
    if not claimed:
        return None
    job = RepurposeJob.objects.select_related("workspace__organization", "created_by").get(pk=job_id)
    try:
        _execute(job)
    except Exception:
        logger.exception("Repurpose job %s failed", job.pk)
        _finish(job, RepurposeJob.Status.FAILED, "Something went wrong while writing. Please try again.")
    return job


def _execute(job: RepurposeJob) -> None:
    workspace = job.workspace
    try:
        requests = outputs.parse_requests(workspace, job.outputs_requested)
    except ValueError as exc:
        return _finish(job, RepurposeJob.Status.FAILED, f"An account for this run is no longer available. {exc}")
    units, _max = outputs.plan(requests)
    if job.source_file:
        try:
            _read_pending_pdf(job, len(units))
        except ValueError as exc:
            return _finish(job, RepurposeJob.Status.FAILED, str(exc))
    ctx = AIContext(workspace=workspace, user=job.created_by, source=job.origin)
    try:
        run = ai._start(ctx, reserve=job.calls_reserved)
    except Exception as exc:  # PlanLimitError, AIError: the allowance went elsewhere since the job was queued
        return _finish(job, RepurposeJob.Status.FAILED, getattr(exc, "message", str(exc)))

    position, skipped, note = 0, 0, ""
    for unit in units:
        if _cancel_requested(job.pk):
            _save_usage(job, run)
            return _finish(job, RepurposeJob.Status.CANCELLED, "Cancelled.")
        if run.input_tokens + run.output_tokens >= job.token_cap:
            skipped += 1
            continue
        try:
            result = outputs.generate(ctx, run, job, unit)
        except AIError as exc:
            note = exc.message
            break
        position = _save_result(job, unit, result, position)
        _save_usage(job, run)
    _save_usage(job, run)
    if skipped:
        note = f"Stopped at this run's cost cap; {skipped} output(s) were not written."
    made = job.outputs.exists()
    _finish(job, RepurposeJob.Status.SUCCEEDED if made else RepurposeJob.Status.FAILED, note)


def _read_pending_pdf(job: RepurposeJob, unit_count: int) -> None:
    """Read the uploaded PDF the job holds (in a sandboxed process), then delete the upload."""
    try:
        with job.source_file.open("rb") as handle:
            data = sources.read_pdf(handle)
        source = sources.pdf_source(sources.pdf_text(data), job.source_label)
    finally:
        job.source_file.delete(save=False)
        job.save(update_fields=["source_file"])
    _check_estimate(unit_count, source.text)
    job.source_text = source.text
    job.save(update_fields=["source_text"])


def _save_usage(job: RepurposeJob, run: ai.AIRun) -> None:
    job.input_tokens, job.output_tokens, job.calls_made = run.input_tokens, run.output_tokens, run.calls
    job.save(update_fields=["input_tokens", "output_tokens", "calls_made"])


def _finish(job: RepurposeJob, status: str, error: str = "") -> None:
    job.status, job.error, job.finished_at = status, error, timezone.now()
    job.save(update_fields=["status", "error", "finished_at"])


def _save_result(job: RepurposeJob, unit: outputs.Unit, result: outputs.Result, position: int) -> int:
    document = _carousel_pdf(job, unit, result) if result.slides else None
    rows = []
    for fields in result.outputs:
        account = fields.get("social_account")
        warnings = list(fields.pop("warnings", []))
        row = RepurposeOutput(job=job, position=position, warnings=warnings, **fields)
        if document and account is not None and _document_carousel(account):
            row.document = document
        elif result.slides and account is not None and _document_carousel(account):
            row.warnings = [*warnings, "The carousel PDF couldn't be saved to the media library."]
        rows.append(row)
        position += 1
    RepurposeOutput.objects.bulk_create(rows)
    return position


def _document_carousel(account) -> bool:
    from providers import PROVIDER_REGISTRY

    return PROVIDER_REGISTRY[account.platform].document_carousel


def _carousel_pdf(job: RepurposeJob, unit: outputs.Unit, result: outputs.Result):
    """Render the slide texts as a branded PDF in the media library, when an account takes PDF carousels."""
    if not any(_document_carousel(a) for a in unit.request.accounts):
        return None
    from apps.media_library.quotas import StorageQuotaExceededError
    from apps.media_library.services import create_asset
    from apps.workspaces.brand_kit import resolve

    workspace = job.workspace
    try:
        data = carousel.render_carousel_pdf(
            result.slides, brand=resolve(workspace), title=job.source_label or "Carousel", author=workspace.name
        )
        upload = SimpleUploadedFile("carousel.pdf", data, content_type="application/pdf")
        return create_asset(
            workspace.organization,
            workspace,
            upload,
            job.created_by,
            alt_text="Carousel slides: " + " / ".join(s["title"] for s in result.slides if s["title"])[:500],
            title=f"Carousel: {job.source_label}"[:255],
            tags=["repurposed", "carousel"],
        )
    except (StorageQuotaExceededError, ValueError):
        logger.info("Carousel PDF for job %s was not saved", job.pk, exc_info=True)
        return None
    except Exception:
        logger.exception("Carousel PDF for job %s failed", job.pk)
        return None


# ---------------------------------------------------------------------------
# Cancelling
# ---------------------------------------------------------------------------


def cancel_job(job: RepurposeJob) -> RepurposeJob:
    """Stop a queued job at once, or a running one before its next provider call."""
    if not job.is_active:
        raise JobStateError("That run has already finished.")
    now = timezone.now()
    if RepurposeJob.objects.filter(pk=job.pk, status=RepurposeJob.Status.QUEUED).update(
        status=RepurposeJob.Status.CANCELLED, cancel_requested=True, finished_at=now, error="Cancelled."
    ):
        job.refresh_from_db()
        return job
    RepurposeJob.objects.filter(pk=job.pk, status=RepurposeJob.Status.RUNNING).update(cancel_requested=True)
    job.refresh_from_db()
    return job


# ---------------------------------------------------------------------------
# Accepting outputs
# ---------------------------------------------------------------------------


def attribution(job: RepurposeJob) -> str:
    """The note kept with every draft or idea made from ``job``."""
    where = job.source_url or job.source_name
    return (
        f"Repurposed from: {where}\nWritten by AI ({job.model or 'AI assistant'}); review before publishing.\n"
        f"Repurpose run {job.id}."
    )


def accept_outputs(job: RepurposeJob, output_ids, user, *, actor=None) -> tuple[list[RepurposeOutput], dict | None]:
    """Make drafts (outputs with an account) and ideas (the rest) from the chosen outputs. Never publishes.

    Safe to repeat: the job's outputs are locked while they're turned into
    drafts, so two clicks (or an agent's retry) can't make two copies; outputs
    already accepted are left as they are. An agent ``actor`` goes through the
    autonomy policy for drafting (:mod:`.hooks`); the second value is the
    notice it gets back.
    """
    wanted = {str(i) for i in output_ids}
    posts = []
    with transaction.atomic():
        rows = list(
            job.outputs.select_for_update(of=("self",))
            .select_related("social_account", "document")
            .filter(status__in=(RepurposeOutput.Status.GENERATED, RepurposeOutput.Status.ACCEPTED))
        )
        rows = [o for o in rows if str(o.id) in wanted]
        chosen = [o for o in rows if o.status == RepurposeOutput.Status.GENERATED]
        if not chosen:
            if rows:  # all of them were accepted already: a repeat, nothing to do
                return [], None
            raise JobStateError("There is nothing to accept: choose outputs that haven't been accepted or discarded.")
        gates = hooks.before_accept(job, chosen, actor)
        for out in chosen:
            if out.social_account_id:
                out.post = _make_draft(job, out, user)
                posts.append(out.post)
            elif out.kind == Kind.HASHTAGS:
                _make_hashtag_set(job, out, user)
            else:
                out.idea = _make_idea(job, out, user)
            out.status = RepurposeOutput.Status.ACCEPTED
            out.save(update_fields=["post", "idea", "status"])
        note = hooks.record_decisions(gates, source_text=job.source_name)
    _submit_for_review(job.workspace, posts, user)
    return chosen, note


def _make_draft(job: RepurposeJob, out: RepurposeOutput, user):
    from apps.composer.services import ThreadPartSpec, create_post
    from providers import PROVIDER_REGISTRY

    account = out.social_account
    notes = attribution(job)
    kwargs: dict = {}
    if out.kind == Kind.THREAD:
        kwargs["parts"] = [ThreadPartSpec(caption=p) for p in out.parts]
    elif out.kind == Kind.CAROUSEL:
        slides = "\n".join(f"{n}. {line}" for n, line in enumerate(out.parts, 1))
        notes += f"\n\nSlide texts:\n{slides}"
        if out.document_id:
            if PROVIDER_REGISTRY[account.platform].publishes_documents:
                kwargs["media_asset_ids"] = [out.document_id]
            else:
                notes += (
                    f"\n\nCarousel PDF: {out.document.filename} in the media library. "
                    f"{account.get_platform_display()} can't publish documents from here yet, so download it "
                    "and upload it on the network."
                )
    post = create_post(
        workspace=job.workspace,
        social_account=account,
        caption=out.text,
        title=out.title,
        internal_notes=notes,
        author=user if getattr(user, "is_authenticated", False) else None,
        status="draft",
        **kwargs,
    )
    return post


def _make_idea(job: RepurposeJob, out: RepurposeOutput, user):
    from apps.composer.models import Idea, IdeaGroup

    group = IdeaGroup.objects.for_workspace(job.workspace_id).order_by("position").first()
    label = dict(Kind.choices)[out.kind]
    title = out.title or out.text.splitlines()[0]
    return Idea.objects.create(
        workspace=job.workspace,
        author=user if getattr(user, "is_authenticated", False) else None,
        title=ai.truncate_to_limit(f"{label}: {title}", 255),
        description=f"{out.text}\n\n---\n{attribution(job)}",
        group=group,
        status=Idea.Status.UNASSIGNED,
    )


def _make_hashtag_set(job: RepurposeJob, out: RepurposeOutput, user):
    from apps.composer.models import HashtagSet

    return HashtagSet.objects.create(
        workspace=job.workspace,
        name=ai.truncate_to_limit(f"From {job.source_name}", 100),
        hashtags=list(out.parts),
        created_by=user if getattr(user, "is_authenticated", False) else None,
    )


def _submit_for_review(workspace, posts, user) -> None:
    """Put new drafts into the approval workflow when the workspace has one."""
    if not posts or not getattr(user, "is_authenticated", False):
        return
    if getattr(workspace, "approval_workflow_mode", "none") == "none":
        return
    from apps.approvals import services as approval_services

    for post in posts:
        try:
            approval_services.submit_for_review(post, user, workspace)
        except (ValueError, PermissionDenied):
            logger.info("Draft %s stays a draft: the person can't submit it for review", post.pk, exc_info=True)


def discard_output(job: RepurposeJob, output_id) -> None:
    updated = job.outputs.filter(pk=output_id, status=RepurposeOutput.Status.GENERATED).update(
        status=RepurposeOutput.Status.DISCARDED
    )
    if not updated:
        raise JobStateError("That output can't be discarded.")
