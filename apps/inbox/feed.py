"""Queries behind the inbox feed: filters, cursor pagination, search and polling.

The feed view, the "new messages" poll and the bulk actions all read the same
filter parameters, so they go through ``filtered_messages`` and can't disagree
about what "the messages matching this filter" means.

Pages are cut by a cursor on ``(received_at, id)`` rather than an offset, so
messages arriving (or being resolved out of the filter) while someone scrolls
never shift a row onto two pages or skip one.
"""

from __future__ import annotations

import base64
import binascii
import re
import uuid
from datetime import datetime
from urllib.parse import urlencode

from django.contrib.postgres.search import SearchQuery, SearchVector
from django.db import connection
from django.db.models import Q

from .models import InboxMessage

MESSAGES_PER_PAGE = 50
# The "N new messages" pill never counts past this; "99+" is as useful as the exact number.
NEW_MESSAGES_CAP = 100
SEARCH_CONFIG = "simple"
SEARCH_SNIPPET_CHARS = 140

# Query parameters that define "the messages matching this filter".
FILTER_KEYS = ("view", "platform", "account", "type", "status", "assigned", "sentiment", "date_from", "date_to", "q")


def search_vector():
    """The expression the full-text GIN index is built on (see migration 0007)."""
    return SearchVector("body", "sender_name", "sender_handle", config=SEARCH_CONFIG)


def _values(params, key):
    return [v for v in params.getlist(key) if v]


def apply_search(qs, q):
    """Full-text match on message text and author, falling back to a substring match.

    ``websearch`` syntax is whole-word, so a partial word ("refu") or a
    non-Postgres database would find nothing; in that case, and when the
    full-text query is empty, the old substring match answers instead.
    """
    if connection.vendor == "postgresql":
        matched = qs.alias(search=search_vector()).filter(
            search=SearchQuery(q, config=SEARCH_CONFIG, search_type="websearch")
        )
        if matched.exists():
            return matched
    return qs.filter(Q(body__icontains=q) | Q(sender_name__icontains=q) | Q(sender_handle__icontains=q))


def filtered_messages(workspace, params, user):
    """The workspace's messages matching ``params``, newest first.

    Returns ``(queryset, active_filters, view)``. ``params`` is a QueryDict
    (``GET`` for the feed and poll, ``POST`` for bulk actions).
    """
    qs = InboxMessage.objects.for_workspace(workspace.id).select_related("social_account", "assigned_to")

    view = params.get("view", "all")
    if view == "mine":
        qs = qs.filter(assigned_to=user)
    elif view == "unassigned":
        qs = qs.filter(assigned_to__isnull=True)

    platforms = _values(params, "platform")
    if platforms:
        qs = qs.filter(social_account__platform__in=platforms)

    accounts = _values(params, "account")
    if accounts:
        qs = qs.filter(social_account_id__in=accounts)

    types = _values(params, "type")
    if types:
        qs = qs.filter(message_type__in=types)

    statuses = _values(params, "status")
    if statuses:
        qs = qs.filter(status__in=statuses)

    assigned = params.get("assigned")
    if assigned:
        qs = qs.filter(assigned_to__isnull=True) if assigned == "unassigned" else qs.filter(assigned_to_id=assigned)

    sentiments = _values(params, "sentiment")
    if sentiments:
        qs = qs.filter(sentiment__in=sentiments)

    date_from = params.get("date_from")
    if date_from:
        qs = qs.filter(received_at__date__gte=date_from)

    date_to = params.get("date_to")
    if date_to:
        qs = qs.filter(received_at__date__lte=date_to)

    q = params.get("q", "").strip()
    if q:
        qs = apply_search(qs, q)

    active_filters = {
        "platform": platforms,
        "account": accounts,
        "type": types,
        "status": statuses,
        "assigned": assigned,
        "sentiment": sentiments,
        "date_from": date_from,
        "date_to": date_to,
        "q": q,
    }
    return qs.order_by("-received_at", "-id"), active_filters, view


def filter_pairs(params):
    """The filter parameters in ``params`` as ``(key, value)`` pairs, empty values dropped."""
    return [(key, value) for key in FILTER_KEYS for value in _values(params, key)]


# --- Cursor ---


def encode_cursor(message):
    raw = f"{message.received_at.isoformat()}|{message.id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(value):
    """``(received_at, id)`` for a cursor from ``encode_cursor``, or None when it is missing or malformed."""
    if not value:
        return None
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()
        stamp, _, message_id = raw.partition("|")
        received_at = datetime.fromisoformat(stamp)
        if received_at.tzinfo is None:
            return None
        return received_at, uuid.UUID(message_id)
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None


def older_than(qs, cursor):
    """Rows that sort after ``cursor`` in the feed's newest-first order."""
    received_at, message_id = cursor
    return qs.filter(Q(received_at__lt=received_at) | Q(received_at=received_at, id__lt=message_id))


def newer_than(qs, cursor):
    """Rows that sort before ``cursor`` in the feed's newest-first order."""
    received_at, message_id = cursor
    return qs.filter(Q(received_at__gt=received_at) | Q(received_at=received_at, id__gt=message_id))


def feed_page(workspace, params, user, cursor_value=""):
    """One page of the feed and everything the list partial needs to render it."""
    qs, active_filters, view = filtered_messages(workspace, params, user)
    cursor = decode_cursor(cursor_value)
    if cursor:
        qs = older_than(qs, cursor)
    rows = list(qs[: MESSAGES_PER_PAGE + 1])
    has_more = len(rows) > MESSAGES_PER_PAGE
    page = rows[:MESSAGES_PER_PAGE]
    pairs = filter_pairs(params)
    return {
        "inbox_messages": page,
        "filter_query": urlencode(pairs),
        "load_more_query": urlencode([*pairs, ("cursor", encode_cursor(page[-1]))]) if has_more else "",
        "poll_query": urlencode([*pairs, ("since", encode_cursor(page[0]) if page and not cursor else "")]),
        "active_filters": active_filters,
        "current_view": view,
    }


def new_message_count(workspace, params, user, since_value):
    """How many messages matching the filter arrived after the ``since`` cursor (capped).

    An empty or unreadable cursor counts from the start of time, so a list
    that rendered empty still learns about its first message.
    """
    qs, _active, _view = filtered_messages(workspace, params, user)
    cursor = decode_cursor(since_value)
    if cursor:
        qs = newer_than(qs, cursor)
    return len(qs.order_by().values_list("id", flat=True)[:NEW_MESSAGES_CAP])


def unread_count(workspace):
    return InboxMessage.objects.for_workspace(workspace.id).filter(status=InboxMessage.Status.UNREAD).count()


# --- Search highlighting ---


def search_terms(q):
    """The words of a search query worth highlighting (operators and quotes dropped)."""
    words = re.findall(r"[^\s\"']+", q or "")
    return sorted({w for w in words if w.lower() != "or" and not w.startswith("-")}, key=len, reverse=True)
