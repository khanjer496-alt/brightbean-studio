"""The Repurpose workspace page: start a run, watch it, review and accept its outputs.

Starting a run and accepting outputs need ``create_posts``. Every job is looked
up inside the workspace in the URL, so another workspace's job id is a 404.
Accepted outputs become drafts and ideas only; nothing here publishes.
"""

from __future__ import annotations

import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST

from apps.account_sets import services as set_services
from apps.ai_writer.client import AIError, ai_available
from apps.billing.entitlements import PlanLimitError
from apps.members.decorators import require_permission
from apps.social_accounts.models import SocialAccount

from . import outputs, services, sources
from .models import RepurposeJob, RepurposeOutput

logger = logging.getLogger(__name__)

RECENT_JOBS = 10
SOURCE_CHOICES = [
    ("url", "Web page", "Paste a link to a blog post or article."),
    ("text", "Pasted text", "Paste the text itself."),
    ("pdf", "PDF", "Upload a PDF (text only)."),
    ("post", "Published post", "Remix a post you already published."),
    ("video", "Video", "A video link; captions are read only for videos on your own connected channel."),
]


def _accounts(workspace):
    return list(
        SocialAccount.objects.for_workspace(workspace.id)
        .filter(connection_status=SocialAccount.ConnectionStatus.CONNECTED)
        .order_by("platform", "account_name")
    )


def _job(request, job_id) -> RepurposeJob:
    services.expire_stale_jobs(request.workspace)
    return get_object_or_404(RepurposeJob, id=job_id, workspace=request.workspace)


def _posts_for_remix(workspace):
    from apps.composer.models import PlatformPost, Post

    return (
        Post.objects.filter(workspace=workspace, platform_posts__status=PlatformPost.Status.PUBLISHED)
        .distinct()
        .order_by("-created_at")[:50]
    )


def _form_context(request, **extra):
    workspace = request.workspace
    accounts = _accounts(workspace)
    return {
        "workspace": workspace,
        "ai_enabled": ai_available(workspace.organization),
        "accounts": accounts,
        "kinds": [
            {"spec": spec, "accounts": [a for a in accounts if not spec.refusal(a)]} for spec in outputs.KINDS.values()
        ],
        "account_sets": set_services.sets_for(workspace),
        "source_choices": SOURCE_CHOICES,
        "remix_posts": _posts_for_remix(workspace),
        "jobs": RepurposeJob.objects.filter(workspace=workspace)[:RECENT_JOBS],
        "form": {},
        **extra,
    }


@login_required
@require_permission("create_posts")
@require_GET
def home(request, workspace_id):
    return render(request, "repurpose/home.html", _form_context(request))


def _requests_from_form(request):
    raw = []
    for kind in outputs.KINDS:
        if request.POST.get(f"kind_{kind}"):
            account_ids = request.POST.getlist(f"accounts_{kind}")
            # A saved set adds its connected accounts that can take this output.
            chosen = set_services.get_set(request.workspace, request.POST.get(f"set_{kind}", ""))
            if chosen is not None:
                account_ids += [
                    str(a.id) for a in set_services.usable_accounts(chosen) if not outputs.KINDS[kind].refusal(a)
                ]
            raw.append(
                {
                    "kind": kind,
                    "account_ids": list(dict.fromkeys(account_ids)),
                    "count": request.POST.get(f"count_{kind}") or 0,
                }
            )
    return outputs.parse_requests(request.workspace, raw)


