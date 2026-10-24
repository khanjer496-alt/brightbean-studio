const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '../..');
const source = fs.readFileSync(path.join(root, 'static/js/composer-inserts.js'), 'utf8');
const emoji = JSON.parse(fs.readFileSync(path.join(root, 'static/js/emoji-data.json'), 'utf8'));

// A text area that behaves like the real one where the toolbar cares: value,
// selection, setRangeText and the events it dispatches.
function textarea(value = '', caret = value.length) {
  const el = {
    value, selectionStart: caret, selectionEnd: caret, inputEvents: 0, attributes: {}, listeners: {},
    setRangeText(text, start, end) {
      this.value = this.value.slice(0, start) + text + this.value.slice(end);
      this.selectionStart = this.selectionEnd = start + text.length;
    },
    dispatchEvent(event) { if (event.type === 'input') this.inputEvents++; (this.listeners[event.type] || []).forEach((fn) => fn()); },
    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); },
    setAttribute(name, val) { this.attributes[name] = val; },
    removeAttribute(name) { delete this.attributes[name]; },
    focus() {}, dataset: {},
  };
  return el;
}

function toolbar({ el = textarea(), accounts = [], storage, fetch } = {}) {
  const timers = [];
  let factory;
  const listeners = {};
  const sandbox = {
    document: { addEventListener(type, fn) { listeners[type] = fn; } },
    Alpine: { data(name, fn) { factory = fn; } },
    localStorage: storage || { getItem: () => null, setItem() {} },
    fetch: fetch || (async () => ({ ok: true, json: async () => ({ groups: emoji.groups }) })),
    URLSearchParams, AbortController, Event: class { constructor(type) { this.type = type; } },
    setTimeout(fn) { timers.push(fn); return timers.length; }, clearTimeout() {}, Date, JSON, Promise, Array, Map,
  };
  vm.runInNewContext(source, sandbox);
  listeners['alpine:init']();
  const component = factory(() => el, () => accounts);
  component.$el = { dataset: { emojiUrl: '/e.json', hashtagsUrl: '/h/', signaturesUrl: '/s/', mentionsUrl: '/m/' } };
  component.$nextTick = (fn) => fn();
  component.$refs = {};
  component.init();
  const flush = async () => { while (timers.length) await timers.shift()(); };
  return { component, el, flush };
}

test('emoji data is a full, compact set with names, keywords, skin tones and flags', () => {
  const rows = emoji.groups.flatMap((g) => g.emoji);
  assert.ok(rows.length > 1800);
  assert.ok(rows.every(([glyph, name, keywords]) => glyph && typeof name === 'string' && name && (keywords === undefined || typeof keywords === 'string')));
  assert.equal(new Set(rows.map((r) => r[0])).size, rows.length);
  assert.ok(emoji.groups.map((g) => g.name).includes('Flags'));
  const wave = rows.find((r) => r[0] === '👋');
  assert.equal(wave[3].length, 5, 'five skin tones');
  assert.ok(rows.filter((r) => r[3]).every((r) => r[3].length === 5));
  assert.match(emoji.source, /^emojibase-data \d+\.\d+\.\d+ \(MIT\)/);
  assert.ok(fs.statSync(path.join(root, 'static/js/emoji-data.json')).size < 200 * 1024, 'stays small');
});

test('emoji search finds flags by country name and shortcode keywords', async () => {
  const { component } = toolbar();
  await component.openEmoji();
  component.emojiQuery = 'new zealand';
  assert.ok(component.emojiShown.some((r) => r[0] === '🇳🇿'));
  component.emojiQuery = 'rofl';
  assert.ok(component.emojiShown.some((r) => r[0] === '🤣'));
});

test('the chosen skin tone applies to emoji that have one and is remembered', async () => {
  const saved = {};
  const storage = { getItem: (k) => saved[k] ?? null, setItem: (k, v) => { saved[k] = v; } };
  const { component } = toolbar({ storage });
  await component.openEmoji();
  const wave = component.groups.flatMap((g) => g.emoji).find((r) => r[0] === '👋');
  const smile = component.groups.flatMap((g) => g.emoji).find((r) => r[0] === '🙂');
  assert.equal(component.emojiGlyph(wave), '👋');
  component.setEmojiTone(3);
  assert.equal(component.emojiGlyph(wave), '👋🏽');
  assert.equal(component.emojiGlyph(smile), '🙂', 'emoji without tones are unchanged');
  assert.equal(saved['composer:emoji-tone'], '3');
  assert.equal(component.emojiName('👋🏽'), 'waving hand');
  const again = toolbar({ storage });
  await again.component.openEmoji();
  assert.equal(again.component.emojiTone, 3);
  const corrupt = toolbar({ storage: { getItem: () => '99', setItem() {} } });
  await corrupt.component.openEmoji();
  assert.equal(corrupt.component.emojiTone, 0);
});

test('insert puts text at the cursor and replaces a selection', () => {
  const { component, el } = toolbar({ el: textarea('Hello world', 5) });
  component.insert('🙂');
  assert.equal(el.value, 'Hello🙂 world');
  el.selectionStart = 5; el.selectionEnd = 7;
  component.insert('!');
  assert.equal(el.value, 'Hello! world');
  assert.equal(el.inputEvents, 2);
});

