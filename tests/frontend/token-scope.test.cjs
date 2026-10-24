const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '../..');
const source = fs.readFileSync(path.join(root, 'static/js/token-scope.js'), 'utf8');
// No `document` in the sandbox, so the script only exports its functions.
const sandbox = {};
vm.runInNewContext(source, Object.assign(sandbox, { globalThis: sandbox }));
const scope = sandbox.tokenScope;

function formWith(names) {
  const boxes = ['read_calendar', 'draft_posts', 'schedule_posts'].map((value) => ({ name: 'capabilities', value, checked: false }));
  const inputs = {
    daily_request_budget: { value: '9' },
    daily_ai_budget: { value: '9' },
    daily_post_budget: { value: '9' },
    requires_approval: { checked: true },
  };
  const presets = [
    { name: 'preset', value: '', checked: false },
    { name: 'preset', value: 'drafting_assistant', checked: true },
  ];
  return {
    boxes,
    inputs,
    presets,
    querySelectorAll(selector) {
      if (selector.includes('capabilities')) return boxes;
      if (selector.includes('preset')) return presets;
      return [];
    },
    querySelector(selector) {
      return inputs[selector.match(/name="(\w+)"/)[1]] || null;
    },
  };
}

test('a preset ticks its capabilities and sets the budgets and approval mode', () => {
  const form = formWith();
  scope.applyPreset(form, { dataset: { capabilities: 'read_calendar,draft_posts', requests: '2000', ai: '100', posts: '', approval: '1' } });
  assert.deepEqual(form.boxes.map((b) => b.checked), [true, true, false]);
  assert.equal(form.inputs.daily_request_budget.value, '2000');
  assert.equal(form.inputs.daily_ai_budget.value, '100');
  assert.equal(form.inputs.daily_post_budget.value, '');
  assert.equal(form.inputs.requires_approval.checked, true);
});

test('a preset without approval clears the approval box', () => {
  const form = formWith();
  scope.applyPreset(form, { dataset: { capabilities: 'read_calendar', requests: '', ai: '', posts: '', approval: '0' } });
  assert.equal(form.inputs.requires_approval.checked, false);
});

test('the custom option leaves the fields as they are', () => {
  const form = formWith();
  scope.applyPreset(form, { dataset: {} });
  assert.deepEqual(form.boxes.map((b) => b.checked), [false, false, false]);
  assert.equal(form.inputs.daily_request_budget.value, '9');
});

test('editing a capability by hand selects custom', () => {
  const form = formWith();
  scope.markCustom(form);
  assert.equal(form.presets[0].checked, true);
});
