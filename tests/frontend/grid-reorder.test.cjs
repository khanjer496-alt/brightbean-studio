const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '../..');
const source = fs.readFileSync(path.join(root, 'static/js/grid-reorder.js'), 'utf8');
// No `document` in the sandbox, so the script only exports its helpers.
const sandbox = {};
sandbox.globalThis = sandbox;
vm.runInNewContext(source, sandbox);
const { closestTile, swapRequest, focusMoved } = sandbox.gridReorder;

const el = (attrs, parentNode = null) => ({
  nodeType: 1,
  parentNode,
  hasAttribute: (n) => n in attrs,
  getAttribute: (n) => (n in attrs ? attrs[n] : null),
});
const gridRoot = el({ 'data-swap-url': '/grid/swap/' });

test('a drop on another tile asks the server to swap the two posts', () => {
  const request = swapRequest(gridRoot, el({ 'data-grid-tile': '', 'data-pp-id': 'a' }), el({ 'data-grid-tile': '', 'data-pp-id': 'b' }));
  assert.equal(request.url, '/grid/swap/');
  assert.deepEqual({ ...request.values }, { first: 'a', second: 'b' });
});

test('dropping on itself, on nothing, or without ids does nothing', () => {
  const a = el({ 'data-grid-tile': '', 'data-pp-id': 'a' });
  assert.equal(swapRequest(gridRoot, a, a), null);
  assert.equal(swapRequest(gridRoot, a, null), null);
  assert.equal(swapRequest(gridRoot, null, a), null);
  assert.equal(swapRequest(gridRoot, a, el({ 'data-grid-tile': '' })), null);
  assert.equal(swapRequest(el({}), a, el({ 'data-grid-tile': '', 'data-pp-id': 'b' })), null);
  assert.equal(swapRequest(gridRoot, a, el({ 'data-grid-tile': '', 'data-pp-id': 'a' })), null);
});

test('a drop on something inside a tile is attributed to the tile; fixed tiles are not targets', () => {
  const tile = el({ 'data-grid-tile': '', 'data-pp-id': 'a' });
  const inner = el({}, tile);
  assert.equal(closestTile(inner), tile);
  assert.equal(closestTile(el({ 'data-pp-id': 'published' })), null);
});

test('focus returns to the control that was used', () => {
  let focused = 0;
  focusMoved({ querySelector: (sel) => (sel === '[data-grid-focus]' ? { focus() { focused++; } } : null) });
  assert.equal(focused, 1);
  focusMoved({ querySelector: () => null });
  focusMoved(null);
});
