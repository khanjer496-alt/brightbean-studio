/**
 * Composer inserts: emoji picker, hashtag sets, signatures and @mention
 * suggestions for the caption editor, the per-account overrides and the
 * thread parts.
 *
 * Loaded by composer/compose.html. Each text area gets one `insertToolbar`
 * (templates/composer/partials/insert_toolbar.html). Text goes in at the
 * cursor through setRangeText and an `input` event, so x-model, the live
 * preview and the character counter all see it like typing.
 */
(function () {
    const RECENT_EMOJI_KEY = 'composer:recent-emoji';
    const RECENT_EMOJI_MAX = 16;
    const EMOJI_SEARCH_MAX = 96;
    const MENTION_PATTERN = /(^|\s)@([\w.@-]{1,40})$/;
    const MENTION_DEBOUNCE_MS = 250;
    const MENTION_CACHE_MS = 60000;

    // One fetch of the emoji data serves every toolbar on the page.
    let emojiGroups = null;
    function loadEmoji(url) {
        if (!emojiGroups) {
            emojiGroups = fetch(url, { credentials: 'same-origin' })
                .then((response) => (response.ok ? response.json() : Promise.reject(response.status)))
                .then((data) => data.groups)
                .catch(() => {
                    emojiGroups = null;
                    return [];
                });
        }
        return emojiGroups;
    }

    function readRecentEmoji() {
        try {
            const stored = JSON.parse(localStorage.getItem(RECENT_EMOJI_KEY) || '[]');
            return Array.isArray(stored) ? stored.filter((e) => typeof e === 'string').slice(0, RECENT_EMOJI_MAX) : [];
        } catch (e) {
            return [];
        }
    }

    function writeRecentEmoji(list) {
        try {
            localStorage.setItem(RECENT_EMOJI_KEY, JSON.stringify(list));
        } catch (e) {
            // Private mode or blocked storage: recents just don't persist.
        }
    }

    async function getJSON(url, options) {
        const response = await fetch(url, {
            credentials: 'same-origin',
            headers: { Accept: 'application/json' },
            ...options,
        });
        if (!response.ok) throw new Error(String(response.status));
        return response.json();
    }

    // Suggestions already fetched, so typing on or back does not ask again.
    const mentionCache = new Map();
    let toolbarCount = 0;

    document.addEventListener('alpine:init', () => {
        /**
         * @param {() => HTMLTextAreaElement|null} getTarget  the text area this toolbar writes to
         * @param {() => string[]} getMentionAccounts  account ids to look mentions up on, or [] for plain text
         */
        Alpine.data('insertToolbar', (getTarget, getMentionAccounts) => {
            let mentionTimer = null;
            let mentionRequest = null;

            return {
                uid: 'insert-' + ++toolbarCount,
                urls: {},
                panel: null,

                // Emoji
                groups: [],
                recentEmoji: [],
                emojiQuery: '',
                emojiGroup: 0,
                emojiLoading: false,

                // Hashtags and signatures
                hashtagSets: [],
                recentHashtags: [],
                signatures: [],
                menuLoading: false,
                menuError: false,

                // Mentions
                mentionOpen: false,
                mentionResults: [],
                mentionIndex: 0,
                mentionAnnouncement: '',

                init() {
                    this.urls = {
                        emoji: this.$el.dataset.emojiUrl,
                        hashtags: this.$el.dataset.hashtagsUrl,
                        signatures: this.$el.dataset.signaturesUrl,
                        mentions: this.$el.dataset.mentionsUrl,
                    };
                    // The text area is rendered by the same template pass as this toolbar.
                    this.$nextTick(() => this.bindTarget());
                },

                // ---- Panels -------------------------------------------------

                togglePanel(name) {
                    if (this.panel === name) {
                        this.closePanel();
                        return;
                    }
                    this.closeMentions();
                    this.panel = name;
                    if (name === 'emoji') this.openEmoji();
                    else if (name === 'hashtags') this.openHashtags();
                    else if (name === 'signatures') this.openSignatures();
                },

                closePanel(focusTarget) {
                    const opened = this.panel;
                    this.panel = null;
                    if (!opened) return;
                    if (focusTarget) getTarget()?.focus();
                    else this.$refs[opened + 'Button']?.focus();
                },

                // ---- Insertion ----------------------------------------------

                /**
                 * Put `text` where the cursor is, replacing any selection.
                 * `pad` adds a space before it when it would touch a word;
                 * `block` sets it apart on its own lines.
                 */
                insert(text, { pad = false, block = false } = {}) {
                    const el = getTarget();
                    if (!el) return;
                    const start = el.selectionStart ?? el.value.length;
                    const end = el.selectionEnd ?? start;
                    const before = el.value.slice(0, start);
                    let out = text;
                    if (block && before.trim()) {
                        out = (before.endsWith('\n\n') ? '' : before.endsWith('\n') ? '\n' : '\n\n') + out;
                    } else if (pad && before && !/\s$/.test(before)) {
                        out = ' ' + out;
                    }
                    el.setRangeText(out, start, end, 'end');
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                },

                // ---- Emoji --------------------------------------------------

                async openEmoji() {
                    this.recentEmoji = readRecentEmoji();
                    if (!this.groups.length) {
                        this.emojiLoading = true;
                        this.groups = await loadEmoji(this.urls.emoji);
                        this.emojiLoading = false;
                    }
                    this.$nextTick(() => this.$refs.emojiSearch?.focus());
                },

                get emojiShown() {
                    const query = this.emojiQuery.trim().toLowerCase();
                    if (!query) return this.groups[this.emojiGroup]?.emoji ?? [];
                    const matches = [];
                    for (const group of this.groups) {
                        for (const row of group.emoji) {
                            if (row[1].includes(query) || (row[2] && row[2].includes(query))) matches.push(row);
                            if (matches.length >= EMOJI_SEARCH_MAX) return matches;
                        }
                    }
                    return matches;
                },

                pickEmoji(emoji) {
                    this.insert(emoji);
                    this.recentEmoji = [emoji, ...this.recentEmoji.filter((e) => e !== emoji)].slice(0, RECENT_EMOJI_MAX);
                    writeRecentEmoji(this.recentEmoji);
                },

                emojiName(emoji) {
                    for (const group of this.groups) {
                        const row = group.emoji.find((r) => r[0] === emoji);
                        if (row) return row[1];
                    }
                    return emoji;
                },

                // Arrow keys move through the grid; Down from the search box enters it.
                emojiKey(event) {
                    const keys = ['ArrowRight', 'ArrowLeft', 'ArrowDown', 'ArrowUp', 'Home', 'End'];
                    if (!keys.includes(event.key)) return;
                    const buttons = [...this.$refs.emojiPanel.querySelectorAll('[data-emoji]')];
                    if (!buttons.length) return;
                    const at = buttons.indexOf(document.activeElement);
                    const columns = 8;
                    let next;
                    if (at === -1) {
                        if (event.key !== 'ArrowDown') return;
                        next = 0;
                    } else if (event.key === 'ArrowRight') next = at + 1;
                    else if (event.key === 'ArrowLeft') next = at - 1;
                    else if (event.key === 'ArrowDown') next = at + columns;
                    else if (event.key === 'ArrowUp') next = at - columns;
                    else if (event.key === 'Home') next = 0;
                    else next = buttons.length - 1;
                    event.preventDefault();
                    if (next < 0) this.$refs.emojiSearch?.focus();
                    else buttons[Math.min(next, buttons.length - 1)].focus();
                },

                // ---- Hashtags and signatures --------------------------------

                async openHashtags() {
                    this.menuLoading = true;
                    this.menuError = false;
                    try {
                        const data = await getJSON(this.urls.hashtags);
                        this.hashtagSets = data.sets;
                        this.recentHashtags = data.recent;
                    } catch (e) {
                        this.menuError = true;
                    }
                    this.menuLoading = false;
                },

                async openSignatures() {
                    this.menuLoading = true;
                    this.menuError = false;
                    try {
                        this.signatures = (await getJSON(this.urls.signatures)).signatures;
                    } catch (e) {
                        this.menuError = true;
                    }
                    this.menuLoading = false;
                },

                pickHashtags(text) {
                    this.insert(text, { pad: true });
                    this.closePanel(true);
                },

                // A signature already closing the text is not added a second time.
                pickSignature(signature) {
                    const el = getTarget();
                    const body = signature.body.trim();
                    if (el && body && !el.value.trimEnd().endsWith(body)) this.insert(signature.body, { block: true });
                    this.closePanel(true);
                },

                // ---- @mentions ----------------------------------------------

                bindTarget() {
                    const el = getTarget();
                    if (!el || el.dataset.insertBound) return;
                    el.dataset.insertBound = '1';
                    el.addEventListener('input', () => this.onInput(el));
                    el.addEventListener('keydown', (event) => this.onKeydown(event));
                    el.addEventListener('blur', () => this.closeMentions());
                    el.addEventListener('click', () => this.closeMentions());
                },

                onInput(el) {
                    const accounts = getMentionAccounts();
                    const typed = accounts.length ? MENTION_PATTERN.exec(el.value.slice(0, el.selectionStart)) : null;
                    if (!typed) {
                        this.closeMentions();
                        return;
                    }
                    clearTimeout(mentionTimer);
                    mentionTimer = setTimeout(() => this.searchMentions(typed[2], accounts), MENTION_DEBOUNCE_MS);
                },

                async searchMentions(query, accounts) {
                    const key = accounts.join(',') + '|' + query.toLowerCase();
                    const cached = mentionCache.get(key);
                    let results;
                    if (cached && Date.now() - cached.at < MENTION_CACHE_MS) {
                        results = cached.results;
                    } else {
                        mentionRequest?.abort();
                        mentionRequest = new AbortController();
                        const params = new URLSearchParams({ q: query });
                        accounts.forEach((id) => params.append('account', id));
                        try {
                            results = (await getJSON(this.urls.mentions + '?' + params, { signal: mentionRequest.signal })).results;
                        } catch (e) {
                            // Suggestions are a nicety: a failure leaves the @ as plain text.
                            return;
                        }
                        mentionCache.set(key, { at: Date.now(), results });
                    }
                    this.mentionResults = results;
                    this.mentionIndex = 0;
                    this.setMentionOpen(results.length > 0);
                    this.mentionAnnouncement = results.length
                        ? results.length + ' accounts to mention. Use the arrow keys to choose.'
                        : '';
                },

                setMentionOpen(open) {
                    this.mentionOpen = open;
                    const el = getTarget();
                    if (!el) return;
                    el.setAttribute('aria-autocomplete', 'list');
                    el.setAttribute('aria-expanded', open ? 'true' : 'false');
                    if (open) {
                        el.setAttribute('aria-controls', this.uid + '-mentions');
                        el.setAttribute('aria-activedescendant', this.uid + '-mention-' + this.mentionIndex);
                    } else {
                        el.removeAttribute('aria-activedescendant');
                    }
                },

                closeMentions() {
                    clearTimeout(mentionTimer);
                    mentionRequest?.abort();
                    if (this.mentionOpen) this.setMentionOpen(false);
                },

                moveMention(step) {
                    const count = this.mentionResults.length;
                    this.mentionIndex = (this.mentionIndex + step + count) % count;
                    getTarget()?.setAttribute('aria-activedescendant', this.uid + '-mention-' + this.mentionIndex);
                },

                onKeydown(event) {
                    if (!this.mentionOpen) return;
                    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
                        event.preventDefault();
                        this.moveMention(event.key === 'ArrowDown' ? 1 : -1);
                    } else if (event.key === 'Enter' || event.key === 'Tab') {
                        event.preventDefault();
                        this.pickMention(this.mentionResults[this.mentionIndex]);
                    } else if (event.key === 'Escape') {
                        event.preventDefault();
                        event.stopPropagation();
                        this.closeMentions();
                    }
                },

                pickMention(item) {
                    const el = getTarget();
                    if (!el || !item) return;
                    const caret = el.selectionStart;
                    const typed = MENTION_PATTERN.exec(el.value.slice(0, caret));
                    this.closeMentions();
                    if (!typed) return;
                    el.setRangeText('@' + item.handle + ' ', caret - typed[2].length - 1, caret, 'end');
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.focus();
                },
            };
        });
    });
})();
