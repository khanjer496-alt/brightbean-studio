"""The confirmation page behind a one-tap link in an approval email.

GET never changes anything, so a mail scanner or a link-preview bot that
fetches the URL is harmless. The decision happens on the POST behind the page's
single button (CSRF-protected), as the person the link was sent to, whether or
not they are signed in; the link grants that one decision and nothing else.

The token is in the path, so these responses are kept out of Django's request
log, carry ``Referrer-Policy: no-referrer`` (nothing on the page leaks the URL
onward) and are rate limited per address.
"""

from __future__ import annotations

from django.db import transaction
from django.http import Http404, HttpResponseRedirect
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods
from django_ratelimit.decorators import ratelimit

from apps.composer.previews import post_previews
from apps.members.models import WorkspaceMembership
from apps.notifications.engine import notify
from apps.notifications.models import EventType

from . import comments as comment_service
from . import decisions
from .links import lock_group, resolve_link, resolve_media, retire_group
from .models import ApprovalLink, ApprovalReminder, Channel, PostComment

INVALID = "invalid"
USED = "used"
EXPIRED = "expired"
WRONG_USER = "wrong_user"
NOTHING = "nothing"
STALE = "stale"
READY = "ready"
DONE = "done"

RATE = "30/m"

_DECISION = {
    ApprovalLink.Action.APPROVE: decisions.APPROVE,
    ApprovalLink.Action.REQUEST_CHANGES: decisions.REQUEST_CHANGES,
    ApprovalLink.Action.APPROVE_HELD: "approve",
    ApprovalLink.Action.DISMISS_HELD: "dismiss",
}


def _page(request, context, status=200):
    response = render(request, "approvals/link/page.html", context, status=status)
    response["Referrer-Policy"] = "no-referrer"
    response["X-Robots-Tag"] = "noindex, nofollow"
    # Django's request logger writes the path of every 4xx; the path holds the token.
    response._has_been_logged = True
    return response


def _held_state(link):
    action = link.agent_action
    if action.decision != "held" or action.review_outcome:
        return NOTHING
    if not decisions.held_verbs(link.recipient, link.workspace):
        return INVALID
    return READY


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
    if link.is_held_item:
        return _held_state(link)
    current = decisions.current_fingerprint(link.post, recipient, link.workspace)
    if not current:
        return NOTHING
    if current != link.fingerprint:
        return STALE
    return READY


def _shared_comments(post, recipient):
    """Only comments shared with the client: whoever holds the link may not be the team member it was sent to."""
    return [
        c
        for c in comment_service.get_comments_for_post(post, recipient)
        if c.visibility == PostComment.Visibility.EXTERNAL
    ]


def _context(link, state, **extra):
    context = {"state": state, "link": link, "READY": READY, **extra}
    if link is None or state not in (READY, STALE, NOTHING):
        return context
    if link.is_held_item:
        action = link.agent_action
        pp = action.platform_post
        context["held"] = action
        context["workspace"] = link.workspace
        if pp is not None:
            context.update(post_previews(pp.post, [pp]))
        return context
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
    context["comments"] = _shared_comments(post, recipient)
    context["due_at"] = (
        ApprovalReminder.objects.filter(post=post, stage__in=states, due_at__isnull=False)
        .values_list("due_at", flat=True)
        .first()
    )
    return context


def _decide(link, comment):
    if link.is_held_item:
        decisions.decide_held(link.agent_action, link.recipient, _DECISION[link.action], channel=Channel.EMAIL)
        return
    decisions.decide(
        link.post,
        link.recipient,
        link.workspace,
        _DECISION[link.action],
        comment,
        channel=Channel.EMAIL,
        expected_fingerprint=link.fingerprint,
    )


@never_cache
@ratelimit(key="ip", rate=RATE, block=True)
@require_http_methods(["GET", "HEAD", "POST"])
def confirm(request, token):
    link = resolve_link(token)
    state = _link_state(request, link)

    if request.method != "POST" or state != READY:
        status = 200 if state in (READY, STALE, NOTHING, WRONG_USER) else 410
        if link is None:
            status = 404
        return _page(request, _context(link, state), status=status)

    comment = request.POST.get("comment", "").strip()
    error = ""
    with transaction.atomic():
        # Serialise taps on this email item (the same button twice, or both):
        # the loser sees it already used.
        if any(sibling.used_at is not None for sibling in lock_group(link)):
            return _page(request, _context(link, USED), status=410)
        try:
            _decide(link, comment)
        except decisions.ContentChangedError:
            return _page(request, _context(link, STALE))
        except decisions.NothingWaitingError:
            return _page(request, _context(link, NOTHING))
        except decisions.DecisionError as exc:
            # Typically a missing comment: the link stays usable.
            error = str(exc)
        else:
            retire_group(link)
            return _page(request, _context(link, DONE))
    return _page(request, _context(link, READY, error=error, comment=comment), status=422)


@never_cache
@ratelimit(key="ip", rate=RATE, block=True)
@require_http_methods(["POST"])
def resend(request, token):
    """After a post changed: retire the stale links and email a fresh pair for the new version."""
    link = resolve_link(token)
    state = _link_state(request, link)
    if state != STALE:
        return _page(request, _context(link, state), status=200 if link else 404)
    retire_group(link)
    notify(
        user=link.recipient,
        event_type=EventType.APPROVAL_REMINDER,
        title="Updated post waiting for your approval",
        body=f'"{link.post.caption_snippet}" changed after your last email. Here is the new version.',
        data={"post_id": str(link.post_id), "workspace_id": str(link.workspace_id)},
    )
    return _page(request, _context(link, "resent"))


@ratelimit(key="ip", rate="120/m", block=True)
@require_http_methods(["GET", "HEAD"])
def email_media(request, token):
    """A picture in an approval email: a signed reference that redirects to a fresh storage URL."""
    asset = resolve_media(token)
    url = asset.preview_url if asset else ""
    if not url:
        raise Http404
    if url.startswith("/"):
        url = request.build_absolute_uri(url)
    response = HttpResponseRedirect(url)
    response["Referrer-Policy"] = "no-referrer"
    response["Cache-Control"] = "private, max-age=600"
    return response
