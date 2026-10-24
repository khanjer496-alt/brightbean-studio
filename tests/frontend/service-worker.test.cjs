const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.resolve(__dirname, '../../static/sw.js'), 'utf8').replace('__PD_SW_VERSION__', 'test-1');
const sandbox = { module: { exports: {} }, URL, console };
vm.runInNewContext(source, sandbox);
const { classify, isCacheable } = sandbox.module.exports;

const ORIGIN = 'https://app.example.com';
const route = (method, p, mode = 'cors', origin = ORIGIN) => classify(method, new URL(p, origin), mode, ORIGIN);

test('navigations are network-first', () => {
  assert.equal(route('GET', '/', 'navigate'), 'navigate');
  assert.equal(route('GET', '/workspace/1/calendar/', 'navigate'), 'navigate');
});

test('only hashed static files are cacheable', () => {
  assert.equal(route('GET', '/static/css/dist/styles.0123456789ab.css'), 'static');
  assert.equal(route('GET', '/static/css/dist/styles.css'), 'bypass');
});

test('private and non-GET requests bypass the worker', () => {
  for (const p of ['/api/v1/posts/', '/api/v1/mcp', '/mcp', '/oauth/authorize/', '/accounts/login/', '/webhooks/stripe/', '/media/x.png', '/admin/', '/offline/']) {
    assert.equal(route('GET', p, 'navigate'), 'bypass', p);
  }
  assert.equal(route('POST', '/', 'navigate'), 'bypass');
  assert.equal(route('GET', '/static/a.0123456789ab.js', 'cors', 'https://cdn.example.com'), 'bypass');
  assert.equal(route('GET', '/static/a.0123456789ab.js', 'cors', 'https://cdn.example.com'), 'bypass');
});

test('responses marked private or no-store are not cacheable', () => {
  const resp = (headers, status = 200, type = 'basic') => ({ status, type, headers: new Headers(headers) });
  assert.equal(isCacheable(resp({ 'Cache-Control': 'max-age=31536000, immutable' })), true);
  assert.equal(isCacheable(resp({ 'Cache-Control': 'no-store' })), false);
  assert.equal(isCacheable(resp({ 'Cache-Control': 'private, max-age=60' })), false);
  assert.equal(isCacheable(resp({}, 404)), false);
  assert.equal(isCacheable(resp({}, 200, 'opaque')), false);
});
