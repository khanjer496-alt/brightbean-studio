"""One counting rule per network, the same on the server and in the composer.

Each platform's limit is counted in the unit its own documentation names
(``SocialProvider.text_length_unit``); ``providers.text_counting.text_length`` is the
one server function and ``static/js/caption-count.js`` the composer's copy.
The JS is run through node over the same vectors, so the two cannot drift.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from providers import PROVIDER_REGISTRY, caption_wire_length
from providers.text_counting import UNITS, text_length

FAMILY = "👨‍👩‍👧‍👦"  # man, woman, girl, boy joined by ZWJ: 7 code points, 11 UTF-16 units, 1 grapheme
FLAG = "🇯🇵"  # two regional indicators
THUMBS = "👍🏽"  # emoji + skin tone modifier
CJK = "日本語のテキスト"
ACCENTED = "é"  # e + combining acute: 2 code points, 1 grapheme
LONG_URL = "https://example.com/a/very/long/path/that/goes/on?and=on&on=1"


class TestUnits:
    @pytest.mark.parametrize(
        ("text", "codepoints", "utf16", "utf8_bytes", "graphemes"),
        [
            ("", 0, 0, 0, 0),
            ("hello", 5, 5, 5, 5),
            ("😀", 1, 2, 4, 1),
            (FAMILY, 7, 11, 25, 1),
            (FLAG, 2, 4, 8, 1),
            (THUMBS, 2, 4, 8, 1),
            (ACCENTED, 2, 2, 3, 1),
            (CJK, 8, 8, 24, 8),
            ("Привет", 6, 6, 12, 6),
        ],
    )
    def test_each_unit(self, text, codepoints, utf16, utf8_bytes, graphemes):
        assert text_length(text, unit="codepoints") == codepoints
        assert text_length(text, unit="utf16") == utf16
        assert text_length(text, unit="utf8_bytes") == utf8_bytes
        assert text_length(text, unit="graphemes") == graphemes

    def test_emoji_cost_their_utf8_bytes(self):
        assert text_length("a😀", unit="codepoints_emoji_bytes") == 1 + 4
        assert text_length(FAMILY, unit="codepoints_emoji_bytes") == 25
        assert text_length(CJK, unit="codepoints_emoji_bytes") == 8  # not emoji: one each
        assert text_length("é", unit="codepoints_emoji_bytes") == 1

    def test_every_provider_names_a_known_unit(self):
        for platform, provider_cls in PROVIDER_REGISTRY.items():
            assert provider_cls.text_length_unit in UNITS, platform


class TestEveryNetwork:
    """The rule each network's documentation gives, checked on the strings that tell them apart."""

    def test_bluesky_counts_graphemes_and_links_in_full(self):
        # https://github.com/bluesky-social/atproto/blob/main/lexicons/app/bsky/feed/post.json
        assert caption_wire_length("bluesky", FAMILY * 300) == 300
        assert caption_wire_length("bluesky", FAMILY * 301) == 301
        assert caption_wire_length("bluesky", LONG_URL) == len(LONG_URL)
        assert caption_wire_length("bluesky", ACCENTED * 10) == 10

    def test_bluesky_also_caps_utf8_bytes(self):
        provider = PROVIDER_REGISTRY["bluesky"]
        # 300 family emoji fit the grapheme limit but are 7,500 bytes against the 3,000-byte lexicon cap.
        assert "bytes" in provider.text_issue(FAMILY * 150)
        assert provider.text_issue("a" * 300) == ""

    def test_mastodon_counts_every_link_as_23_and_remote_mentions_as_the_username(self):
        # https://docs.joinmastodon.org/api/guidelines/
        assert caption_wire_length("mastodon", f"read {LONG_URL}") == len("read ") + 23
        assert caption_wire_length("mastodon", "http://a.io") == 23
        assert caption_wire_length("mastodon", "hi @gargron@mastodon.social") == len("hi @gargron")
        assert caption_wire_length("mastodon", "hi @gargron") == len("hi @gargron")
        # Mastodon counts code points, not graphemes.
        assert caption_wire_length("mastodon", FAMILY) == 7

    def test_a_trailing_full_stop_is_not_part_of_the_link(self):
        assert caption_wire_length("mastodon", f"see {LONG_URL}.") == len("see ") + 23 + 1

    def test_x_keeps_its_weighted_rule(self):
        assert caption_wire_length("x", LONG_URL) == 23
        assert caption_wire_length("x", CJK) == 16  # CJK weighs 2
        assert caption_wire_length("x", "😀") == 2

    def test_threads_counts_emoji_by_utf8_bytes(self):
        # https://developers.facebook.com/docs/threads/posts: "Emojis are counted as the number of UTF-8 bytes"
        assert caption_wire_length("threads", "😀" * 125) == 500
        assert caption_wire_length("threads", "😀" * 126) == 504
        assert caption_wire_length("threads", "a" * 500) == 500
        assert caption_wire_length("threads", LONG_URL) == len(LONG_URL)

    def test_tiktok_counts_utf16_units(self):
        # "The maximum length is 2200 in UTF-16 runes"
        assert caption_wire_length("tiktok", "😀") == 2
        assert caption_wire_length("tiktok", "a😀") == 3

    def test_farcaster_counts_utf8_bytes(self):
        assert caption_wire_length("farcaster", "é" * 160) == 320
        assert caption_wire_length("farcaster", "日" * 107) == 321

    def test_youtube_descriptions_are_utf8_bytes(self):
        assert caption_wire_length("youtube", "日" * 100) == 300

    @pytest.mark.parametrize("platform", ["linkedin_personal", "instagram", "facebook", "pinterest", "google_business"])
    def test_the_rest_count_code_points(self, platform):
        assert caption_wire_length(platform, FAMILY) == 7
        assert caption_wire_length(platform, "😀") == 1

    def test_linkedin_still_charges_for_escaped_characters(self):
        assert caption_wire_length("linkedin_personal", "(a)") == 5


