/**
 * Composer unsaved-changes guard.
 *
 * Loaded by composer/compose.html. Active when #composer-form carries data-unsaved-guard (an editable post).
 * "Unsaved" means the form's values differ from the last state the server has: the page as loaded (taken just
 * before the first interaction), or what the last successful autosave / explicit save sent. So an autosave that
 * just ran, or an explicit save / schedule, leaves nothing to warn about.
 *
 *  - Closing the tab, reloading or typing a new address: the browser's own beforeunload prompt.
 *  - Following a link in the app (plain or htmx-boosted): the shared confirm dialog (confirm-dialog.js) with
 *    "Stay" / "Leave page"; Leave carries on to the link.
 */
(function (root) {
    // Fields that change without the user editing anything (tokens, ids the server hands back, the pressed button).
    const IGNORED = ['csrfmiddlewaretoken', '_autosave_post_id', 'action'];

    // Stable string for a list of [name, value] pairs; files count by name and size.
    function serialize(entries) {
        return JSON.stringify(entries
            .filter(([name]) => IGNORED.indexOf(name) === -1)
            .map(([name, value]) => (typeof value === 'string' ? [name, value] : [name, 'file:' + value.name + ':' + value.size])));
    }

    // The link a click should be guarded for, or null (new tab, download, same-page hash, other origin, modified click).
    function guardedHref(link, event, location) {
        if (!link || !link.getAttribute) return null;
        const raw = link.getAttribute('href');
        if (!raw || raw.charAt(0) === '#' || /^(javascript|mailto|tel):/i.test(raw)) return null;
        if (link.hasAttribute('download') || (link.target && link.target !== '_self')) return null;
        if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button) return null;
        if (link.origin !== location.origin) return null;
        if (link.pathname === location.pathname && link.search === location.search && link.hash) return null;
        return link.href;
    }

    // release(): the page is about to leave on purpose (the post was deleted), so stop guarding it.
    let released = false;
    root.composerUnsaved = { serialize: serialize, guardedHref: guardedHref, release: function () { released = true; } };
    if (typeof document === 'undefined') return;

    document.addEventListener('DOMContentLoaded', function () {
        const form = document.getElementById('composer-form');
        if (!form || !form.hasAttribute('data-unsaved-guard')) return;

        const current = () => serialize(Array.from(new FormData(form).entries()));
        let baseline = null;     // serialized state the server has
        let leaving = false;     // a confirmed in-app navigation is under way
        const captureBaseline = () => { if (baseline === null) baseline = current(); };
        const dirty = () => baseline !== null && current() !== baseline;

        // The loaded page is the baseline once Alpine has rendered its hidden inputs, or just before the first
        // interaction (capture phase, so the edit has not landed yet), whichever comes first.
        setTimeout(captureBaseline, 1500);
        ['keydown', 'pointerdown', 'input', 'change'].forEach((type) => form.addEventListener(type, captureBaseline, true));

        // What each save request carries is what the server will have once it succeeds.
        const sent = new WeakMap();
        // Capture phase: the form's own after-request handler redirects after a schedule, and the
        // browser may fire beforeunload inside that assignment, so the baseline must already be updated.
        document.addEventListener('htmx:beforeRequest', function (event) {
            const elt = event.detail.elt;
            if (elt === form || (elt && elt.id === 'autosave-trigger')) sent.set(elt, current());
        }, true);
        document.addEventListener('htmx:afterRequest', function (event) {
            const elt = event.detail.elt;
            if (event.detail.successful && sent.has(elt)) { baseline = sent.get(elt); sent.delete(elt); }
        }, true);

        // An htmx swap inside the form (template picker, best-time chips) is not an edit: when the form was clean
        // before the request, whatever the swap brought in is part of the saved state too.
        const cleanBefore = new WeakMap();
        document.addEventListener('htmx:beforeSwap', function (event) {
            const target = event.detail.target;
            if (target && form.contains(target)) cleanBefore.set(target, !dirty());
        });
        document.addEventListener('htmx:afterSettle', function (event) {
            const target = event.detail.target;
            if (target && cleanBefore.get(target) && baseline !== null) baseline = current();
            if (target) cleanBefore.delete(target);
        });

        window.addEventListener('beforeunload', function (event) {
            if (leaving || released || !dirty()) return;
            event.preventDefault();
            event.returnValue = '';
        });

        document.addEventListener('click', function (event) {
            const link = event.target.closest ? event.target.closest('a[href]') : null;
            const href = guardedHref(link, event, window.location);
            if (!href || leaving || released || !dirty() || !root.pdConfirm) return;
            event.preventDefault();
            event.stopPropagation();
            root.pdConfirm.ask({
                title: 'Leave without saving?',
                message: 'You have changes that are not saved yet. If you leave now they will be lost.',
                label: 'Leave page',
                cancel: 'Stay',
                variant: 'danger'
            }).then(function (ok) {
                if (!ok) return;
                leaving = true;
                window.location.assign(href);
            });
        }, true);
    });
}(typeof window !== 'undefined' ? window : globalThis));
