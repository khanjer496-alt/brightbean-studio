/**
 * Inbox keyboard triage and the "/" saved-reply picker.
 *
 * Loaded by inbox/feed.html. Triage keys act on the focused row of the
 * message list and are ignored while typing:
 *
 *   j / k  next / previous message     Enter  open       x  select
 *   e      resolve (archive if done)   u      mark unread
 *   a      assign to me                r      focus the reply box
 *   ?      show the shortcuts
 *
 * Single-key shortcuts can be switched off in the shortcuts dialog (WCAG
 * 2.1.4), for people whose speech or switch input types letters.
 * Rows answer to the server (`data-triage-url`) and are swapped in place, so
 * the list keeps its scroll position and focus. The "/" picker lives on the
 * reply box (`data-saved-replies`): typing "/" at the start of a word lists
 * the saved replies, filtered as you type.
 */
(function (root) {
    const KEYS = {
        j: 'next', k: 'prev', Enter: 'open', ' ': 'open', e: 'resolve', r: 'reply', a: 'assign_me', u: 'unread', x: 'select', '?': 'help',
    };
    const NON_TEXT_INPUTS = ['checkbox', 'radio', 'button', 'submit', 'reset', 'range', 'color', 'file', 'image'];
    const PICKER_MAX = 8;
    const SLASH_PATTERN = /(^|\s)\/([^\s/]*)$/;
    const SHORTCUTS_OFF_KEY = 'inbox:shortcuts-off';

    function shortcutsEnabled() {
        try {
            return root.localStorage.getItem(SHORTCUTS_OFF_KEY) !== '1';
        } catch (e) {
            return true;
        }
    }

    function setShortcutsEnabled(enabled) {
        try {
            if (enabled) root.localStorage.removeItem(SHORTCUTS_OFF_KEY);
            else root.localStorage.setItem(SHORTCUTS_OFF_KEY, '1');
        } catch (e) {
            /* Storage blocked: the switch lasts until the page is left. */
        }
    }

    function isTypingTarget(el) {
        if (!el) return false;
        const tag = String(el.tagName || '').toUpperCase();
        if (el.isContentEditable || tag === 'TEXTAREA' || tag === 'SELECT') return true;
        return tag === 'INPUT' && !NON_TEXT_INPUTS.includes(String(el.type || 'text').toLowerCase());
    }

    /** The triage action a keydown stands for, or null when it should be left alone. */
    function triageAction(event) {
        if (event.ctrlKey || event.metaKey || event.altKey || isTypingTarget(event.target)) return null;
        const key = event.key && event.key.length === 1 ? event.key.toLowerCase() : event.key;
        if (event.shiftKey && key !== '?') return null;
        return Object.prototype.hasOwnProperty.call(KEYS, key) ? KEYS[key] : null;
    }

    /** Index of the row j/k lands on; -1 when there are no rows. */
    function stepIndex(current, length, delta) {
        if (!length) return -1;
        if (current < 0) return delta > 0 ? 0 : length - 1;
        return Math.min(Math.max(current + delta, 0), length - 1);
    }

    function createTriage(doc, htmx, enabled = () => true) {
        const rows = () => Array.from(doc.querySelectorAll('#inbox-message-list .inbox-row'));
        const currentRow = () => {
            const focused = doc.activeElement && doc.activeElement.closest && doc.activeElement.closest('.inbox-row');
            return focused || doc.querySelector('#inbox-message-list .inbox-row.is-active');
        };

        function focusRow(row) {
            row.focus();
            if (row.scrollIntoView) row.scrollIntoView({ block: 'nearest' });
        }

        function post(row, action) {
            const wasActive = row.classList.contains('is-active');
            const id = row.id;
            const done = htmx.ajax('POST', row.dataset.triageUrl, { target: row, swap: 'outerHTML', values: { action } });
            return Promise.resolve(done).then(() => {
                const fresh = doc.getElementById(id);
                if (!fresh) return;
                if (wasActive) {
                    fresh.classList.add('is-active');
                    if (action !== 'unread' && fresh.dataset.detailUrl) {
                        htmx.ajax('GET', fresh.dataset.detailUrl, { target: '#inbox-detail-panel', swap: 'innerHTML' });
                    }
                }
                focusRow(fresh);
            });
        }

        function onKeydown(event) {
            if (doc.querySelector('dialog[open]') || !enabled()) return;
            const action = triageAction(event);
            if (!action) return;
            const list = rows();
            const row = currentRow();
            if (action === 'help') {
                const dialog = doc.getElementById('inbox-shortcuts');
                if (dialog && dialog.showModal) {
                    event.preventDefault();
                    dialog.showModal();
                }
                return;
            }
            if (action === 'next' || action === 'prev') {
                const target = list[stepIndex(list.indexOf(row), list.length, action === 'next' ? 1 : -1)];
                if (target) {
                    event.preventDefault();
                    focusRow(target);
                }
                return;
            }
            if (action === 'reply') {
                const box = doc.getElementById('inbox-reply-body');
                if (box) {
                    event.preventDefault();
                    box.focus();
                }
                return;
            }
            if (!row) return;
            // Enter and Space only open a row that has focus itself, never a button or link inside it.
            if (action === 'open') {
                if (doc.activeElement !== row) return;
                event.preventDefault();
                row.click();
            } else if (action === 'select') {
                const box = row.querySelector('input[type=checkbox]');
                if (box) {
                    event.preventDefault();
                    box.click();
                }
            } else {
                event.preventDefault();
                post(row, action);
            }
        }

        return { onKeydown, rows, currentRow };
    }

    /** `{start, query}` when the caret sits in a "/word" at the start of a word, else null. */
    function slashQuery(value, caret) {
        const match = SLASH_PATTERN.exec(value.slice(0, caret));
        return match ? { start: match.index + match[1].length, query: match[2] } : null;
    }

    function filterReplies(replies, query) {
        const needle = query.toLowerCase();
        const hits = replies.filter(
            (r) => !needle || r.title.toLowerCase().includes(needle) || r.body.toLowerCase().includes(needle),
        );
        const starts = (r) => (r.title.toLowerCase().startsWith(needle) ? 0 : 1);
        return hits.sort((a, b) => starts(a) - starts(b)).slice(0, PICKER_MAX);
    }

    /** Pick-list behaviour for one reply box; `list` is the listbox element beside it. */
    function createSlashPicker(doc, textarea, list, replies) {
        let match = null;
        let shown = [];
        let active = 0;

        function close() {
            match = null;
            shown = [];
            list.hidden = true;
            textarea.setAttribute('aria-expanded', 'false');
            textarea.removeAttribute('aria-activedescendant');
        }

        function render() {
            list.textContent = '';
            shown.forEach((reply, index) => {
                const item = doc.createElement('li');
                item.id = `${list.id}-opt-${index}`;
                item.setAttribute('role', 'option');
                item.setAttribute('aria-selected', index === active ? 'true' : 'false');
                item.className = 'inbox-slash-option';
                const title = doc.createElement('span');
                title.className = 'inbox-slash-title';
                title.textContent = reply.title;
                const body = doc.createElement('span');
                body.className = 'inbox-slash-body';
                body.textContent = reply.body;
                item.append(title, body);
                item.addEventListener('mousedown', (event) => {
                    event.preventDefault();
                    choose(index);
                });
                list.append(item);
            });
            textarea.setAttribute('aria-activedescendant', `${list.id}-opt-${active}`);
        }

        function choose(index) {
            const reply = shown[index];
            if (!reply || !match) return;
            textarea.setRangeText(reply.body, match.start, textarea.selectionStart, 'end');
            close();
            textarea.dispatchEvent(new root.Event('input', { bubbles: true }));
        }

        function update() {
            match = slashQuery(textarea.value, textarea.selectionStart);
            shown = match ? filterReplies(replies, match.query) : [];
            if (!shown.length) return close();
            active = 0;
            list.hidden = false;
            textarea.setAttribute('aria-expanded', 'true');
            render();
        }

        /** True when the key was the picker's. */
        function onKeydown(event) {
            if (list.hidden) return false;
            if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
                active = (active + (event.key === 'ArrowDown' ? 1 : shown.length - 1)) % shown.length;
                render();
            } else if (event.key === 'Enter' || event.key === 'Tab') {
                choose(active);
            } else if (event.key === 'Escape') {
                close();
            } else {
                return false;
            }
            event.preventDefault();
            return true;
        }

        textarea.addEventListener('input', update);
        textarea.addEventListener('keydown', onKeydown);
        textarea.addEventListener('blur', close);
        return { update, onKeydown, close };
    }

    function initSlashPickers(doc) {
        doc.querySelectorAll('textarea[data-saved-replies]').forEach((textarea) => {
            if (textarea.dataset.slashReady) return;
            const source = doc.getElementById(textarea.dataset.savedReplies);
            const list = doc.getElementById(textarea.getAttribute('aria-controls'));
            if (!source || !list) return;
            textarea.dataset.slashReady = '1';
            createSlashPicker(doc, textarea, list, JSON.parse(source.textContent));
        });
    }

    /**
     * Keep keyboard focus in the list when the control that had it is swapped
     * away: the "N new" pill, "Load more" and the bulk bar all replace or hide
     * themselves. Focus goes to the first row the swap brought in. It is never
     * taken from anything that still holds it (the search box, for one).
     */
    function keepFocusInList(doc) {
        const rows = () => Array.from(doc.querySelectorAll('#inbox-message-list .inbox-row'));
        let pending = null;
        doc.addEventListener('htmx:beforeRequest', (event) => {
            const elt = event.detail && event.detail.elt;
            const active = doc.activeElement;
            if (!elt || !active || active === doc.body || !elt.contains(active)) return;
            if (!elt.closest('#inbox-message-list, .bulk-bar')) return;
            // A poll replaces the pill with a fresh one: focus stays on the pill.
            const poll = elt.id === 'inbox-poll';
            pending = { poll, first: elt.classList.contains('inbox-load-more') ? rows().length : 0 };
        });
        doc.addEventListener('htmx:afterSettle', () => {
            if (!pending) return;
            const { poll, first } = pending;
            pending = null;
            const active = doc.activeElement;
            const lost = !active || active === doc.body || !active.isConnected || active.offsetParent === null;
            if (!lost) return;
            const target = poll ? doc.querySelector('#inbox-poll .inbox-new-pill') : rows()[first];
            if (target) target.focus();
        });
    }

    function initShortcutsSwitch(doc) {
        const box = doc.getElementById('inbox-shortcuts-enabled');
        if (!box) return;
        box.checked = shortcutsEnabled();
        box.addEventListener('change', () => setShortcutsEnabled(box.checked));
    }

    const api = { isTypingTarget, triageAction, stepIndex, slashQuery, filterReplies, createTriage, createSlashPicker };
    root.inboxKeyboard = api;

    if (root.document) {
        const doc = root.document;
        doc.addEventListener(
            'keydown',
            createTriage(doc, { ajax: (...args) => root.htmx.ajax(...args) }, shortcutsEnabled).onKeydown,
        );
        keepFocusInList(doc);
        initShortcutsSwitch(doc);
        // The reply box is swapped in with every opened message.
        doc.addEventListener('htmx:afterSettle', () => initSlashPickers(doc));
        initSlashPickers(doc);
    }
})(typeof window !== 'undefined' ? window : globalThis);
