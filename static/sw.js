/* PostDelegate service worker. Served from /sw.js (root scope) by apps.common.pwa.service_worker,
 * which replaces the version token below with the build id so every deploy gets a fresh cache.
 *
 * Security rules (do not relax):
 *  - Never cache HTML for signed-in pages. Navigations are network-first and only the public offline page is stored.
 *  - Never touch API, MCP, OAuth, account, webhook, media or other authenticated paths: they bypass the worker.
 *  - Only GET requests to this origin for content-hashed /static/ files are cached, never when the server says
 *    no-store, private or no-cache.
 */
const VERSION = '__PD_SW_VERSION__';
const STATIC_CACHE = 'pd-static-' + VERSION;
const OFFLINE_CACHE = 'pd-offline-' + VERSION;
const OFFLINE_URL = '/offline/';
const CACHE_PREFIXES = ['pd-static-', 'pd-offline-'];

// Paths the worker never answers (prefix match), so nothing authenticated or signed can end up in a cache.
const NEVER_INTERCEPT = ['/api/', '/mcp', '/oauth/', '/accounts/', '/webhooks/', '/admin/', '/media/', '/integrations/', '/portal/', '/connect/'];

// ManifestStaticFilesStorage names: app.<12 hex chars>.css. Unhashed files are never cached, so dev and deploys stay fresh.
const HASHED_STATIC = /^\/static\/.+\.[0-9a-f]{12}\.[a-z0-9]+$/;

// Decide what the worker does with a request: 'bypass' | 'static' | 'navigate'. Pure, so it can be unit tested.
function classify(method, url, mode, origin) {
  if (method !== 'GET') return 'bypass';
  if (url.origin !== origin) return 'bypass';
  for (const prefix of NEVER_INTERCEPT) {
    if (url.pathname === prefix.replace(/\/$/, '') || url.pathname.startsWith(prefix)) return 'bypass';
  }
  if (url.pathname === OFFLINE_URL) return 'bypass';
  if (HASHED_STATIC.test(url.pathname)) return 'static';
  if (mode === 'navigate') return 'navigate';
  return 'bypass';
}

// A response is cacheable only if it is a complete, successful, non-private one.
function isCacheable(response) {
  if (!response || response.status !== 200 || response.type !== 'basic') return false;
  const cc = (response.headers.get('Cache-Control') || '').toLowerCase();
  if (cc.includes('no-store') || cc.includes('private') || cc.includes('no-cache')) return false;
  if (response.headers.has('Set-Cookie')) return false;
  return true;
}

if (typeof self !== 'undefined' && typeof self.addEventListener === 'function') {
  self.addEventListener('install', (event) => {
    event.waitUntil(
      caches.open(OFFLINE_CACHE)
        .then((cache) => cache.add(new Request(OFFLINE_URL, { cache: 'reload', credentials: 'omit' })))
        .then(() => self.skipWaiting())
    );
  });

  self.addEventListener('activate', (event) => {
    event.waitUntil(
      caches.keys()
        .then((names) => Promise.all(names
          .filter((n) => CACHE_PREFIXES.some((p) => n.startsWith(p)) && n !== STATIC_CACHE && n !== OFFLINE_CACHE)
          .map((n) => caches.delete(n))))
        .then(() => self.clients.claim())
    );
  });

  self.addEventListener('fetch', (event) => {
    const request = event.request;
    const kind = classify(request.method, new URL(request.url), request.mode, self.location.origin);
    if (kind === 'bypass') return;

    if (kind === 'navigate') {
      // Network-first and never stored. Offline falls back to the generic page.
      event.respondWith(
        fetch(request).catch(() => caches.open(OFFLINE_CACHE)
          .then((cache) => cache.match(OFFLINE_URL))
          .then((hit) => hit || new Response('Offline', { status: 503, headers: { 'Content-Type': 'text/plain' } })))
      );
      return;
    }

    // Versioned static assets: cache-first. The hash is in the file name and the cache name carries the build id.
    if (request.headers.has('range')) return;
    event.respondWith(
      caches.open(STATIC_CACHE).then((cache) => cache.match(request).then((hit) => {
        if (hit) return hit;
        return fetch(request).then((response) => {
          if (isCacheable(response)) cache.put(request, response.clone());
          return response;
        });
      }))
    );
  });
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { classify, isCacheable, OFFLINE_URL, NEVER_INTERCEPT };
}
