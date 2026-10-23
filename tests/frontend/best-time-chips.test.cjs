const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');
const source = fs.readFileSync(path.join(root, 'templates/composer/compose.html'), 'utf8');
const block = source.match(/\/\/ "Best times" chips[\s\S]*?\n    \}\);\n/)[0];

function run(inputs, chip) {
  let handler;
  const document = {
    addEventListener(type, fn) { if (type === 'click') handler = fn; },
    querySelector(selector) { return inputs[selector.match(/name="(\w+)"/)[1]] || null; },
  };
  new Function('document', 'Event', block)(document, class { constructor(type) { this.type = type; } });
  handler({ target: { closest: () => chip } });
}

function input(extra = {}) {
  const events = [];
  return Object.assign({ value: '', events, dispatchEvent(e) { events.push(e.type); } }, extra);
}

test('a best-time chip fills the date and time fields and notifies listeners', () => {
  const date = input();
  const time = input();
  run({ scheduled_date: date, scheduled_time: time }, { dataset: { date: '2026-06-16', time: '09:00' } });
  assert.equal(date.value, '2026-06-16');
  assert.equal(time.value, '09:00');
  assert.deepEqual(date.events, ['input', 'change']);
});

test('a flatpickr-backed field is set through the picker', () => {
  const calls = [];
  const date = input({ _flatpickr: { setDate: (v, trigger) => calls.push([v, trigger]) } });
  const time = input();
  run({ scheduled_date: date, scheduled_time: time }, { dataset: { date: '2026-06-16', time: '09:00' } });
  assert.deepEqual(calls, [['2026-06-16', true]]);
  assert.equal(date.value, '');
});

test('clicks elsewhere do nothing', () => {
  const date = input();
  run({ scheduled_date: date, scheduled_time: input() }, null);
  assert.equal(date.value, '');
});
