"""How a platform counts a caption against its limit.

One server function, ``text_length``, behind every platform's ``caption_length``. The
composer's live counter (``static/js/caption-count.js``) implements the same rules in
JavaScript and ``tests/providers/test_text_counting.py`` runs both over the same
vectors, so the number the user sees is the number the server enforces.

Units (``SocialProvider.text_length_unit``):

* ``codepoints``: one per Unicode code point (Python ``len``).
* ``utf16``: one per UTF-16 code unit (TikTok: "UTF-16 runes"; astral characters cost 2).
* ``utf8_bytes``: one per UTF-8 byte (Farcaster's 320-byte cast).
* ``graphemes``: one per user-perceived character (Bluesky: an emoji, a ZWJ family or a
  flag is 1). Segmented with ``\\X``, the same Unicode algorithm as ``Intl.Segmenter``.
* ``codepoints_emoji_bytes``: code points, except that an emoji costs its UTF-8 byte
  length (Threads: "Emojis are counted as the number of UTF-8 bytes").
"""

from __future__ import annotations

import regex

UNITS = ("codepoints", "utf16", "utf8_bytes", "graphemes", "codepoints_emoji_bytes")

# An http(s) link as the platforms that weigh links find one: a scheme is required, and
# trailing sentence punctuation is not part of the link. Mirrored in caption-count.js.
URL_RE = regex.compile(r"(?<![=/\w])https?://[^\s<>]*[^\s<>.,;:!?'\")\]}]")

# ``@user@remote.example`` counts as ``@user`` on Mastodon.
MENTION_RE = regex.compile(r"(?<![=/\w])@([A-Za-z0-9_]+(?:[A-Za-z0-9_.-]+[A-Za-z0-9_]+)?)(?:@[\w.-]+\w)?")

# ``#tag`` the way the platforms that cap hashtags find one: a hash that starts a word and
# is followed by at least one letter, so "#1" and "C#" are not tags.
HASHTAG_RE = regex.compile(r"(?<![\w&#/])#(?=[\w]*[^\W\d_])\w+")

_GRAPHEME_RE = regex.compile(r"\X")
# What the "emoji cost their bytes" rule treats as an emoji. Mirrored in caption-count.js.
_EMOJI_RE = regex.compile(r"[\p{Emoji_Presentation}️\U0001F000-\U0001FAFF]")


def count_graphemes(text: str) -> int:
    return len(_GRAPHEME_RE.findall(text))


def utf8_length(text: str) -> int:
    return len(text.encode("utf-8"))


def find_urls(text: str) -> list[str]:
    """The links in ``text``, in order (repeats kept)."""
    return [match.group(0) for match in URL_RE.finditer(text or "")]


def _count(text: str, unit: str) -> int:
    if unit == "utf16":
        return len(text.encode("utf-16-le")) // 2
    if unit == "utf8_bytes":
        return utf8_length(text)
    if unit == "graphemes":
        return count_graphemes(text)
    if unit == "codepoints_emoji_bytes":
        return sum(utf8_length(g) if _EMOJI_RE.search(g) else len(g) for g in _GRAPHEME_RE.findall(text))
    return len(text)


def text_length(
    text: str,
    *,
    unit: str = "codepoints",
    url_counts_as: int | None = None,
    mention_domain_counts: bool = True,
) -> int:
    """``text``'s length under a platform's rule.

    ``url_counts_as`` makes every link cost that many characters whatever its length;
    ``mention_domain_counts=False`` drops the ``@domain`` part of a remote mention.
    """
    text = text or ""
    if not mention_domain_counts:
        text = MENTION_RE.sub(lambda match: "@" + match.group(1), text)
    if url_counts_as is None:
        return _count(text, unit)
    total = 0
    position = 0
    for match in URL_RE.finditer(text):
        total += _count(text[position : match.start()], unit) + url_counts_as
        position = match.end()
    return total + _count(text[position:], unit)
