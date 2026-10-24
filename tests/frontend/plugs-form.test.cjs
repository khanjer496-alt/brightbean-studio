const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '../..');
const source = fs.readFileSync(path.join(root, 'static/js/plugs-form.js'), 'utf8');

const caps = [
  {
    id: 'a',
    triggers: [{ value: 'likes', label: 'Likes' }],
    own: [{ value: 'repost', label: 'Repost' }, { value: 'reply', label: 'Reply' }],
    cross: [{ value: 'repost', label: 'Repost' }, { value: 'quote', label: 'Quote' }],
    reposters: [{ id: 'b', name: 'Booster' }],
    message_max: 300,
    metered: false,
    interval: 15,
  },
  {
    id: 'c',
    triggers: [{ value: 'views', label: 'Views' }],
    own: [{ value: 'reply', label: 'Reply' }],
    cross: [{ value: 'repost', label: 'Repost' }],
    reposters: [],
    message_max: 500,
    metered: true,
    interval: 60,
  },
];

// The component runs in its own realm, so arrays are compared as plain JSON.
const plain = (value) => JSON.parse(JSON.stringify(value));

function component(accountId) {
  const sandbox = {};
  const document = { getElementById: (id) => (id === 'caps' ? { textContent: JSON.stringify(caps) } : null) };
  vm.runInNewContext(source, Object.assign(sandbox, { window: sandbox, document }));
  return sandbox.plugsRuleForm('caps', accountId);
}

test('offers what the chosen account can do itself, plus actions only another account can do', () => {
  const form = component('a');
  assert.deepEqual(plain(form.actions().map((a) => [a.value, a.crossOnly])), [['repost', false], ['reply', false], ['quote', true]]);
});

test('an action the account cannot do alone asks who does it', () => {
  const form = component('a');
  form.action = 'quote';
  assert.equal(form.needsReposter(), true);
  assert.equal(form.canOwn(), false);
  form.action = 'repost';
  assert.equal(form.needsReposter(), true);
  assert.equal(form.canOwn(), true);
  form.action = 'reply';
  assert.equal(form.needsReposter(), false);
});

test('no other account to name means no reposter question', () => {
  const form = component('c');
  assert.deepEqual(plain(form.actions().map((a) => a.value)), ['reply', 'repost']);
  form.action = 'repost';
  assert.equal(form.needsReposter(), false);
});

test('switching account keeps the action only if the new network offers it', () => {
  const form = component('a');
  form.action = 'quote';
  form.accountId = 'c';
  form.actions();
  assert.equal(form.action, 'reply');
  assert.equal(form.current().metered, true);
});

test('an unknown account offers nothing instead of failing', () => {
  const form = component('zzz');
  assert.deepEqual(plain(form.actions()), []);
  assert.equal(form.current().triggers.length, 0);
});
