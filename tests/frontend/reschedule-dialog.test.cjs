const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.resolve(__dirname, '../../static/js/reschedule-dialog.js'), 'utf8');
const sandbox = {};
sandbox.globalThis = sandbox;
vm.runInNewContext(source, sandbox);
const { errorText, chipFor } = sandbox.rescheduleDialog;

test('errorText reads the JSON error and falls back for anything else', () => {
  assert.equal(errorText('{"error":"Permission denied."}', 'x'), 'Permission denied.');
  assert.equal(errorText('<html>', 'fallback'), 'fallback');
  assert.equal(errorText('{}', 'fallback'), 'fallback');
});

test('chipFor focuses the shown copy of the redrawn chip row, else its button, else null', () => {
  const link = { id: 'link' };
  const shown = { offsetParent: {}, querySelector: (sel) => (sel === '.post-chip' ? link : null) };
  const hidden = { offsetParent: null, querySelector: () => ({ id: 'hidden' }) };
  const container = { querySelectorAll: (sel) => (sel === '[data-platform-post-id="42"]' ? [hidden, shown] : []) };
  assert.equal(chipFor(container, '42'), link);
  assert.equal(chipFor(container, '7'), null);
  assert.equal(chipFor(container, ''), null);
  const button = { id: 'button' };
  const bare = { offsetParent: {}, querySelector: (sel) => (sel === 'button' ? button : null) };
  assert.equal(chipFor({ querySelectorAll: () => [bare] }, '1'), button);
});
