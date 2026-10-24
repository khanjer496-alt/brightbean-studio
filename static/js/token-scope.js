/**
 * Agent token form: choosing a preset fills in the capabilities, budgets and approval mode.
 *
 * Loaded by api_keys/list.html. A form carries `data-token-scope`; each preset radio (`name="preset"`)
 * carries what it sets as data attributes (`data-capabilities` is comma separated; `data-requests`,
 * `data-ai` and `data-posts` are the daily budgets, empty for none; `data-approval` is "1" or "0").
 * The "custom" radio has none of them and leaves the fields as they are. Ticking a capability by hand
 * moves the selection to "custom", because the token then no longer is the preset.
 */
(function (root) {
    function fields(form) {
        const q = (selector) => Array.from(form.querySelectorAll(selector));
        return {
            capabilities: q('input[name="capabilities"]'),
            requests: form.querySelector('input[name="daily_request_budget"]'),
            ai: form.querySelector('input[name="daily_ai_budget"]'),
            posts: form.querySelector('input[name="daily_post_budget"]'),
            approval: form.querySelector('input[name="requires_approval"]'),
            presets: q('input[name="preset"]'),
        };
    }

    function applyPreset(form, radio) {
        const data = radio.dataset || {};
        if (data.capabilities === undefined) return;
        const f = fields(form);
        const wanted = new Set(data.capabilities.split(',').filter(Boolean));
        f.capabilities.forEach((box) => { box.checked = wanted.has(box.value); });
        if (f.requests) f.requests.value = data.requests || '';
        if (f.ai) f.ai.value = data.ai || '';
        if (f.posts) f.posts.value = data.posts || '';
        if (f.approval) f.approval.checked = data.approval === '1';
    }

    function markCustom(form) {
        const custom = fields(form).presets.find((radio) => radio.value === '');
        if (custom) custom.checked = true;
    }

    const api = { applyPreset, markCustom };
    root.tokenScope = api;

    if (root.document) {
        root.document.addEventListener('change', (event) => {
            const target = event.target;
            const form = target && target.closest && target.closest('form[data-token-scope]');
            if (!form) return;
            if (target.name === 'preset') applyPreset(form, target);
            else if (target.name === 'capabilities') markCustom(form);
        });
    }
})(typeof window !== 'undefined' ? window : globalThis);