def _reader(request):
    """A function that reads the source the form describes (run only after the AI calls are reserved)."""
    kind = request.POST.get("source_type", "")
    workspace = request.workspace
    if kind == "url":
        return lambda: sources.from_url(request.POST.get("source_url", ""))
    if kind == "text":
        return lambda: sources.from_text(request.POST.get("source_text", ""))
    if kind == "pdf":
        upload = request.FILES.get("source_pdf")
        if upload is None:
            raise ValueError("Choose a PDF to upload.")
        return lambda: sources.pending_pdf(upload, upload.name)
    if kind == "post":
        from apps.composer.models import Post

        post = Post.objects.filter(workspace=workspace, id=_uuid_or_none(request.POST.get("source_post"))).first()
        if post is None:
            raise ValueError("Choose one of your published posts.")
        return lambda: sources.from_post(post)
    if kind == "video":
        return lambda: sources.from_video(
            request.POST.get("source_video", ""),
            transcript=request.POST.get("source_transcript", ""),
            accounts=_accounts(workspace),
        )
    raise ValueError("Choose where the content comes from.")


def _uuid_or_none(value):
    import uuid

    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


@login_required
@require_permission("create_posts")
@require_POST
def job_create(request, workspace_id):
    workspace = request.workspace
    if not ai_available(workspace.organization):
        raise Http404
    try:
        job, _ = services.create_job(
            workspace, request.user, read_source=_reader(request), requests=_requests_from_form(request)
        )
    except sources.TranscriptNeededError as exc:
        return render(
            request,
            "repurpose/home.html",
            _form_context(request, form=request.POST, error=str(exc), needs_transcript=True),
            status=422,
        )
    except (ValueError, PlanLimitError, AIError) as exc:
        status = 402 if isinstance(exc, PlanLimitError) else 422
        return render(
            request,
            "repurpose/home.html",
            _form_context(request, form=request.POST, error=getattr(exc, "message", str(exc))),
            status=status,
        )
    return redirect("repurpose:detail", workspace_id=workspace.id, job_id=job.id)


def _detail_context(request, job):
    return {
        "workspace": request.workspace,
        "job": job,
        "outputs": job.outputs.select_related("social_account", "document"),
        "kind_labels": dict(RepurposeOutput.Kind.choices),
    }


@login_required
@require_permission("create_posts")
@require_GET
def job_detail(request, workspace_id, job_id):
    return render(request, "repurpose/detail.html", _detail_context(request, _job(request, job_id)))


@login_required
@require_permission("create_posts")
@require_GET
def job_status(request, workspace_id, job_id):
    """The job's body, swapped in every few seconds while it runs."""
    return render(request, "repurpose/_job.html", _detail_context(request, _job(request, job_id)))


@login_required
@require_permission("create_posts")
@require_POST
def job_cancel(request, workspace_id, job_id):
    job = _job(request, job_id)
    try:
        services.cancel_job(job)
    except services.JobStateError as exc:
        messages.info(request, str(exc))
    return redirect("repurpose:detail", workspace_id=request.workspace.id, job_id=job.id)


@login_required
@require_permission("create_posts")
@require_POST
def job_accept(request, workspace_id, job_id):
    job = _job(request, job_id)
    try:
        accepted, _ = services.accept_outputs(job, request.POST.getlist("output"), request.user)
    except (services.JobStateError, ValueError) as exc:
        messages.error(request, str(exc))
    else:
        if accepted:
            messages.success(request, f"{len(accepted)} saved as drafts or ideas. Nothing has been published.")
        else:
            messages.info(request, "Those were already saved.")
    return redirect("repurpose:detail", workspace_id=request.workspace.id, job_id=job.id)


@login_required
@require_permission("create_posts")
@require_POST
def output_discard(request, workspace_id, job_id, output_id):
    job = _job(request, job_id)
    try:
        services.discard_output(job, output_id)
    except services.JobStateError as exc:
        messages.error(request, str(exc))
    return redirect("repurpose:detail", workspace_id=request.workspace.id, job_id=job.id)


@login_required
@require_permission("create_posts")
@require_GET
def output_pdf(request, workspace_id, job_id, output_id):
    """Download a carousel's PDF (LinkedIn can't publish documents from here yet)."""
    out = get_object_or_404(RepurposeOutput, id=output_id, job=_job(request, job_id))
    if not out.document_id or not out.document.file:
        raise Http404
    return FileResponse(
        out.document.file.open("rb"), as_attachment=True, filename="carousel.pdf", content_type="application/pdf"
    )