class TestComposerParity:
    """The browser's counter (static/js/caption-count.js) gives the server's number."""

    RULES = [
        {"unit": unit, "url_counts_as": url, "mention_domain_counts": mentions}
        for unit in UNITS
        for url in (None, 23)
        for mentions in (True, False)
    ]
    TEXTS = [
        "",
        "hello world",
        "a😀b",
        FAMILY,
        FAMILY * 3 + " end",
        FLAG + FLAG,
        THUMBS,
        ACCENTED * 4,
        CJK,
        f"read {LONG_URL} now",
        f"({LONG_URL}).",
        f"{LONG_URL}, and http://x.io/y!",
        "hi @gargron@mastodon.social and @bob, see https://a.io",
        "email me@example.com or a/@b@c.d",
        "#tag @user@host.tld " + FAMILY,
    ]

    @pytest.fixture
    def browser_counts(self):
        if not shutil.which("node"):
            pytest.skip("node is not installed")
        script = Path(__file__).resolve().parents[2] / "static" / "js" / "caption-count.js"
        program = (
            "const api = require(process.argv[1]);"
            "const {rules, texts} = JSON.parse(require('fs').readFileSync(0, 'utf8'));"
            "console.log(JSON.stringify(rules.map(r => texts.map(t => api.length(t, r)))));"
        )
        result = subprocess.run(
            ["node", "-e", program, str(script)],
            input=json.dumps({"rules": self.RULES, "texts": self.TEXTS}),
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        return json.loads(result.stdout)

    def test_every_rule_and_text_agrees(self, browser_counts):
        for rule, counts in zip(self.RULES, browser_counts, strict=True):
            for text, browser in zip(self.TEXTS, counts, strict=True):
                server = text_length(
                    text,
                    unit=rule["unit"],
                    url_counts_as=rule["url_counts_as"],
                    mention_domain_counts=rule["mention_domain_counts"],
                )
                assert browser == server, f"{rule} {text!r}: browser {browser}, server {server}"
