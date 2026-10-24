const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.resolve(__dirname, '../../static/js/confirm-dialog.js'), 'utf8');
const sandbox = { Promise };
sandbox.globalThis = sandbox;
vm.runInNewContext(source, sandbox);
const { optionsFor, ask } = sandbox.pdConfirm;

const el = (attrs) => ({ getAttribute: (name) => (name in attrs ? attrs[name] : null) });

test('optionsFor reads title, label and variant from the data-confirm-* attributes', () => {
  const options = optionsFor(el({ 'data-confirm-title': 'Delete event', 'data-confirm-label': 'Delete', 'data-confirm-variant': 'danger' }), 'Delete this event?');
  assert.deepEqual({ ...options }, { message: 'Delete this event?', title: 'Delete event', label: 'Delete', cancel: 'Cancel', variant: 'danger' });
});

test('optionsFor falls back to a neutral dialog and ignores unknown variants', () => {
  const options = optionsFor(el({ 'data-confirm-variant': 'weird' }), 'Sure?');
  assert.equal(options.title, 'Are you sure?');
  assert.equal(options.label, 'Confirm');
  assert.equal(options.variant, 'brand');
});

test('ask refuses (never confirms) while the dialog component is not ready', async () => {
  assert.equal(await ask({ message: 'x' }), false);
});

test('the Alpine component resolves true on accept and false on dismissal', async () => {
  const data = sandbox.pdConfirmDialog();
  let watcher;
  data.$watch = (key, fn) => { if (key === 'open') watcher = fn; };
  data.$nextTick = (fn) => fn();
  data.init();
  const first = ask({ message: 'a', title: 't', label: 'l', variant: 'danger' });
  assert.equal(data.open, true);
  assert.equal(data.variant, 'danger');
  data.accept();
  assert.equal(await first, true);
  watcher(false);   // the watcher firing after accept() must not change the answer
  const second = ask({ message: 'b', title: 't', label: 'l', variant: 'brand' });
  data.open = false;
  watcher(false);
  assert.equal(await second, false);
});

test('a question asked while the previous dialog is closing is not answered by that close', async () => {
  const data = sandbox.pdConfirmDialog();
  let watcher;
  data.$watch = (key, fn) => { if (key === 'open') watcher = fn; };
  const ticks = [];
  data.$nextTick = (fn) => ticks.push(fn);
  const flush = () => ticks.splice(0).forEach((fn) => fn());
  data.init();
  const first = ask({ message: 'a', title: 't', label: 'l', variant: 'danger' });
  flush();
  data.accept();                       // open = false; Alpine runs the watcher later
  const second = ask({ message: 'b', title: 't', label: 'l', variant: 'danger' });
  watcher(false);                      // the late close of the first dialog
  flush();
  assert.equal(await first, true);
  assert.equal(data.open, true);
  data.accept();
  assert.equal(await second, true);
});

