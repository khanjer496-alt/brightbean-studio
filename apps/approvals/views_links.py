"""The confirmation page behind a one-tap link in an approval email.

GET never changes anything, so a mail scanner or a link-preview bot that
fetches the URL is harmless. The decision happens on the POST behind the page's
single button, as the person the link was sent to, whether or not they are
signed in; the link grants that one decision and nothing else.
"""

from __future__ import annotations

from django.db import transaction
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from apps.composer.previews import post_previews
from apps.members.models import WorkspaceMembership
from apps.notifications.engine import notify
from apps.notifications.models import EventType

from . import comments as comment_service
from . import decisions
from .links import resolve_link, retire_group
from .models import ApprovalLink, ApprovalReminder, Channel

INVALID = "invalid"
USED = "used"
EXPIRED = "expired"
WRONG_USER = "wrong_user"
NOTHING = "nothing"
STALE = "stale"
READY = "ready"
DONE = "done"

_DECISION = {
    ApprovalLink.Action.APPROVE: decisions.APPROVE,
    ApprovalLink.Action.REQUEST_CHANGES: decisions.REQUEST_CHANGES,
}


def _link_state(request, link):
    """Why ``link`` can't be used right now, or ``READY``."""
    if link is None:
        return INVALID
    recipient = link.recipient
    # The link is bound to the person and the address it was sent to: an
    # account that has since been deactivated or re-addressed no longer qualifies.
    if not recipient.is_active or recipient.email.lower() != link.recipient_email.lower():
        return INVALID
    if link.used_at is not None:
        return USED
    if link.is_expired:
        return EXPIRED
    if request.user.is_authenticated and request.user.pk != recipient.pk:
        return WRONG_USER
    if not WorkspaceMembership.objects.filter(user=recipient, workspace=link.workspace).exists():
        return INVALID
    current = decisions.current_fingerprint(link.post, recipient, link.workspace)
    if not current:
        return NOTHING
    if current != link.fingerprint:
        return STALE
    return READY


def _context(link, state, **extra):
    context = {"state": state, "link": link, "READY": READY, **extra}
    if link is not None and state in (READY, STALE, NOTHING):
        post, recipient, workspace = link.post, link.recipient, link.workspace
        states = decisions.review_states(recipient, workspace)
        targets = list(
            post.platform_posts.filter(status__in=states)
            .select_related("social_account")
            .order_by("social_account__platform")
        ) or list(post.platform_posts.select_related("social_account").order_by("social_account__platform"))
        context.update(post_previews(post, targets))
        context["post"] = post
        context["workspace"] = workspace
        context["comments"] = list(comment_service.get_comments_for_post(post, recipient))
        context["due_at"] = (
            ApprovalReminder.objects.filter(post=post, stage__in=states, due_at__isnull=False)
            .values_list("due_at", flat=True)
            .first()
        )
    return context


@never_cache
@require_http_methods(["GET", "HEAD", "POST"])
def confirm(request, token):
    link = resolve_link(token)
    state = _link_state(request, link)

    if request.method != "POST" or state != READY:
        status = 200 if state in (READY, STALE, NOTHING, WRONG_USER) else 410
        if link is None:
            status = 404
        return render(request, "approvals/link/page.html", _context(link, state), status=status)

    comment = request.POST.get("comment", "").strip()
    error = ""
    with transaction.atomic():
        # Serialise two taps on the same link: the loser sees it already used.
        locked = (
            ApprovalLink.objects.select_for_update().select_related("post", "workspace", "recipient").get(pk=link.pk)
        )
        if locked.used_at is not None:
            return render(request, "approvals/link/page.html", _context(locked, USED), status=410)
        try:
            decisions.decide(
                locked.post,
                locked.recipient,
                locked.workspace,
                _DECISION[locked.action],
                comment,
                channel=Channel.EMAIL,
                expected_fingerprint=locked.fingerprint,
            )
        except decisions.ContentChangedError:
            return render(request, "approvals/link/page.html", _context(locked, STALE))
        except decisions.NothingWaitingError:
            return render(request, "approvals/link/page.html", _context(locked, NOTHING))
        except decisions.DecisionError as exc:
            # Typically a missing comment: the link stays usable.
            error = str(exc)
        else:
            retire_group(locked)
            return render(request, "approvals/link/page.html", _context(locked, DONE))
    return render(request, "approvals/link/page.html", _context(link, READY, error=error, comment=comment), status=422)


@never_cache
@require_http_methods(["POST"])
def resend(request, token):
    """After a post changed: retire the stale links and email a fresh pair for the new version."""
    link = resolve_link(token)
    state = _link_state(request, link)
    if state != STALE:
        return render(request, "approvals/link/page.html", _context(link, state), status=200 if link else 404)
    retire_group(link)
    notify(
        user=link.recipient,
        event_type=EventType.APPROVAL_REMINDER,
        title="Updated post waiting for your approval",
        body=f'"{link.post.caption_snippet}" changed after your last email. Here is the new version.',
        data={"post_id": str(link.post_id), "workspace_id": str(link.workspace_id)},
    )
    return render(request, "approvals/link/page.html", _context(link, "resent"))