test('hashtags are spaced off a word and signatures set apart on their own lines', () => {
  const { component, el } = toolbar({ el: textarea('Great coffee') });
  component.insert('#coffee', { pad: true });
  assert.equal(el.value, 'Great coffee #coffee');
  component.insert('-- Sam', { block: true });
  assert.equal(el.value, 'Great coffee #coffee\n\n-- Sam');
  el.value = 'Line\n'; el.selectionStart = el.selectionEnd = 5;
  component.insert('-- Sam', { block: true });
  assert.equal(el.value, 'Line\n\n-- Sam');
  const empty = toolbar({ el: textarea('') });
  empty.component.insert('-- Sam', { block: true });
  assert.equal(empty.el.value, '-- Sam');
});

test('emoji search matches names and keywords and is capped', async () => {
  const { component } = toolbar();
  await component.openEmoji();
  component.emojiQuery = 'face with tears of joy';
  assert.ok(component.emojiShown.some((r) => r[0] === '😂'));
  component.emojiQuery = 'LOL';
  assert.ok(component.emojiShown.some((r) => r[0] === '😂'));
  component.emojiQuery = 'face';
  assert.ok(component.emojiShown.length <= 96);
  component.emojiQuery = 'zzzzzz';
  assert.equal(component.emojiShown.length, 0);
});

test('recent emoji are remembered, newest first, without duplicates', async () => {
  const saved = {};
  const { component } = toolbar({ storage: { getItem: (k) => saved[k] ?? null, setItem: (k, v) => { saved[k] = v; } } });
  component.pickEmoji('🙂'); component.pickEmoji('🔥'); component.pickEmoji('🙂');
  assert.deepEqual(JSON.parse(saved['composer:recent-emoji']), ['🙂', '🔥']);
});

test('blocked or corrupt storage never stops an emoji going in', async () => {
  const broken = { getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); } };
  const { component, el } = toolbar({ storage: broken });
  await component.openEmoji();
  component.pickEmoji('🙂');
  assert.equal(el.value, '🙂');
  const corrupt = toolbar({ storage: { getItem: () => '{not json', setItem() {} } });
  await corrupt.component.openEmoji();
  assert.equal(corrupt.component.recentEmoji.length, 0);
});

test('typing @ suggests accounts, debounced, and picking one completes the handle', async () => {
  const calls = [];
  const fetch = async (url) => { calls.push(url); return { ok: true, json: async () => ({ results: [{ handle: 'alice.bsky.social', name: 'Alice', avatar_url: '' }] }) }; };
  const { component, el, flush } = toolbar({ el: textarea('Thanks @ali'), accounts: ['a1', 'a2'], fetch });
  el.dispatchEvent({ type: 'input' });
  assert.equal(calls.length, 0, 'waits for the debounce');
  await flush();
  assert.equal(calls.length, 1);
  assert.match(calls[0], /^\/m\/\?q=ali&account=a1&account=a2$/);
  assert.equal(component.mentionOpen, true);
  assert.equal(el.attributes['aria-expanded'], 'true');
  component.pickMention(component.mentionResults[0]);
  assert.equal(el.value, 'Thanks @alice.bsky.social ');
  assert.equal(component.mentionOpen, false);
});

test('suggestions are cached, so the same query does not ask again', async () => {
  let asked = 0;
  const fetch = async () => { asked++; return { ok: true, json: async () => ({ results: [{ handle: 'a' }] }) }; };
  const { el, flush } = toolbar({ el: textarea('@al'), accounts: ['a1'], fetch });
  el.dispatchEvent({ type: 'input' }); await flush();
  el.dispatchEvent({ type: 'input' }); await flush();
  assert.equal(asked, 1);
});

test('@ stays plain text without lookup, inside an email or when the request fails', async () => {
  let asked = 0;
  const fetch = async () => { asked++; throw new Error('offline'); };
  for (const [text, accounts] of [['hi @ali', []], ['mail me@example.com', ['a1']]]) {
    const { el, flush, component } = toolbar({ el: textarea(text), accounts, fetch });
    el.dispatchEvent({ type: 'input' }); await flush();
    assert.equal(component.mentionOpen, false, text);
  }
  assert.equal(asked, 0);
  const failing = toolbar({ el: textarea('@ali'), accounts: ['a1'], fetch });
  failing.el.dispatchEvent({ type: 'input' });
  await failing.flush();
  assert.equal(failing.component.mentionOpen, false);
  assert.equal(failing.el.value, '@ali');
});

test('arrow keys, Enter and Escape drive the suggestion list', async () => {
  const fetch = async () => ({ ok: true, json: async () => ({ results: [{ handle: 'one' }, { handle: 'two' }] }) });
  const { component, el, flush } = toolbar({ el: textarea('@o'), accounts: ['a1'], fetch });
  el.dispatchEvent({ type: 'input' }); await flush();
  const key = (k) => { const e = { key: k, prevented: false, preventDefault() { this.prevented = true; }, stopPropagation() {} }; component.onKeydown(e); return e; };
  assert.equal(key('ArrowDown').prevented, true);
  assert.equal(component.mentionIndex, 1);
  key('ArrowDown');
  assert.equal(component.mentionIndex, 0, 'wraps');
  key('ArrowUp');
  assert.equal(component.mentionIndex, 1);
  key('Enter');
  assert.equal(el.value, '@two ');
  assert.equal(key('Enter').prevented, false, 'Enter is typing again once the list is closed');
});

test('a signature already closing the text is not inserted twice', () => {
  const { component, el } = toolbar({ el: textarea('Great coffee\n\n-- Sam\n') });
  component.$refs = {};
  component.panel = 'signatures';
  component.pickSignature({ body: '-- Sam' });
  assert.equal(el.value, 'Great coffee\n\n-- Sam\n');
  assert.equal(el.inputEvents, 0);
  component.pickSignature({ body: '-- Team' });
  assert.equal(el.value, 'Great coffee\n\n-- Sam\n\n-- Team');
});
