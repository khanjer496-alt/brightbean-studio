"""Queries behind the inbox feed: filters, cursor pagination, search and polling.

The feed view, the "new messages" poll and the bulk actions all read the same
filter parameters, so they go through ``filtered_messages`` and can't disagree
about what "the messages matching this filter" means.

Pages are cut by a cursor on ``(received_at, id)`` rather than an offset, so
messages arriving (or being resolved out of the filter) while someone scrolls
never shift a row onto two pages or skip one.

The poll counts by ``created_at`` (when the row reached us), not
``received_at``: the scheduled syncs fetch comments with a look-back window,
so a message can arrive minutes after one with a later ``received_at`` is
already at the top of the list.
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
from django.utils import timezone

from .models import InboxMessage

MESSAGES_PER_PAGE = 50
# The "N new messages" pill never counts past this; "99+" is as useful as the exact number.
NEW_MESSAGES_CAP = 100
SEARCH_CONFIG = "simple"
# A query of plain words is matched as word prefixes ("refund" finds "refunds",
# "refu" finds both); anything else goes through ``websearch`` syntax.
PLAIN_SEARCH = re.compile(r"[^\W_]+(?:\s+[^\W_]+)*")
SEARCH_SNIPPET_CHARS = 140

# Poll every POLL_SECONDS; after POLL_IDLE_AFTER polls in a row with nothing
# new, every POLL_IDLE_SECONDS until something arrives.
POLL_SECONDS = 20
POLL_IDLE_SECONDS = 60
POLL_IDLE_AFTER = 15

# Query parameters that define "the messages matching this filter".
FILTER_KEYS = ("view", "platform", "account", "type", "status", "assigned", "sentiment", "date_from", "date_to", "q")


def search_vector():
    """The expression the full-text GIN index is built on (see migration 0007)."""
    return SearchVector("body", "sender_name", "sender_handle", config=SEARCH_CONFIG)


def _values(params, key):
    return [v for v in params.getlist(key) if v]


def search_query(q):
    """The full-text query for ``q``: word prefixes for plain words, else ``websearch`` syntax."""
    if PLAIN_SEARCH.fullmatch(q) and "or" not in q.lower().split():
        # Letters and digits only, so nothing in a word can be tsquery syntax.
        return SearchQuery(" & ".join(f"{word}:*" for word in q.split()), config=SEARCH_CONFIG, search_type="raw")
    return SearchQuery(q, config=SEARCH_CONFIG, search_type="websearch")


def apply_search(qs, q):
    """Full-text match on message text and author, falling back to a substring match.

    Full-text matching works on whole words (or word prefixes), so text inside
    a word ("fund" in "refund"), a query of punctuation or a non-Postgres
    database would find nothing; then the old substring match answers instead.
    """
    if connection.vendor == "postgresql":
        matched = qs.alias(search=search_vector()).filter(search=search_query(q))
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


def _encode(raw):
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()


def _aware(stamp):
    moment = datetime.fromisoformat(stamp)
    if moment.tzinfo is None:
        raise ValueError("naive timestamp")
    return moment


def encode_cursor(message):
    return _encode(f"{message.received_at.isoformat()}|{message.id}")


def decode_cursor(value):
    """``(received_at, id)`` for a cursor from ``encode_cursor``, or None when it is missing or malformed."""
    if not value:
        return None
    try:
        stamp, _, message_id = _decode(value).partition("|")
        return _aware(stamp), uuid.UUID(message_id)
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None


def encode_since(moment):
    """The poll's ``since`` token: the moment the list was rendered."""
    return _encode(moment.isoformat())


def decode_since(value):
    """The moment in a ``since`` token from ``encode_since``, or None when it is missing or malformed."""
    if not value:
        return None
    try:
        return _aware(_decode(value))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None


def older_than(qs, cursor):
    """Rows that sort after ``cursor`` in the feed's newest-first order."""
    received_at, message_id = cursor
    # The redundant ``<=`` bound is what lets the index seek to the cursor
    # instead of reading (and discarding) every newer row first.
    return qs.filter(received_at__lte=received_at).filter(Q(received_at__lt=received_at) | Q(id__lt=message_id))


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
        "poll_query": urlencode([*pairs, ("since", encode_since(timezone.now()))]),
        "poll_seconds": POLL_SECONDS,
        "active_filters": active_filters,
        "current_view": view,
    }


def new_message_count(workspace, params, user, since_value):
    """How many messages matching the filter reached us after the ``since`` token (capped).

    An empty or unreadable token counts from the start of time.
    """
    qs, _active, _view = filtered_messages(workspace, params, user)
    since = decode_since(since_value)
    if since:
        qs = qs.filter(created_at__gt=since)
    return len(qs.order_by().values_list("id", flat=True)[:NEW_MESSAGES_CAP])


def poll_backoff(params, count):
    """``(idle, seconds)``: polls in a row that found the same count, and the next interval."""
    try:
        seen, idle = int(params.get("seen", "")), int(params.get("idle", ""))
    except ValueError:
        return 0, POLL_SECONDS
    idle = idle + 1 if count == seen else 0
    return idle, POLL_IDLE_SECONDS if idle >= POLL_IDLE_AFTER else POLL_SECONDS


def unread_count(workspace):
    return InboxMessage.objects.for_workspace(workspace.id).filter(status=InboxMessage.Status.UNREAD).count()


# --- Search highlighting ---


def search_terms(q):
    """The words of a search query worth highlighting (operators and quotes dropped)."""
    words = re.findall(r"[^\s\"']+", q or "")
    return sorted({w for w in words if w.lower() != "or" and not w.startswith("-")}, key=len, reverse=True)
