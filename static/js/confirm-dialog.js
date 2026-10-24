/**
 * Shared confirm dialog: the only way the app asks "are you sure?" (no native confirm()).
 * Markup lives in templates/components/_confirm_dialog.html (included once by base.html) and uses x-pd-dialog,
 * so focus trap, Escape, inert background and focus return are the shared dialog behaviour.
 *
 * Declare a confirmation on the element that triggers the action:
 *
 *   <button hx-post="..." hx-confirm="Delete this event?"
 *           data-confirm-title="Delete event" data-confirm-label="Delete" data-confirm-variant="danger">
 *   <form method="post" data-confirm="Delete this report?" data-confirm-title="..." ...>
 *   <button type="submit" name="action" value="delete" data-confirm="Delete this signature?">   (button inside a form)
 *
 * hx-confirm is intercepted through htmx:confirm; data-confirm works for plain forms, submit buttons (also with the
 * form="" attribute) and links. Optional attributes: data-confirm-title (default "Are you sure?"),
 * data-confirm-label (default "Confirm"), data-confirm-cancel (default "Cancel"), data-confirm-variant ("danger" or "brand", default "brand").
 * Danger dialogs start with Cancel focused, so Enter never destroys by accident.
 *
 * Scripts: window.pdConfirm.ask({message, title, label, cancel, variant}) resolves true/false.
 */
(function (root) {
    var DEFAULTS = { title: 'Are you sure?', label: 'Confirm', cancel: 'Cancel', variant: 'brand' };

    // Dialog options for an element (title/label/variant from its data-confirm-* attributes).
    function optionsFor(el, message) {
        var get = function (name) { return el && el.getAttribute ? el.getAttribute(name) : null; };
        var variant = get('data-confirm-variant');
        return {
            message: message,
            title: get('data-confirm-title') || DEFAULTS.title,
            label: get('data-confirm-label') || DEFAULTS.label,
            cancel: get('data-confirm-cancel') || DEFAULTS.cancel,
            variant: variant === 'danger' ? 'danger' : DEFAULTS.variant
        };
    }

    var asker = null;   // set by the Alpine component once it is initialised

    function ask(options) {
        // Not ready yet (Alpine still loading): refuse rather than run a destructive action unconfirmed.
        if (!asker) return Promise.resolve(false);
        return asker(options);
    }

    // Alpine component for the dialog markup.
    function pdConfirmDialog() {
        return {
            // resolver: the pending question's answer; shown: the resolver of the question on screen (a new
            // question asked while the previous one is still closing must not be answered by that close).
            open: false, message: '', title: '', label: '', cancel: 'Cancel', variant: 'brand', resolver: null, shown: null,
            init: function () {
                var self = this;
                asker = function (options) {
                    if (self.resolver) self.settle(false);
                    self.message = options.message; self.title = options.title;
                    self.label = options.label; self.variant = options.variant;
                    self.cancel = options.cancel || DEFAULTS.cancel;
                    // Open on the next tick so the variant-dependent bindings (which button gets data-autofocus)
                    // are rendered before x-pd-dialog picks where focus lands: danger must start on Cancel.
                    return new Promise(function (resolve) {
                        self.resolver = resolve;
                        self.$nextTick(function () {
                            if (self.resolver !== resolve) return;
                            self.shown = resolve;
                            self.open = true;
                        });
                    });
                };
                // Escape, backdrop click and the X button all just close the dialog: that is a "no".
                this.$watch('open', function (value) {
                    if (value) return;
                    if (self.shown && self.shown === self.resolver) self.settle(false);
                    self.shown = null;
                });
            },
            settle: function (answer) {
                var resolve = this.resolver;
                this.resolver = null;
                if (resolve) resolve(answer);
            },
            accept: function () { this.settle(true); this.open = false; }
        };
    }

    root.pdConfirm = { ask: ask, optionsFor: optionsFor };
    root.pdConfirmDialog = pdConfirmDialog;

    if (typeof document === 'undefined') return;

    var passing = null;   // the element whose re-dispatched action must not be asked about again
    // Plain (non-htmx) forms already submitted after a yes: the page is navigating away, so a second click on
    // the same button must not ask again and post twice. Reset when the page is shown again (back button).
    var sent = new WeakSet();
    window.addEventListener('pageshow', function () { sent = new WeakSet(); });
    var HTMX_VERBS = ['hx-post', 'hx-get', 'hx-put', 'hx-patch', 'hx-delete'];
    function viaHtmx(form, submitter) {
        var has = function (node) { return node && HTMX_VERBS.some(function (a) { return node.hasAttribute(a); }); };
        return has(form) || has(submitter) || !!form.closest('[hx-boost="true"]');
    }
    function submit(form, submitter) {
        form.requestSubmit(submitter || undefined);
        if (!viaHtmx(form, submitter)) sent.add(form);
    }

    // hx-confirm: htmx asks us instead of window.confirm().
    document.addEventListener('htmx:confirm', function (event) {
        var detail = event.detail;
        if (!detail || !detail.question) return;
        event.preventDefault();
        ask(optionsFor(detail.elt, detail.question)).then(function (ok) { if (ok) detail.issueRequest(true); });
    });

    function confirmThen(el, run) {
        var message = el.getAttribute('data-confirm');
        ask(optionsFor(el, message)).then(function (ok) {
            if (!ok) return;
            passing = el;
            try { run(); } finally { passing = null; }
        });
    }

    // data-confirm on a form: ask before it submits.
    document.addEventListener('submit', function (event) {
        var form = event.target;
        if (sent.has(form)) { event.preventDefault(); event.stopImmediatePropagation(); return; }
        if (!form.hasAttribute || !form.hasAttribute('data-confirm') || passing === form) return;
        event.preventDefault();
        event.stopImmediatePropagation();
        var submitter = event.submitter;
        confirmThen(form, function () { submit(form, submitter); });
    }, true);

    // data-confirm on a submit button or a link.
    document.addEventListener('click', function (event) {
        var el = event.target.closest ? event.target.closest('[data-confirm]') : null;
        if (!el || el.tagName === 'FORM' || passing === el || el.disabled) return;
        if (event.defaultPrevented) return;
        var form = el.form;
        var isSubmit = form && (el.type === 'submit' || el.type === 'image');
        var href = el.tagName === 'A' ? el.href : null;
        if (!isSubmit && !href) return;
        event.preventDefault();
        event.stopImmediatePropagation();
        if (isSubmit && sent.has(form)) return;
        // Re-dispatch the click itself (passing lets it through) so htmx (hx-boost, hx-get on a link) and the
        // browser handle it exactly as they would have without the question.
        confirmThen(el, function () {
            if (isSubmit) submit(form, el); else el.click();
        });
    }, true);
}(typeof window !== 'undefined' ? window : globalThis));
