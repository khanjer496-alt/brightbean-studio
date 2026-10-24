const test = require('node:test');
const assert = require('node:assert/strict');
const tabs = require('../../static/js/composer-tabs.js');

const ALL = ['a', 'b', 'c', 'd'];

test('tabOrder is the shared editor then the selected channels in strip order', () => {
  assert.deepEqual(tabs.tabOrder(ALL, ['c', 'a']), ['global', 'a', 'c']);
  assert.deepEqual(tabs.tabOrder(ALL, []), ['global']);
  assert.deepEqual(tabs.tabOrder(ALL, ['zzz']), ['global']);
});

test('arrow keys wrap around and Home/End jump to the ends', () => {
  const order = ['global', 'a', 'c'];
  assert.equal(tabs.nextTab(order, 'global', 'ArrowRight', false), 'a');
  assert.equal(tabs.nextTab(order, 'c', 'ArrowRight', false), 'global');
  assert.equal(tabs.nextTab(order, 'global', 'ArrowLeft', false), 'c');
  assert.equal(tabs.nextTab(order, 'a', 'Home', false), 'global');
  assert.equal(tabs.nextTab(order, 'a', 'End', false), 'c');
  assert.equal(tabs.nextTab(order, 'a', 'Enter', false), null);
  assert.equal(tabs.nextTab([], 'a', 'ArrowRight', false), null);
});

test('arrow keys are mirrored in a right-to-left page', () => {
  const order = ['global', 'a', 'c'];
  assert.equal(tabs.nextTab(order, 'global', 'ArrowLeft', true), 'a');
  assert.equal(tabs.nextTab(order, 'global', 'ArrowRight', true), 'c');
});

test('resolveActive falls back to the shared editor when the open channel is gone', () => {
  assert.equal(tabs.resolveActive(['global', 'a'], 'a'), 'a');
  assert.equal(tabs.resolveActive(['global', 'a'], 'b'), 'global');
});

test('effectiveCaption uses a non-blank custom text only while customised', () => {
  assert.equal(tabs.effectiveCaption('shared', 'mine', true), 'mine');
  assert.equal(tabs.effectiveCaption('shared', 'mine', false), 'shared');
  assert.equal(tabs.effectiveCaption('shared', '   ', true), 'shared');
  assert.equal(tabs.effectiveCaption(undefined, undefined, true), '');
});

test('standing flags over and near-limit counts, and ignores networks without a limit', () => {
  assert.deepEqual(tabs.standing(281, 280), { over: true, near: false, left: -1 });
  assert.equal(tabs.standing(275, 280).near, true);
  assert.equal(tabs.standing(100, 280).near, false);
  assert.deepEqual(tabs.standing(5000, null), { over: false, near: false, left: null });
});
