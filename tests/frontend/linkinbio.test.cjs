const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '../..');
const source = fs.readFileSync(path.join(root, 'static/js/linkinbio.js'), 'utf8');

function load(extra = {}) {
  const beacons = [];
  const sandbox = {
    navigator: { sendBeacon: (url, blob) => { beacons.push({ url, blob }); return true; } },
    Blob: class { constructor(parts, opts) { this.parts = parts; this.type = opts.type; } },
    ...extra,
  };
  sandbox.globalThis = sandbox;
  vm.runInNewContext(source, sandbox);
  return { lib: sandbox.linkInBio, beacons };
}

const anchor = (id) => ({ nodeType: 1, tagName: 'A', getAttribute: (n) => (n === 'data-bio-id' ? id : null) });
const doc = { body: { getAttribute: (n) => (n === 'data-click-url' ? '/l/acme/click/' : null) } };

test('the payload carries only the link id', () => {
  const { lib } = load();
  assert.equal(lib.clickPayload(anchor('abc-123')), 'l=abc-123');
  assert.equal(lib.clickPayload(anchor('a b&c')), 'l=a%20b%26c');
});

test('links without an id (socials, the badge) are not reported', () => {
  const { lib, beacons } = load();
  assert.equal(lib.clickPayload(anchor(null)), null);
  assert.equal(lib.report({ target: anchor(null) }, doc), false);
  assert.equal(beacons.length, 0);
});

test('a click inside an anchor is attributed to the anchor and sent as a beacon', () => {
  const { lib, beacons } = load();
  const span = { nodeType: 1, tagName: 'SPAN', parentNode: anchor('id-1') };
  assert.equal(lib.report({ target: span }, doc), true);
  assert.equal(beacons.length, 1);
  assert.equal(beacons[0].url, '/l/acme/click/');
  assert.deepEqual(Array.from(beacons[0].blob.parts), ['l=id-1']);
  assert.equal(beacons[0].blob.type, 'application/x-www-form-urlencoded');
});

test('without sendBeacon it falls back to a cookie-less keepalive fetch', () => {
  const calls = [];
  const { lib } = load({ navigator: {}, fetch: (url, opts) => { calls.push({ url, opts }); return Promise.resolve(); } });
  assert.equal(lib.report({ target: anchor('id-2') }, doc), true);
  assert.equal(calls[0].opts.credentials, 'omit');
  assert.equal(calls[0].opts.keepalive, true);
});
