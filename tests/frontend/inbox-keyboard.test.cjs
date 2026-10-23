const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '../..');
const source = fs.readFileSync(path.join(root, 'static/js/inbox-keyboard.js'), 'utf8');
// No `document` in the sandbox, so the script only exports its functions.
const sandbox = { Event: class { constructor(type, init) { this.type = type; Object.assign(this, init); } } };
vm.runInNewContext(source, Object.assign(sandbox, { globalThis: sandbox }));
const kb = sandbox.inboxKeyboard;

const key = (k, extra = {}) => ({ key: k, target: { tagName: 'DIV' }, ...extra });

test('triage keys map to actions', () => {
  const expected = { j: 'next', k: 'prev', Enter: 'open', e: 'resolve', r: 'reply', a: 'assign_me', u: 'unread', x: 'select' };
  for (const [k, action] of Object.entries(expected)) assert.equal(kb.triageAction(key(k)), action);
  assert.equal(kb.triageAction(key('?', { shiftKey: true })), 'help');
  assert.equal(kb.triageAction(key('z')), null);
});

test('keys are ignored while typing or with modifiers', () => {
  for (const target of [{ tagName: 'TEXTAREA' }, { tagName: 'SELECT' }, { tagName: 'INPUT', type: 'text' }, { tagName: 'DIV', isContentEditable: true }]) {
    assert.equal(kb.triageAction({ key: 'j', target }), null);
  }
  assert.equal(kb.triageAction({ key: 'j', target: { tagName: 'INPUT', type: 'checkbox' } }), 'next');
  assert.equal(kb.triageAction(key('e', { metaKey: true })), null);
  assert.equal(kb.triageAction(key('e', { ctrlKey: true })), null);
  assert.equal(kb.triageAction(key('J', { shiftKey: true })), null);
});

test('j and k step within the list and clamp at the ends', () => {
  assert.equal(kb.stepIndex(-1, 3, 1), 0);
  assert.equal(kb.stepIndex(-1, 3, -1), 2);
  assert.equal(kb.stepIndex(1, 3, 1), 2);
  assert.equal(kb.stepIndex(2, 3, 1), 2);
  assert.equal(kb.stepIndex(0, 3, -1), 0);
  assert.equal(kb.stepIndex(0, 0, 1), -1);
});

function fakeRow(id) {
  return {
    id, dataset: { triageUrl: `/triage/${id}/` }, clicked: 0, focused: 0,
    classList: { contains: () => false, add() {} },
    click() { this.clicked++; }, focus() { this.focused++; }, closest() { return this; }, querySelector: () => null,
  };
}

function triage(rows, activeElement) {
  const ajax = [];
  const doc = {
    activeElement,
    querySelector: () => null,
    querySelectorAll: () => rows,
    getElementById: () => null,
  };
  const controller = kb.createTriage(doc, { ajax: (...args) => { ajax.push(args); return Promise.resolve(); } });
  return { controller, ajax, doc };
}

test('j focuses the first row, then the next', () => {
  const rows = [fakeRow('a'), fakeRow('b')];
  const { controller, doc } = triage(rows, null);
  controller.onKeydown({ ...key('j'), preventDefault() {} });
  assert.equal(rows[0].focused, 1);
  doc.activeElement = rows[0];
  controller.onKeydown({ ...key('j'), preventDefault() {} });
  assert.equal(rows[1].focused, 1);
});

test('e posts the resolve action for the focused row', () => {
  const rows = [fakeRow('a')];
  const { controller, ajax } = triage(rows, rows[0]);
  let prevented = false;
  controller.onKeydown({ ...key('e'), preventDefault() { prevented = true; } });
  assert.ok(prevented);
  assert.deepEqual(ajax[0].slice(0, 2), ['POST', '/triage/a/']);
  assert.equal(JSON.stringify(ajax[0][2].values), JSON.stringify({ action: 'resolve' }));
});

test('Enter opens only a focused row, not a control inside it', () => {
  const rows = [fakeRow('a')];
  const inner = { tagName: 'BUTTON', closest: () => rows[0] };
  const { controller } = triage(rows, inner);
  controller.onKeydown({ ...key('Enter'), preventDefault() {} });
  assert.equal(rows[0].clicked, 0);
  const second = triage(rows, rows[0]);
  second.controller.onKeydown({ ...key('Enter'), preventDefault() {} });
  assert.equal(rows[0].clicked, 1);
});

