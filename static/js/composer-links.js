/**
 * Composer short links: how links count against a limit once they are shortened, and when to ask.
 *
 * Loaded by composer/compose.html. Links are shortened on the server when a post is scheduled or sent for
 * review (one short link per account); here the live counter only has to know their length, and the "ask"
 * preference has to decide whether to put the question to the author before scheduling or submitting. Pure functions, no DOM, so they are
 * tested with node (tests/frontend/composer-links.test.cjs).
 */
(function (root) {
    const URL_PATTERN = /https?:\/\/[^\s<>"]+/gi;
    const TRAILING = '.,!?:;)]}\'’”';
    // Links are rewritten when a post is scheduled or sent for review (so reviewers approve the final links).
    const SCHEDULING_ACTIONS = ['schedule', 'publish_now', 'add_to_queue', 'add_to_queue_priority', 'submit_for_approval', 'resubmit_for_approval'];

    function trimUrl(url) {
        let end = url.length;
        while (end > 0 && TRAILING.indexOf(url[end - 1]) !== -1) end--;
        return url.slice(0, end);
    }

    // Every address in the text, minus the punctuation that ends a sentence.
    function findUrls(text) {
        const found = [];
        for (const match of (text || '').matchAll(URL_PATTERN)) {
            const url = trimUrl(match[0]);
            if (url && found.indexOf(url) === -1) found.push(url);
        }
        return found;
    }

    function isShort(url, shortPrefix) {
        return !!shortPrefix && url.indexOf(shortPrefix) === 0;
    }

    // Whether links will be shortened for this post: the author's choice ('1' or '0'), else the workspace default.
    function shortenActive(mode, choice) {
        if (mode !== 'always' && mode !== 'ask') return false;
        if (choice === '1') return true;
        if (choice === '0') return false;
        return mode === 'always';
    }

    // The text as it will count once its links are short: each address swapped for one of the short length.
    function countingText(text, shortLength, shortPrefix) {
        if (!text || !shortLength) return text || '';
        const placeholder = 'https://' + 'x'.repeat(Math.max(shortLength - 8, 1));
        return text.replace(URL_PATTERN, (raw) => {
            const url = trimUrl(raw);
            if (isShort(url, shortPrefix)) return raw;
            return placeholder + raw.slice(url.length);
        });
    }

    // Postiz's prompt: in "ask" mode, scheduling a post that holds a link nobody has decided about asks first.
    function needsPrompt(options) {
        const { mode, choice, texts, action, shortPrefix } = options;
        if (mode !== 'ask' || choice !== '' || SCHEDULING_ACTIONS.indexOf(action) === -1) return false;
        return (texts || []).some((text) => findUrls(text).some((url) => !isShort(url, shortPrefix)));
    }

    const api = { findUrls, shortenActive, countingText, needsPrompt, SCHEDULING_ACTIONS };
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
    root.composerLinks = api;
})(typeof window !== 'undefined' ? window : globalThis);
