/**
 * Click counting for public link-in-bio pages.
 *
 * Loaded by linkinbio/public.html. Every outbound link already points at its real
 * destination; this only tells the server which one was used, with a beacon that carries
 * the link id and nothing else (no cookies are set, the server stores no address).
 */
(function (root) {
    function clickPayload(anchor) {
        const id = anchor && anchor.getAttribute && anchor.getAttribute('data-bio-id');
        return id ? 'l=' + encodeURIComponent(id) : null;
    }

    function findAnchor(node) {
        while (node && node.nodeType === 1) {
            if (String(node.tagName).toUpperCase() === 'A') return node;
            node = node.parentNode;
        }
        return null;
    }

    function report(event, doc) {
        const payload = clickPayload(findAnchor(event.target));
        const url = doc.body && doc.body.getAttribute('data-click-url');
        if (!payload || !url) return false;
        const nav = root.navigator;
        if (nav && typeof nav.sendBeacon === 'function') {
            const blob = new root.Blob([payload], { type: 'application/x-www-form-urlencoded' });
            return nav.sendBeacon(url, blob);
        }
        root.fetch(url, {
            method: 'POST',
            body: payload,
            headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
            keepalive: true,
            credentials: 'omit',
        }).catch(function () {});
        return true;
    }

    root.linkInBio = { clickPayload, findAnchor, report };

    if (typeof document !== 'undefined') {
        // click covers touch and keyboard; auxclick covers middle-click, which opens a tab without a click event.
        document.addEventListener('click', function (e) { report(e, document); });
        document.addEventListener('auxclick', function (e) { if (e.button === 1) report(e, document); });
    }
})(typeof globalThis !== 'undefined' ? globalThis : this);
