/**
 * Double-submit guard: nothing that changes data can be fired twice by an impatient click.
 *
 * HTMX: every request made with hx-post / hx-put / hx-patch / hx-delete (including boosted forms) locks the
 * element that triggered it while the request is in flight. Buttons, submit inputs and links are disabled
 * (links get aria-disabled); for a form, its submit buttons (also those attached with form="id") are
 * disabled too. Locked controls carry aria-busy="true" and data-pd-busy; after a short delay they also get
 * .btn-loading (spinner, label width kept) so quick requests do not flicker. Inputs, selects and other
 * non-button triggers are only marked aria-busy: disabling a field the user is typing in would steal focus.
 * Unlocked again on htmx:afterRequest / sendError / sendAbort / timeout, and when the page is restored from
 * the back/forward cache.
 *
 * Plain forms: a POST form that is not handled by HTMX and not already cancelled (by a confirm dialog or a
 * fetch-based handler) is submitted once; later submit events are swallowed and the submit buttons are
 * disabled. Restored on pageshow, and after RELEASE_MS in case the response is a download (no navigation).
 *
 * Opt out with data-no-lock on the element (or any ancestor), e.g. for a polling element.
 */
(function (root) {
    var VERBS = ['post', 'put', 'patch', 'delete'];
    var SPINNER_DELAY_MS = 200;
    var RELEASE_MS = 15000;

    function exempt(el) {
        return !!(el && el.closest && el.closest('[data-no-lock]'));
    }

    function isControl(el) {
        var tag = el && el.tagName;
        if (tag === 'BUTTON') return true;
        if (tag === 'A') return true;
        if (tag === 'INPUT') return /^(submit|button|image|reset)$/i.test(el.type || '');
        return false;
    }

    // The controls to lock for a trigger element: itself if it is a control, plus a form's submit buttons.
    function targetsFor(elt, submitter) {
        var out = [];
        var add = function (el) { if (el && out.indexOf(el) === -1) out.push(el); };
        if (isControl(elt)) add(elt);
        if (isControl(submitter)) add(submitter);
        if (elt && elt.tagName === 'FORM') {
            var scope = elt.ownerDocument || root.document;
            var own = elt.querySelectorAll('button:not([type=button]):not([type=reset]), input[type=submit], input[type=image]');
            for (var i = 0; i < own.length; i++) add(own[i]);
            if (elt.id && scope && scope.querySelectorAll) {
                var linked = scope.querySelectorAll('[form="' + elt.id + '"]');
                for (var j = 0; j < linked.length; j++) {
                    if (linked[j].tagName === 'BUTTON' || linked[j].tagName === 'INPUT') add(linked[j]);
                }
            }
        }
        return out;
    }

    // el -> { count, wasDisabled, timer } so overlapping requests and pre-disabled controls are handled.
    var locks = new Map();

    function lock(el, withSpinner) {
        var state = locks.get(el);
        if (state) { state.count++; return; }
        state = { count: 1, wasDisabled: !!el.disabled, timer: null };
        locks.set(el, state);
        el.setAttribute('aria-busy', 'true');
        el.setAttribute('data-pd-busy', '');
        if (isControl(el)) {
            if (el.tagName === 'A') el.setAttribute('aria-disabled', 'true');
            else el.disabled = true;
            if (withSpinner) {
                state.timer = setTimeout(function () { el.classList.add('btn-loading'); }, SPINNER_DELAY_MS);
            }
        }
    }

    function unlock(el) {
        var state = locks.get(el);
        if (!state) return;
        if (--state.count > 0) return;
        locks.delete(el);
        clearTimeout(state.timer);
        el.removeAttribute('aria-busy');
        el.removeAttribute('data-pd-busy');
        el.classList.remove('btn-loading');
        if (el.tagName === 'A') { el.removeAttribute('aria-disabled'); return; }
        // A control whose disabled state Alpine owns (:disabled="...") gets that state re-evaluated, not blindly cleared:
        // the data it depends on may have changed while the request was in flight.
        var expr = el.getAttribute(':disabled') || el.getAttribute('x-bind:disabled');
        if (expr && root.Alpine && typeof root.Alpine.evaluate === 'function') {
            try { el.disabled = !!root.Alpine.evaluate(el, expr); return; } catch (e) { /* fall through */ }
        }
        if (!state.wasDisabled) el.disabled = false;
    }

    function unlockAll() {
        Array.from(locks.keys()).forEach(function (el) { locks.get(el).count = 1; unlock(el); });
    }

    // A swap can replace the trigger itself; its afterRequest then never reaches the document. Forget such locks.
    function sweep() {
        Array.from(locks.keys()).forEach(function (el) {
            if (el.isConnected === false) { clearTimeout(locks.get(el).timer); locks.delete(el); }
        });
    }

    // What each in-flight request locked, so afterRequest releases exactly that.
    var inflight = new WeakMap();

    function mutating(detail) {
        var verb = detail && detail.requestConfig && detail.requestConfig.verb;
        return !!verb && VERBS.indexOf(String(verb).toLowerCase()) !== -1;
    }

    function onBeforeRequest(event) {
        var detail = event.detail;
        if (!detail || !mutating(detail)) return;
        var elt = detail.elt;
        if (!elt || exempt(elt)) return;
        var triggering = detail.requestConfig && detail.requestConfig.triggeringEvent;
        var submitter = triggering && triggering.submitter;
        var held = targetsFor(elt, submitter);
        if (!held.length) {
            held = [elt];   // non-button trigger: aria-busy only (lock() skips disabling non-controls)
        }
        held.forEach(function (el) { lock(el, true); });
        var xhr = detail.xhr;
        if (xhr) inflight.set(xhr, held);
    }

    function onDone(event) {
        var detail = event.detail;
        var xhr = detail && detail.xhr;
        var held = xhr && inflight.get(xhr);
        if (!held) return;
        inflight.delete(xhr);
        held.forEach(unlock);
    }

    // ---- plain forms ----
    var submitting = new WeakMap();   // form -> release timer

    function viaHtmx(form) {
        for (var i = 0; i < form.attributes.length; i++) {
            if (/^hx-(post|put|patch|delete|get|boost)$/.test(form.attributes[i].name)) return true;
        }
        return !!(form.closest && form.closest('[hx-boost="true"]'));
    }

    function release(form) {
        if (!submitting.has(form)) return;
        clearTimeout(submitting.get(form));
        submitting.delete(form);
        targetsFor(form).forEach(unlock);
    }

    function onSubmit(event) {
        var form = event.target;
        if (!form || form.tagName !== 'FORM' || event.defaultPrevented) return;
        if (viaHtmx(form) || exempt(form)) return;
        if (String(form.method).toLowerCase() !== 'post') return;
        if (form.target && form.target !== '_self') return;
        if (submitting.has(form)) { event.preventDefault(); return; }
        submitting.set(form, setTimeout(function () { release(form); }, RELEASE_MS));
        // After the browser has built the form data: a disabled submitter would drop its name/value.
        setTimeout(function () { targetsFor(form, event.submitter).forEach(function (el) { lock(el, true); }); }, 0);
    }

    function onPageShow(event) {
        if (!event.persisted) return;
        unlockAll();
    }

    root.pdFormGuard = { lock: lock, unlock: unlock, unlockAll: unlockAll, targetsFor: targetsFor, sweep: sweep, isLocked: function (el) { return locks.has(el); } };

    var doc = root.document;
    if (!doc || !doc.addEventListener) return;
    doc.addEventListener('htmx:beforeRequest', onBeforeRequest);
    ['htmx:afterRequest', 'htmx:sendError', 'htmx:sendAbort', 'htmx:timeout'].forEach(function (name) {
        doc.addEventListener(name, onDone);
    });
    doc.addEventListener('htmx:afterSwap', sweep);
    doc.addEventListener('submit', onSubmit);
    if (root.addEventListener) root.addEventListener('pageshow', onPageShow);
})(typeof globalThis !== 'undefined' ? globalThis : this);
