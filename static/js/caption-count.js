// How a platform counts a caption against its limit — the composer's side of
// providers/text_counting.py. tests/providers/test_text_counting.py runs both over the
// same vectors, so the counter and the server cannot drift apart.
//
// rule = { unit, url_counts_as, mention_domain_counts } as the composer receives it per
// account (apps/composer/views.py, "char_limits"). Units: codepoints, utf16, utf8_bytes,
// graphemes, codepoints_emoji_bytes.
(function (root) {
    'use strict';

    const URL_RE = /(?<![=/\p{L}\p{N}_])https?:\/\/[^\s<>]*[^\s<>.,;:!?'")\]}]/gu;
    const MENTION_RE = /(?<![=/\p{L}\p{N}_])@([A-Za-z0-9_]+(?:[A-Za-z0-9_.-]+[A-Za-z0-9_]+)?)(?:@[\p{L}\p{N}_.-]+[\p{L}\p{N}_])/gu;
    const EMOJI_RE = /[\p{Emoji_Presentation}️\u{1F000}-\u{1FAFF}]/u;
    const encoder = new TextEncoder();

    let segmenter = null;
    function graphemes(text) {
        if (!segmenter) segmenter = new Intl.Segmenter(undefined, { granularity: 'grapheme' });
        return Array.from(segmenter.segment(text), (part) => part.segment);
    }

    function count(text, unit) {
        if (unit === 'utf16') return text.length;
        if (unit === 'utf8_bytes') return encoder.encode(text).length;
        if (unit === 'graphemes') return graphemes(text).length;
        if (unit === 'codepoints_emoji_bytes') {
            let total = 0;
            for (const cluster of graphemes(text)) {
                total += EMOJI_RE.test(cluster) ? encoder.encode(cluster).length : Array.from(cluster).length;
            }
            return total;
        }
        return Array.from(text).length;
    }

    function length(text, rule) {
        rule = rule || {};
        const unit = rule.unit || 'codepoints';
        text = text || '';
        if (rule.mention_domain_counts === false) text = text.replace(MENTION_RE, '@$1');
        const urlCost = rule.url_counts_as;
        if (urlCost === null || urlCost === undefined) return count(text, unit);
        let total = 0;
        let position = 0;
        for (const match of text.matchAll(URL_RE)) {
            total += count(text.slice(position, match.index), unit) + urlCost;
            position = match.index + match[0].length;
        }
        return total + count(text.slice(position), unit);
    }

    const api = { length };
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
    else root.pdCaptionCount = api;
})(typeof window !== 'undefined' ? window : globalThis);
