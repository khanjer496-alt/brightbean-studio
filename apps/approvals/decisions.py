"""One-shot approval decisions from outside the web app's own buttons.

The email links, the Slack buttons and the portal's batch page each decide on a
post as a particular person, on the content that person was shown. They all go
through :func:`decide`, which re-checks that content, calls the same service the
web buttons call (so permissions, the workflow stages, notifications and
webhooks are identical), and leaves the audit entry in the autonomy trail.
"""

from __future__ import annotations

from django.db import transaction

from apps.autonomy import service as autonomy_service
from apps.autonomy.actors import human as human_actor
from apps.autonomy.models import ActionType, Decision
from apps.members.models import WorkspaceMembership

from . import services
from .models import Channel
from .policy import review_fingerprint

APPROVE = "approve"
REQUEST_CHANGES = "request_changes"

_SERVICES = {
    APPROVE: services.approve_post,
    REQUEST_CHANGES: services.request_changes,
}

_CHANNEL_LABEL = {
    Channel.EMAIL: "an email link",
    Channel.SLACK: "Slack",
    Channel.PORTAL: "the client portal",
    Channel.API: "the API",
}


class DecisionError(ValueError):
    """The decision could not be made; the message says why, in words for the decider."""


class ContentChangedError(DecisionError):
    """The post changed after the decider was shown it."""

    def __init__(self):
        super().__init__("This post changed — review the new version.")


class NothingWaitingError(DecisionError):
    """No part of the post is waiting on this person any more."""

    def __init__(self):
        super().__init__("This post is no longer waiting for your decision.")


def review_states(user, workspace) -> set[str]:
    """The editorial statuses ``user`` decides on in ``workspace`` (empty when they are not a member)."""
    membership = (
        WorkspaceMembership.objects.select_related("workspace")
        .filter(user=user, workspace=workspace, user__is_active=True)
        .first()
    )
    return services.review_states(membership) if membership else set()


def current_fingerprint(post, user, workspace) -> str:
    """What ``user`` would be deciding on right now: the hash of the targets in their stage."""
    return review_fingerprint(post, review_states(user, workspace))


def decide(post, user, workspace, decision, comment="", *, channel, expected_fingerprint=""):
    """Record ``user``'s ``decision`` ("approve" / "request_changes") on ``post``.

    ``expected_fingerprint`` is the content hash the decider was shown; when it
    no longer matches, nothing happens and :class:`ContentChangedError` is raised.
    Raises :class:`DecisionError` for anything else that stops the decision.
    """
    if decision not in _SERVICES:
        raise DecisionError("Unknown decision.")
    states = review_states(user, workspace)
    with transaction.atomic():
        # Lock the targets so the check and the decision see the same content.
        list(post.platform_posts.select_for_update().filter(status__in=states))
        current = review_fingerprint(post, states)
        if not current:
            raise NothingWaitingError
        if expected_fingerprint and current != expected_fingerprint:
            raise ContentChangedError
        try:
            if decision == APPROVE:
                moved = services.approve_post(post, user, workspace, comment, channel=channel)
            else:
                moved = services.request_changes(post, user, workspace, comment, channel=channel)
        except ValueError as exc:
            raise DecisionError(str(exc)) from exc
        if not moved:
            raise NothingWaitingError
        autonomy_service.record(
            workspace=workspace,
            actor=human_actor(user),
            action_type=ActionType.APPROVAL,
            operation=f"approval.{decision}",
            decision=Decision.EXECUTED,
            post=post,
            summary=f"Recorded {decision.replace('_', ' ')} from {_CHANNEL_LABEL.get(channel, channel)}.",
            final_text=comment or post.caption,
        )
    return moved


def decide_batch(user, workspace, items, *, channel):
    """Decide several posts at once: ``items`` is ``[(post, shown_fingerprint, comment)]``, all approvals.

    Each post is decided on its own, so one that changed or was already
    handled is reported and the rest still go through. Returns
    ``[(post, outcome, message)]`` with ``outcome`` one of ``approved``,
    ``changed`` or ``skipped``.
    """
    results = []
    for post, fingerprint, comment in items:
        try:
            decide(post, user, workspace, APPROVE, comment, channel=channel, expected_fingerprint=fingerprint)
        except ContentChangedError as exc:
            results.append((post, "changed", str(exc)))
        except DecisionError as exc:
            results.append((post, "skipped", str(exc)))
        else:
            results.append((post, "approved", ""))
    return results
