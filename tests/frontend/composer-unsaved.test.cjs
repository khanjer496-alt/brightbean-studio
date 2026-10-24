const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.resolve(__dirname, '../../static/js/composer-unsaved.js'), 'utf8');
const sandbox = {};
sandbox.globalThis = sandbox;
vm.runInNewContext(source, sandbox);
const { serialize, guardedHref } = sandbox.composerUnsaved;

test('serialize ignores tokens, the autosave post id and the pressed action', () => {
  const a = serialize([['caption', 'hi'], ['csrfmiddlewaretoken', 'x'], ['_autosave_post_id', ''], ['action', 'save_draft']]);
  const b = serialize([['caption', 'hi'], ['csrfmiddlewaretoken', 'y'], ['_autosave_post_id', '42']]);
  assert.equal(a, b);
  assert.notEqual(a, serialize([['caption', 'hi!']]));
});

test('serialize tells files apart by name and size', () => {
  assert.notEqual(serialize([['f', { name: 'a.png', size: 1 }]]), serialize([['f', { name: 'a.png', size: 2 }]]));
});

const location = { origin: 'https://app.test', pathname: '/compose/', search: '' };
const link = (over) => ({
  getAttribute: () => (over.raw === undefined ? '/calendar/' : over.raw), hasAttribute: () => false,
  target: '', origin: 'https://app.test', pathname: '/calendar/', search: '', hash: '', href: 'https://app.test/calendar/', ...over,
});

test('guardedHref guards plain same-origin links only', () => {
  assert.equal(guardedHref(link({}), {}, location), 'https://app.test/calendar/');
  assert.equal(guardedHref(link({ target: '_blank' }), {}, location), null);
  assert.equal(guardedHref(link({ raw: '#top' }), {}, location), null);
  assert.equal(guardedHref(link({ raw: 'mailto:a@b.c' }), {}, location), null);
  assert.equal(guardedHref(link({ origin: 'https://other.test' }), {}, location), null);
  assert.equal(guardedHref(link({ hasAttribute: (n) => n === 'download' }), {}, location), null);
  assert.equal(guardedHref(link({}), { ctrlKey: true }, location), null);
  assert.equal(guardedHref(link({ pathname: '/compose/', search: '', hash: '#x' }), {}, location), null);
  assert.equal(guardedHref(null, {}, location), null);
});
