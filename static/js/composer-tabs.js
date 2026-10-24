/**
 * Composer channel tabs: which tabs exist, how the arrow keys move between them, and what a channel's tab shows.
 *
 * Loaded by composer/compose.html. The first tab, "global", is the shared editor; each selected channel adds one
 * tab. Pure functions, no DOM, so they are tested with node (tests/frontend/composer-tabs.test.cjs). Character
 * counting is NOT done here: the composer passes in the count its own per-network rule (caption-count.js) produced.
 */
(function (root) {
    const GLOBAL = 'global';

    // Tabs in display order: the shared editor, then the selected channels in the order the strip lists them.
    function tabOrder(allIds, selected) {
        return [GLOBAL].concat((allIds || []).filter((id) => (selected || []).indexOf(id) !== -1));
    }

    // The tab to land on after a keypress (WAI-ARIA tabs pattern), or null when the key is not a tab key.
    // In a right-to-left page the arrows are mirrored.
    function nextTab(order, active, key, rtl) {
        if (!order || !order.length) return null;
        const at = Math.max(0, order.indexOf(active));
        const forward = rtl ? 'ArrowLeft' : 'ArrowRight';
        const back = rtl ? 'ArrowRight' : 'ArrowLeft';
        if (key === forward) return order[(at + 1) % order.length];
        if (key === back) return order[(at - 1 + order.length) % order.length];
        if (key === 'Home') return order[0];
        if (key === 'End') return order[order.length - 1];
        return null;
    }

    // A channel that was deselected (or never existed) cannot stay the open tab.
    function resolveActive(order, active) {
        return order.indexOf(active) === -1 ? GLOBAL : active;
    }

    // What a channel posts: its own text when it is customised and not blank, otherwise the shared caption.
    function effectiveCaption(shared, custom, customised) {
        return customised && custom && custom.trim() ? custom : shared || '';
    }

    // How a count stands against a limit: null limit means the network has none.
    function standing(count, limit) {
        if (!limit) return { over: false, near: false, left: null };
        const left = limit - count;
        return { over: left < 0, near: left >= 0 && left <= Math.max(10, Math.floor(limit * 0.1)), left };
    }

    const api = { GLOBAL, tabOrder, nextTab, resolveActive, effectiveCaption, standing };
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
    root.composerTabs = api;
})(typeof window !== 'undefined' ? window : globalThis);
