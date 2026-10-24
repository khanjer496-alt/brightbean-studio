"""One-tap Approve / Request changes links for approval emails.

A link is a signed reference to an :class:`ApprovalLink` row. The signature
stops guessing and tampering; the row makes the link single-use, expiring
(``APPROVAL_LINK_TTL_HOURS``) and bound to the person and address it was sent
to. Opening a link only ever shows a confirmation page (see
``views_links``): the decision happens on the POST behind its button, so a mail
scanner that fetches every URL cannot approve anything.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from django.conf import settings
from django.core import signing
from django.urls import reverse
from django.utils import timezone

from .models import ApprovalLink

SALT = "approvals.one_tap_link"
ACTIONS = (ApprovalLink.Action.APPROVE, ApprovalLink.Action.REQUEST_CHANGES)


def link_token(link: ApprovalLink) -> str:
    return signing.dumps(str(link.pk), salt=SALT)


def link_url(link: ApprovalLink) -> str:
    app_url = getattr(settings, "APP_URL", "http://localhost:8000").rstrip("/")
    return f"{app_url}{reverse('approval_links:confirm', kwargs={'token': link_token(link)})}"


def issue_links(post, recipient, workspace, fingerprint: str) -> dict[str, str]:
    """A fresh Approve and Request changes link for ``recipient``, as ``{action: url}``.

    Both share a group, so using either retires the other.
    """
    group = uuid.uuid4()
    links = ApprovalLink.objects.bulk_create(
        ApprovalLink(
            group=group,
            workspace=workspace,
            post=post,
            recipient=recipient,
            recipient_email=recipient.email,
            action=action,
            fingerprint=fingerprint,
        )
        for action in ACTIONS
    )
    return {link.action: link_url(link) for link in links}


def resolve_link(token: str) -> ApprovalLink | None:
    """The link a URL token names, or None when the signature or the row is bad."""
    try:
        link_id = signing.loads(token, salt=SALT)
        return ApprovalLink.objects.select_related("post", "workspace", "recipient").get(pk=link_id)
    except (signing.BadSignature, ApprovalLink.DoesNotExist, ValueError):
        return None


def retire_group(link: ApprovalLink) -> None:
    """Mark every link of ``link``'s email as used."""
    ApprovalLink.objects.filter(group=link.group, used_at__isnull=True).update(used_at=timezone.now())


def purge_expired(older_than_days: int = 30) -> int:
    """Delete links that have been dead for ``older_than_days``; returns how many."""
    cutoff = timezone.now() - timedelta(days=older_than_days)
    deleted, _ = ApprovalLink.objects.filter(expires_at__lt=cutoff).delete()
    return deleted