test('keys do nothing while a dialog is open', () => {
  const rows = [fakeRow('a')];
  const { controller, ajax, doc } = triage(rows, rows[0]);
  doc.querySelector = () => ({});
  controller.onKeydown({ ...key('e'), preventDefault() {} });
  assert.equal(ajax.length, 0);
});

test('slashQuery finds a "/word" at the start of a word only', () => {
  assert.deepEqual({ ...kb.slashQuery('/thanks', 7) }, { start: 0, query: 'thanks' });
  assert.deepEqual({ ...kb.slashQuery('Hi\n/re', 6) }, { start: 3, query: 're' });
  assert.deepEqual({ ...kb.slashQuery('Hello /', 7) }, { start: 6, query: '' });
  assert.equal(kb.slashQuery('http://x', 8), null);
  assert.equal(kb.slashQuery('a/b', 3), null);
  assert.equal(kb.slashQuery('/done thanks', 12), null);
});

test('filterReplies matches title or body, title prefix first, and caps the list', () => {
  const replies = [
    { title: 'Order status', body: 'Your thanks order' },
    { title: 'Thanks', body: 'Thank you!' },
    ...Array.from({ length: 12 }, (_, i) => ({ title: `Other ${i}`, body: 'thanks' })),
  ];
  const hits = kb.filterReplies(replies, 'thanks');
  assert.equal(hits[0].title, 'Thanks');
  assert.equal(hits.length, 8);
  assert.equal(kb.filterReplies(replies, 'zzz').length, 0);
});

function pickerFixture(replies) {
  const listeners = {};
  const textarea = {
    value: '/th', selectionStart: 3, attrs: {}, dispatched: [],
    setAttribute(k, v) { this.attrs[k] = v; }, removeAttribute(k) { delete this.attrs[k]; },
    addEventListener(type, fn) { listeners[type] = fn; },
    setRangeText(text, start, end) { this.value = this.value.slice(0, start) + text + this.value.slice(end); this.selectionStart = start + text.length; },
    dispatchEvent(e) { this.dispatched.push(e.type); },
  };
  const makeEl = () => ({ children: [], attrs: {}, listeners: {}, setAttribute(k, v) { this.attrs[k] = v; }, append(...c) { this.children.push(...c); }, addEventListener(t, f) { this.listeners[t] = f; } });
  const list = { id: 'picker', hidden: true, children: [], append(c) { this.children.push(c); }, set textContent(_) { this.children = []; } };
  const doc = { createElement: makeEl };
  const picker = kb.createSlashPicker(doc, textarea, list, replies);
  return { textarea, list, picker, listeners };
}

test('the picker opens on "/", moves with the arrows and inserts on Enter', () => {
  const { textarea, list, picker } = pickerFixture([{ title: 'Thanks', body: 'Thank you!' }, { title: 'Throttle', body: 'Slow down' }]);
  picker.update();
  assert.equal(list.hidden, false);
  assert.equal(list.children.length, 2);
  assert.equal(textarea.attrs['aria-expanded'], 'true');
  let prevented = false;
  assert.ok(picker.onKeydown({ key: 'ArrowDown', preventDefault() { prevented = true; } }));
  assert.ok(prevented);
  assert.equal(textarea.attrs['aria-activedescendant'], 'picker-opt-1');
  picker.onKeydown({ key: 'Enter', preventDefault() {} });
  assert.equal(textarea.value, 'Slow down');
  assert.equal(list.hidden, true);
  assert.deepEqual(textarea.dispatched, ['input']);
});

test('Escape closes the picker and other keys pass through', () => {
  const { list, picker } = pickerFixture([{ title: 'Thanks', body: 'Thank you!' }]);
  picker.update();
  assert.equal(picker.onKeydown({ key: 'a', preventDefault() {} }), false);
  picker.onKeydown({ key: 'Escape', preventDefault() {} });
  assert.equal(list.hidden, true);
  assert.equal(picker.onKeydown({ key: 'Enter', preventDefault() {} }), false);
});
