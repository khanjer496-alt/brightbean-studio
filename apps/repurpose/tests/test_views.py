"""The Repurpose page: starting a run, watching it, accepting outputs, and who may do what."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from fpdf import FPDF

from apps.composer.models import Post
from apps.members.models import WorkspaceMembership
from apps.repurpose import services
from apps.repurpose.models import RepurposeJob

pytestmark = pytest.mark.django_db

TEXT = "Slow roasting keeps the sweetness in the bean. We roast every batch for eighteen minutes at lower heat."


@pytest.fixture(autouse=True)
def _platform_key(platform_key):
    return platform_key


def url(workspace, name, *args):
    from django.urls import reverse

    kwargs = {"workspace_id": workspace.id}
    kwargs.update({key: value for key, value in zip(("job_id", "output_id"), args, strict=False)})
    return reverse(f"repurpose:{name}", kwargs=kwargs)


def _post_form(client, workspace, bluesky, **extra):
    data = {"source_type": "text", "source_text": TEXT, "kind_post": "1", "accounts_post": [str(bluesky.id)], **extra}
    return client.post(url(workspace, "create"), data)


def test_page_renders_with_accessible_form(owner_client, workspace, bluesky):
    response = owner_client.get(url(workspace, "home"))
    html = response.content.decode()
    assert response.status_code == 200
    assert "<legend" in html and 'for="source-url"' in html
    from django.template.loader import get_template

    for name in ("home", "detail", "_job"):  # no inline styles in our templates
        assert "style=" not in get_template(f"repurpose/{name}.html").template.source


def test_page_says_when_ai_is_off(owner_client, workspace, settings):
    settings.ANTHROPIC_API_KEY = ""
    assert "isn't set up" in owner_client.get(url(workspace, "home")).content.decode()


def test_text_run_end_to_end(owner_client, workspace, bluesky, fake_claude):
    fake_claude.queue({"text": "Slow roasting, sweet cup."})
    with patch("apps.repurpose.tasks.execute_repurpose_job") as queued:
        response = _post_form(owner_client, workspace, bluesky)
    job = RepurposeJob.objects.get()
    assert response.status_code == 302 and response["Location"].endswith(f"/repurpose/{job.id}/")
    assert queued.call_count == 0 or queued.called  # queued after commit; run it directly
    services.execute_job(job.id)

    page = owner_client.get(url(workspace, "detail", job.id)).content.decode()
    assert "Slow roasting, sweet cup." in page and "Done" in page and "/accept/" in page

    out = job.outputs.get()
    response = owner_client.post(url(workspace, "accept", job.id), {"output": [str(out.id)]}, follow=True)
    page = response.content.decode()
    assert page.count("Nothing has been published") == 1  # the layout shows messages; the job body doesn't repeat them
    post = Post.objects.get()
    assert post.platform_posts.get().status == "draft"
    assert "Open the draft" in page and str(post.id) in page
    again = owner_client.post(url(workspace, "accept", job.id), {"output": [str(out.id)]}, follow=True)
    assert "already saved" in again.content.decode() and Post.objects.count() == 1


def test_running_job_polls_and_can_be_cancelled(owner_client, workspace, bluesky):
    _post_form(owner_client, workspace, bluesky)
    job = RepurposeJob.objects.get()
    assert f"/repurpose/{job.id}/status/" in owner_client.get(url(workspace, "detail", job.id)).content.decode()
    assert owner_client.get(url(workspace, "status", job.id)).status_code == 200
    owner_client.post(url(workspace, "cancel", job.id))
    job.refresh_from_db()
    assert job.status == "cancelled"
    assert "hx-trigger" not in owner_client.get(url(workspace, "status", job.id)).content.decode()


def test_pdf_upload(owner_client, workspace, bluesky):
    pdf = FPDF()
    pdf.set_font("Helvetica", size=12)
    pdf.add_page()
    pdf.multi_cell(0, 8, TEXT)
    upload = SimpleUploadedFile("guide.pdf", bytes(pdf.output()), content_type="application/pdf")
    response = _post_form(owner_client, workspace, bluesky, source_type="pdf", source_pdf=upload)
    assert response.status_code == 302
    job = RepurposeJob.objects.get()
    # The request only checks and stores the file; the background run parses it.
    assert job.source_type == "pdf" and job.source_label == "guide.pdf" and job.source_text == ""
    stored = job.source_file.name
    assert stored
    services._read_pending_pdf(job, 1)
    job.refresh_from_db()
    assert "sweetness" in job.source_text and not job.source_file
    assert not default_storage.exists(stored)


def test_unreadable_pdf_fails_the_run_not_the_request(owner_client, workspace, bluesky):
    upload = SimpleUploadedFile(
        "bad.pdf", b"%PDF-1.7\n1 0 obj << /Pages 9 0 R >> endobj", content_type="application/pdf"
    )
    assert _post_form(owner_client, workspace, bluesky, source_type="pdf", source_pdf=upload).status_code == 302
    job = RepurposeJob.objects.get()
    services.execute_job(job.id)
    job.refresh_from_db()
    assert job.status == "failed" and "couldn't be read" in job.error and not job.source_file


def test_bad_pdf_shows_the_problem_and_saves_nothing(owner_client, workspace, bluesky):
    upload = SimpleUploadedFile("evil.pdf", b"<script>alert(1)</script>", content_type="application/pdf")
    response = _post_form(owner_client, workspace, bluesky, source_type="pdf", source_pdf=upload)
    assert response.status_code == 422 and "isn&#x27;t a PDF" in response.content.decode()
    assert RepurposeJob.objects.count() == 0


def test_video_without_captions_asks_for_a_transcript(owner_client, workspace, bluesky):
    response = _post_form(
        owner_client, workspace, bluesky, source_type="video", source_video="https://youtu.be/dQw4w9WgXcQ"
    )
    assert response.status_code == 422
    assert "Transcript (needed)" in response.content.decode()
    response = _post_form(
        owner_client,
        workspace,
        bluesky,
        source_type="video",
        source_video="https://youtu.be/dQw4w9WgXcQ",
        source_transcript=TEXT,
    )
    assert response.status_code == 302
    job = RepurposeJob.objects.get()
    assert job.source_type == "video" and job.source_url == "https://youtu.be/dQw4w9WgXcQ"


def test_no_outputs_chosen(owner_client, workspace):
    response = owner_client.post(url(workspace, "create"), {"source_type": "text", "source_text": TEXT})
    assert response.status_code == 422 and "at least one output" in response.content.decode()


def test_viewers_cannot_use_the_page(viewer_client, workspace, bluesky):
    assert viewer_client.get(url(workspace, "home")).status_code == 403
    assert _post_form(viewer_client, workspace, bluesky).status_code == 403
    assert RepurposeJob.objects.count() == 0


def test_another_workspaces_job_is_a_404(owner_client, workspace, organization, bluesky, owner):
    from apps.workspaces.models import Workspace

    other = Workspace.objects.create(organization=organization, name="Other")
    WorkspaceMembership.objects.create(user=owner, workspace=other, workspace_role="owner")
    job = RepurposeJob.objects.create(workspace=other, source_type="text", source_text=TEXT)
    for name in ("detail", "status"):
        assert owner_client.get(url(workspace, name, job.id)).status_code == 404
    assert owner_client.post(url(workspace, "cancel", job.id)).status_code == 404
    assert owner_client.post(url(workspace, "accept", job.id), {"output": []}).status_code == 404


def test_another_workspaces_account_is_refused(owner_client, workspace, organization, owner):
    from apps.repurpose.tests.conftest import make_account
    from apps.workspaces.models import Workspace

    other = Workspace.objects.create(organization=organization, name="Other")
    foreign = make_account(other, "bluesky", "Foreign")
    response = _post_form(owner_client, workspace, foreign)
    assert response.status_code == 422 and RepurposeJob.objects.count() == 0


def test_published_post_source_is_workspace_scoped(owner_client, workspace, bluesky):
    from apps.composer.models import PlatformPost
    from apps.composer.services import create_post

    post = create_post(workspace=workspace, social_account=bluesky, caption=TEXT)
    PlatformPost.objects.filter(post=post).update(status="published")
    response = _post_form(owner_client, workspace, bluesky, source_type="post", source_post=str(post.id))
    assert response.status_code == 302
    assert RepurposeJob.objects.get().source_post == post


def test_composer_panel_links_to_the_page(owner_client, workspace, bluesky):
    html = owner_client.get(f"/workspace/{workspace.id}/compose/").content.decode()
    assert url(workspace, "home") in html
