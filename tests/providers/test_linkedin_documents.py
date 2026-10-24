"""LinkedIn document (PDF carousel) posts through the Documents API, with HTTP mocked."""

import io
from unittest.mock import MagicMock

import pytest
from pypdf import PdfWriter

from providers import PROVIDER_REGISTRY, media_rules_for
from providers.exceptions import PublishError
from providers.linkedin_company import LinkedInCompanyProvider
from providers.linkedin_personal import LinkedInPersonalProvider
from providers.types import PostType, PublishContent


def _pdf(path, pages):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=100, height=100)
    buffer = io.BytesIO()
    writer.write(buffer)
    path.write_bytes(buffer.getvalue())
    return str(path)


def _resp(data=None, headers=None):
    return MagicMock(json=MagicMock(return_value=data or {}), headers=headers or {})


def _provider(cls):
    provider = cls({"client_id": "id", "client_secret": "secret"})
    provider._upload_binary = MagicMock()
    provider._wait_for_document_available = MagicMock()
    provider._request = MagicMock(
        side_effect=[
            _resp({"value": {"uploadUrl": "https://www.linkedin.com/dms-uploads/x", "document": "urn:li:document:D1"}}),
            _resp(headers={"x-restli-id": "urn:li:share:99"}),
        ]
    )
    return provider


def _content(path, author):
    return PublishContent(
        text="Slides inside",
        title="My carousel",
        media_files=[path],
        media_urls=["https://m/x.pdf"],
        media_types=["document"],
        post_type=PostType.DOCUMENT,
        extra={"author": author},
    )


@pytest.mark.parametrize(
    "cls, author",
    [(LinkedInPersonalProvider, "urn:li:person:1"), (LinkedInCompanyProvider, "urn:li:organization:7")],
)
def test_document_post_initializes_uploads_and_posts_with_media_id_and_title(tmp_path, cls, author):
    provider = _provider(cls)
    result = provider.publish_post("tok", _content(_pdf(tmp_path / "c.pdf", 3), author))

    init, post = provider._request.call_args_list
    assert init.args[:2] == ("POST", "https://api.linkedin.com/rest/documents")
    assert init.kwargs["params"] == {"action": "initializeUpload"}
    assert init.kwargs["json"] == {"initializeUploadRequest": {"owner": author}}
    provider._upload_binary.assert_called_once_with(
        "tok", "https://www.linkedin.com/dms-uploads/x", str(tmp_path / "c.pdf")
    )
    provider._wait_for_document_available.assert_called_once()
    body = post.kwargs["json"]
    assert body["author"] == author
    assert body["content"] == {"media": {"title": "My carousel", "id": "urn:li:document:D1"}}
    assert result.platform_post_id == "urn:li:share:99"


def test_a_pdf_over_300_pages_is_refused_before_any_request(tmp_path):
    provider = _provider(LinkedInPersonalProvider)
    with pytest.raises(PublishError) as exc:
        provider.publish_post("tok", _content(_pdf(tmp_path / "big.pdf", 301), "urn:li:person:1"))
    assert exc.value.retryable is False
    provider._request.assert_not_called()


def test_a_document_over_100_mb_is_refused(tmp_path, monkeypatch):
    provider = _provider(LinkedInPersonalProvider)
    path = _pdf(tmp_path / "c.pdf", 1)
    monkeypatch.setattr("providers.linkedin.os.path.getsize", lambda _p: 101 * 1024 * 1024)
    with pytest.raises(PublishError):
        provider.publish_post("tok", _content(path, "urn:li:person:1"))
    provider._request.assert_not_called()


def test_both_linkedin_providers_publish_documents_and_declare_rules():
    for platform in ("linkedin_personal", "linkedin_company"):
        assert PROVIDER_REGISTRY[platform].publishes_documents is True
        rules = media_rules_for(platform, PostType.DOCUMENT)
        assert rules.max_items == 1 and rules.max_document_bytes == 100 * 1024 * 1024


def test_processing_failure_is_permanent():
    provider = LinkedInPersonalProvider({"client_id": "id", "client_secret": "secret"})
    provider._request = MagicMock(return_value=_resp({"status": "PROCESSING_FAILED"}))
    with pytest.raises(PublishError) as exc:
        provider._wait_for_document_available("tok", "urn:li:document:D1", timeout_seconds=1, poll_interval=0.01)
    assert exc.value.retryable is False


def test_the_engine_resolves_a_document_attachment_to_a_document_post():
    from apps.publisher.engine import PublishEngine

    assert PublishEngine._resolve_post_type("linkedin_company", {}, 1, "document") is PostType.DOCUMENT
