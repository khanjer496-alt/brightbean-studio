const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.resolve(__dirname, '../../static/js/form-guard.js'), 'utf8');

// A tiny DOM: elements with attributes, classList, a form's controls, and a document that records listeners.
function element(tag, attrs = {}) {
  const attributes = { ...attrs };
  const classes = new Set();
  const el = {
    tagName: tag.toUpperCase(),
    disabled: false,
    isConnected: true,
    type: attrs.type || '',
    id: attrs.id || '',
    method: attrs.method || 'get',
    target: attrs.target || '',
    attributes: [],
    classList: { add: (c) => classes.add(c), remove: (c) => classes.delete(c), contains: (c) => classes.has(c) },
    setAttribute(k, v) { attributes[k] = String(v); },
    getAttribute(k) { return k in attributes ? attributes[k] : null; },
    removeAttribute(k) { delete attributes[k]; },
    hasAttribute(k) { return k in attributes; },
    closest(sel) {
      const name = sel.replace(/[\[\]]/g, '');
      if (sel.startsWith('[') && name in attributes) return el;
      return el.parent && el.parent.closest ? el.parent.closest(sel) : null;
    },
    querySelectorAll() { return el.controls || []; },
    children: [],
  };
  Object.defineProperty(el, 'attributes', { get: () => Object.keys(attributes).map((name) => ({ name })) });
  return el;
}

function load() {
  const listeners = {};
  const doc = {
    addEventListener: (name, fn) => { (listeners[name] = listeners[name] || []).push(fn); },
    querySelectorAll: () => [],
  };
  const timers = [];
  const sandbox = {
    document: doc,
    addEventListener: (name, fn) => { (listeners['win:' + name] = listeners['win:' + name] || []).push(fn); },
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout: () => {},
    Map, WeakMap, Array,
  };
  sandbox.globalThis = sandbox;
  vm.runInNewContext(source, sandbox);
  const fire = (name, event) => (listeners[name] || []).forEach((fn) => fn(event));
  return { sandbox, fire, timers, guard: sandbox.pdFormGuard };
}

test('an htmx POST disables the button and marks it busy until the request ends', () => {
  const { fire } = load();
  const button = element('button', { 'hx-post': '/x' });
  const xhr = {};
  fire('htmx:beforeRequest', { detail: { elt: button, xhr, requestConfig: { verb: 'post' } } });
  assert.equal(button.disabled, true);
  assert.equal(button.getAttribute('aria-busy'), 'true');
  fire('htmx:afterRequest', { detail: { elt: button, xhr } });
  assert.equal(button.disabled, false);
  assert.equal(button.getAttribute('aria-busy'), null);
});

test('GET requests are never locked', () => {
  const { fire } = load();
  const button = element('button');
  fire('htmx:beforeRequest', { detail: { elt: button, xhr: {}, requestConfig: { verb: 'get' } } });
  assert.equal(button.disabled, false);
});

test('a form request locks its submit buttons and the submitter, but not a plain type=button', () => {
  const { fire } = load();
  const form = element('form', { 'hx-post': '/x' });
  const submit = element('button', { type: 'submit' });
  form.controls = [submit];
  const xhr = {};
  fire('htmx:beforeRequest', { detail: { elt: form, xhr, requestConfig: { verb: 'post', triggeringEvent: { submitter: submit } } } });
  assert.equal(submit.disabled, true);
  fire('htmx:sendError', { detail: { elt: form, xhr } });
  assert.equal(submit.disabled, false);
});

test('input fields are only marked busy, never disabled (typing must not lose focus)', () => {
  const { fire } = load();
  const input = element('input', { type: 'text' });
  const xhr = {};
  fire('htmx:beforeRequest', { detail: { elt: input, xhr, requestConfig: { verb: 'post' } } });
  assert.equal(input.disabled, false);
  assert.equal(input.getAttribute('aria-busy'), 'true');
  fire('htmx:afterRequest', { detail: { elt: input, xhr } });
  assert.equal(input.getAttribute('aria-busy'), null);
});

test('data-no-lock on the element opts out', () => {
  const { fire } = load();
  const button = element('button', { 'data-no-lock': '' });
  fire('htmx:beforeRequest', { detail: { elt: button, xhr: {}, requestConfig: { verb: 'post' } } });
  assert.equal(button.disabled, false);
});

test('a control that was already disabled stays disabled afterwards', () => {
  const { fire } = load();
  const button = element('button');
  button.disabled = true;
  const xhr = {};
  fire('htmx:beforeRequest', { detail: { elt: button, xhr, requestConfig: { verb: 'delete' } } });
  fire('htmx:afterRequest', { detail: { elt: button, xhr } });
  assert.equal(button.disabled, true);
});

test('Alpine-owned :disabled is re-evaluated on release', () => {
  const { sandbox, fire } = load();
  sandbox.Alpine = { evaluate: () => true };
  const button = element('button', { ':disabled': 'nothingSelected' });
  const xhr = {};
  fire('htmx:beforeRequest', { detail: { elt: button, xhr, requestConfig: { verb: 'post' } } });
  fire('htmx:afterRequest', { detail: { elt: button, xhr } });
  assert.equal(button.disabled, true);
});

test('the spinner class arrives after the delay and goes away on release', () => {
  const { fire, timers } = load();
  const button = element('button');
  const xhr = {};
  fire('htmx:beforeRequest', { detail: { elt: button, xhr, requestConfig: { verb: 'post' } } });
  assert.equal(button.classList.contains('btn-loading'), false);
  timers[0].fn();
  assert.equal(button.classList.contains('btn-loading'), true);
  fire('htmx:afterRequest', { detail: { elt: button, xhr } });
  assert.equal(button.classList.contains('btn-loading'), false);
});

test('plain POST form: first submit passes, the second is swallowed, pageshow restores', () => {
  const { fire, timers, guard } = load();
  const form = element('form', { method: 'post' });
  const submit = element('button', { type: 'submit' });
  form.controls = [submit];
  form.closest = () => null;
  const first = { target: form, defaultPrevented: false, submitter: submit, preventDefault() { this.defaultPrevented = true; } };
  fire('submit', first);
  assert.equal(first.defaultPrevented, false);
  timers.find((t) => t.ms === 0).fn();
  assert.equal(submit.disabled, true);
  const second = { target: form, defaultPrevented: false, submitter: submit, preventDefault() { this.defaultPrevented = true; } };
  fire('submit', second);
  assert.equal(second.defaultPrevented, true);
  fire('win:pageshow', { persisted: true });
  assert.equal(submit.disabled, false);
  assert.equal(guard.isLocked(submit), false);
});

test('forms already cancelled, GET forms and htmx forms are left alone', () => {
  const { fire, timers } = load();
  const submit = element('button', { type: 'submit' });
  for (const form of [element('form', { method: 'post' }), element('form', { method: 'get' }), element('form', { method: 'post', 'hx-post': '/x' })]) {
    form.method = form.getAttribute('method');
    form.controls = [submit];
    form.closest = () => null;
    const cancelled = form.getAttribute('hx-post') === null && form.method === 'post';
    fire('submit', { target: form, defaultPrevented: cancelled, submitter: submit, preventDefault() {} });
  }
  assert.equal(timers.filter((t) => t.ms === 0).length, 0);
  assert.equal(submit.disabled, false);
});
