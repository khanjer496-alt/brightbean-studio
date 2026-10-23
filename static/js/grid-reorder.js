/**
 * Drag-to-swap for the Instagram grid preview.
 *
 * Loaded by calendar/grid.html. Dropping a scheduled tile onto another one asks the
 * server to swap their publish times (the same request the Earlier / Later buttons
 * make), and the server answers with the refreshed grid. Dragging is only an
 * enhancement: touch screens and keyboards use the buttons on each tile, and every
 * permission is checked again on the server.
 */
(function (root) {
    const TILE = '[data-grid-tile]';

    function closestTile(node) {
        while (node && node.nodeType === 1) {
            if (typeof node.hasAttribute === 'function' && node.hasAttribute('data-grid-tile')) return node;
            node = node.parentNode;
        }
        return null;
    }

    // The request to make when `dragged` is dropped on `target`, or null when nothing should happen.
    function swapRequest(gridRoot, dragged, target) {
        if (!gridRoot || !dragged || !target || dragged === target) return null;
        const first = dragged.getAttribute('data-pp-id');
        const second = target.getAttribute('data-pp-id');
        const url = gridRoot.getAttribute('data-swap-url');
        if (!first || !second || !url || first === second) return null;
        return { url, values: { first, second } };
    }

    function focusMoved(gridRoot) {
        const next = gridRoot && gridRoot.querySelector('[data-grid-focus]');
        if (next && typeof next.focus === 'function') next.focus();
    }

    root.gridReorder = { closestTile, swapRequest, focusMoved };

    if (typeof document === 'undefined') return;

    let dragged = null;

    document.addEventListener('dragstart', function (e) {
        const tile = closestTile(e.target);
        if (!tile) return;
        dragged = tile;
        tile.classList.add('opacity-50');
        if (e.dataTransfer) {
            e.dataTransfer.effectAllowed = 'move';
            e.dataTransfer.setData('text/plain', tile.getAttribute('data-pp-id') || '');
        }
    });

    document.addEventListener('dragend', function () {
        if (dragged) dragged.classList.remove('opacity-50');
        dragged = null;
        document.querySelectorAll(TILE).forEach(function (t) { t.classList.remove('ring-2', 'ring-accent'); });
    });

    document.addEventListener('dragover', function (e) {
        const tile = closestTile(e.target);
        if (!dragged || !tile || tile === dragged) return;
        e.preventDefault();
        if (e.dataTransfer) e.dataTransfer.dropEffect = 'move';
        tile.classList.add('ring-2', 'ring-accent');
    });

    document.addEventListener('dragleave', function (e) {
        const tile = closestTile(e.target);
        if (tile) tile.classList.remove('ring-2', 'ring-accent');
    });

    document.addEventListener('drop', function (e) {
        const tile = closestTile(e.target);
        const gridRoot = document.querySelector('[data-grid-root]');
        const request = swapRequest(gridRoot, dragged, tile);
        if (!request) return;
        e.preventDefault();
        root.htmx.ajax('POST', request.url, { target: '#grid-body', swap: 'outerHTML', values: request.values });
    });

    // After a swap the refreshed grid replaces the old one; put focus back on the control that was used.
    document.body.addEventListener('htmx:afterSettle', function () {
        focusMoved(document.querySelector('[data-grid-root]'));
    });
})(typeof globalThis !== 'undefined' ? globalThis : this);
