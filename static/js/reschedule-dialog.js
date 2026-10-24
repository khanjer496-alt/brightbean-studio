/**
 * "Reschedule…" dialog for calendar post chips: the keyboard and touch alternative to dragging.
 *
 * Loaded by calendar/calendar.html. A chip's button fetches the form into #reschedule-dialog-body
 * (htmx), this script opens the native <dialog> (focus trap, Escape and focus return come from the
 * browser), posts the form to the same reschedule endpoint the drag-and-drop uses, then asks the
 * calendar to reload. Every rule (permissions, approval, publishability) is enforced by the server.
 */
(function (root) {
    const DIALOG_ID = 'reschedule-dialog';
    const BODY_ID = 'reschedule-dialog-body';

    // The message to show for a failed reschedule response body (JSON `{error}` or plain text).
    function errorText(responseText, fallback) {
        try {
            const data = JSON.parse(responseText);
            if (data && data.error) return String(data.error);
        } catch (e) { /* not JSON */ }
        return fallback;
    }

    // The chip to focus once the grid has been redrawn after a reschedule, or null. The week view
    // draws the post twice (phone agenda and desktop grid), so skip the copy that is not shown.
    function chipFor(container, platformPostId) {
        if (!container || !platformPostId) return null;
        const rows = container.querySelectorAll('[data-platform-post-id="' + platformPostId + '"]');
        for (let i = 0; i < rows.length; i++) {
            if (rows[i].offsetParent === null) continue;
            return rows[i].querySelector('.post-chip') || rows[i].querySelector('button');
        }
        return null;
    }

    root.rescheduleDialog = { errorText, chipFor };

    if (typeof document === 'undefined') return;

    let pendingFocusId = null;

    function dialog() { return document.getElementById(DIALOG_ID); }

    function toast(tone, title) {
        document.body.dispatchEvent(new CustomEvent('showToast', { detail: { tone: tone, title: title } }));
    }

    // A refused request (400/403) carries the explanation as dialog markup: swap it in rather than drop it.
    document.body.addEventListener('htmx:beforeSwap', function (e) {
        const target = e.detail && e.detail.target;
        if (target && target.id === BODY_ID) {
            const status = e.detail.xhr && e.detail.xhr.status;
            if (status === 400 || status === 403) {
                e.detail.shouldSwap = true;
                e.detail.isError = false;
            }
        }
    });

    document.body.addEventListener('htmx:afterSwap', function (e) {
        const target = e.detail && e.detail.target;
        const dlg = dialog();
        if (!target || target.id !== BODY_ID || !dlg) return;
        if (!dlg.open && typeof dlg.showModal === 'function') dlg.showModal();
    });

    document.addEventListener('click', function (e) {
        const dlg = dialog();
        if (!dlg || !dlg.open) return;
        if (e.target === dlg || (e.target.closest && e.target.closest('[data-dialog-close]') && dlg.contains(e.target))) dlg.close();
    });

    document.addEventListener('submit', function (e) {
        const form = e.target;
        if (!form || !form.hasAttribute || !form.hasAttribute('data-reschedule-form')) return;
        e.preventDefault();
        const message = form.querySelector('[data-reschedule-error]');
        const button = form.querySelector('[type="submit"]');
        if (message) message.classList.add('hidden');
        if (button) button.disabled = true;
        fetch(form.action, {
            method: 'POST',
            headers: { 'X-Requested-With': 'XMLHttpRequest', 'X-CSRFToken': form.querySelector('[name=csrfmiddlewaretoken]').value },
            body: new URLSearchParams(new FormData(form)),
        }).then(function (res) {
            if (res.ok) {
                pendingFocusId = form.getAttribute('data-platform-post-id');
                const dlg = dialog();
                if (dlg) dlg.close();
                toast('success', 'Post rescheduled');
                window.dispatchEvent(new CustomEvent('calendar-reschedule-done'));
                return null;
            }
            return res.text().then(function (text) {
                if (button) button.disabled = false;
                if (message) {
                    message.textContent = errorText(text, 'Could not reschedule that post.');
                    message.classList.remove('hidden');
                }
            });
        }).catch(function () {
            if (button) button.disabled = false;
            if (message) {
                message.textContent = 'Could not reschedule that post. Check your connection and try again.';
                message.classList.remove('hidden');
            }
        });
    });

    // The redrawn grid replaced the chip that opened the dialog; put focus on its new copy.
    document.body.addEventListener('htmx:afterSettle', function (e) {
        if (!pendingFocusId) return;
        const target = e.detail && e.detail.target;
        if (!target || target.id !== 'calendar-content') return;
        const chip = chipFor(target, pendingFocusId);
        pendingFocusId = null;
        if (chip) chip.focus();
    });
})(typeof globalThis !== 'undefined' ? globalThis : this);
