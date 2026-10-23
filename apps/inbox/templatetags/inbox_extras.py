"""Template helpers for the inbox."""

import re

from django import template
from django.utils.html import escape, format_html
from django.utils.safestring import mark_safe

from apps.inbox.feed import SEARCH_SNIPPET_CHARS, search_terms

register = template.Library()

SNIPPET_LEAD_CHARS = 40


@register.filter
def highlight(text, query):
    """``text`` with the words of the search ``query`` wrapped in ``<mark>``.

    Long text is cut to a snippet around the first match, so the match is
    visible in a one-line preview. Everything but the ``<mark>`` tags is escaped.
    """
    text = str(text or "")
    terms = search_terms(query)
    if not terms:
        return text
    pattern = re.compile("|".join(re.escape(term) for term in terms), re.IGNORECASE)
    first = pattern.search(text)
    if first and len(text) > SEARCH_SNIPPET_CHARS:
        start = max(first.start() - SNIPPET_LEAD_CHARS, 0)
        end = start + SEARCH_SNIPPET_CHARS
        text = ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")
    parts, position = [], 0
    for match in pattern.finditer(text):
        parts.append(escape(text[position : match.start()]))
        parts.append(format_html('<mark class="inbox-mark">{}</mark>', match.group()))
        position = match.end()
    parts.append(escape(text[position:]))
    return mark_safe("".join(parts))
