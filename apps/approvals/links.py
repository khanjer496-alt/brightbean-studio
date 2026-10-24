"""One-tap Approve / Request changes links for approval emails.

A link is a signed reference to an :class:`ApprovalLink` row and the decision
it makes. The signature (its own salt, used for nothing else) stops guessing
and tampering, and a token minted for one button can't be replayed as the
other; the row (a random UUID) makes the link single-use, expiring
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
MEDIA_SALT = "approvals.email_media"
# How long a picture in an approval email keeps loading (the email outlives the storage's presigned URLs).
MEDIA_MAX_AGE = timedelta(days=7)
ACTIONS = (ApprovalLink.Action.APPROVE, ApprovalLink.Action.REQUEST_CHANGES)
HELD_ACTIONS = (ApprovalLink.Action.APPROVE_HELD, ApprovalLink.Action.DISMISS_HELD)


def link_token(link: ApprovalLink) -> str:
    return signing.dumps([str(link.pk), link.action], salt=SALT, compress=True)


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


def issue_held_links(agent_action, recipient, *, verbs) -> dict[str, str]:
    """Approve / Dismiss links for an item an autonomy policy held, as ``{verb: url}``.

    ``verbs`` is the subset of ``("approve", "dismiss")`` the recipient may use;
    both share a group, so using either retires the other.
    """
    group = uuid.uuid4()
    wanted = [action for action in HELD_ACTIONS if action.removesuffix("_held") in verbs]
    links = ApprovalLink.objects.bulk_create(
        ApprovalLink(
            group=group,
            workspace_id=agent_action.workspace_id,
            agent_action=agent_action,
            recipient=recipient,
            recipient_email=recipient.email,
            action=action,
        )
        for action in wanted
    )
    return {link.action.removesuffix("_held"): link_url(link) for link in links}


def media_url(asset_id) -> str:
    """A signed, expiring address for one media asset's picture in an email (never an attachment).

    It redirects to a freshly presigned storage URL each time it is opened, so the
    picture keeps working after the storage's own short-lived URL would have expired.
    """
    token = signing.dumps(str(asset_id), salt=MEDIA_SALT, compress=True)
    app_url = getattr(settings, "APP_URL", "http://localhost:8000").rstrip("/")
    return f"{app_url}{reverse('approval_links:media', kwargs={'token': token})}"


def resolve_media(token: str):
    """The ``MediaAsset`` a signed email-media token names, or None when it is bad or too old."""
    from apps.media_library.models import MediaAsset

    try:
        asset_id = signing.loads(token, salt=MEDIA_SALT, max_age=MEDIA_MAX_AGE)
        return MediaAsset.objects.filter(pk=asset_id).first()
    except (signing.BadSignature, ValueError):
        return None


def resolve_link(token: str) -> ApprovalLink | None:
    """The link a URL token names, or None when the signature, the decision or the row is bad."""
    try:
        link_id, action = signing.loads(token, salt=SALT)
        link = ApprovalLink.objects.select_related("post", "agent_action", "workspace", "recipient").get(pk=link_id)
    except (signing.BadSignature, ApprovalLink.DoesNotExist, ValueError, TypeError):
        return None
    return link if link.action == action else None


def lock_group(link: ApprovalLink) -> list[ApprovalLink]:
    """Lock every link of ``link``'s email item (call inside a transaction).

    Two taps, on the same button or on both, queue here: the second sees the
    first's ``used_at`` and stops.
    """
    return list(ApprovalLink.objects.select_for_update().filter(group=link.group).order_by("pk"))


def retire_group(link: ApprovalLink) -> None:
    """Mark every link of ``link``'s email item as used."""
    ApprovalLink.objects.filter(group=link.group, used_at__isnull=True).update(used_at=timezone.now())


def retire_for(*, recipient_id, workspace_id) -> int:
    """Retire the live links a person removed from a workspace still holds; returns how many."""
    return ApprovalLink.objects.filter(
        recipient_id=recipient_id, workspace_id=workspace_id, used_at__isnull=True
    ).update(used_at=timezone.now())


def purge_expired(older_than_days: int = 30) -> int:
    """Delete links that have been dead for ``older_than_days``; returns how many."""
    cutoff = timezone.now() - timedelta(days=older_than_days)
    deleted, _ = ApprovalLink.objects.filter(expires_at__lt=cutoff).delete()
    return deleted
