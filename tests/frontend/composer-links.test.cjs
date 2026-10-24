const test = require('node:test');
const assert = require('node:assert/strict');
const links = require('../../static/js/composer-links.js');

test('findUrls drops sentence punctuation and repeats', () => {
  const text = 'Read https://example.com/a?x=1, then (https://example.org/b). Again https://example.com/a?x=1!';
  assert.deepEqual(links.findUrls(text), ['https://example.com/a?x=1', 'https://example.org/b']);
});

test('shortenActive follows the post choice, then the workspace mode', () => {
  assert.equal(links.shortenActive('never', '1'), false);
  assert.equal(links.shortenActive('always', ''), true);
  assert.equal(links.shortenActive('always', '0'), false);
  assert.equal(links.shortenActive('ask', ''), false);
  assert.equal(links.shortenActive('ask', '1'), true);
});

test('countingText swaps each address for one of the short length and keeps the rest', () => {
  const long = 'https://example.com/' + 'a'.repeat(100);
  const out = links.countingText('Go ' + long + '.', 24, 'https://pd.test/s/');
  assert.equal(out, 'Go https://' + 'x'.repeat(16) + '.');
  assert.equal(out.length, 'Go '.length + 24 + 1);
});

test('countingText leaves addresses that are already short', () => {
  const text = 'See https://pd.test/s/abc2345 now';
  assert.equal(links.countingText(text, 24, 'https://pd.test/s/'), text);
});

test('countingText changes nothing without a short length', () => {
  assert.equal(links.countingText('https://example.com/long', 0, ''), 'https://example.com/long');
});

test('needsPrompt asks only in ask mode, undecided, when scheduling a post with a new link', () => {
  const base = { mode: 'ask', choice: '', texts: ['hi https://example.com'], action: 'schedule', shortPrefix: 'https://pd.test/s/' };
  assert.equal(links.needsPrompt(base), true);
  assert.equal(links.needsPrompt({ ...base, action: 'save_draft' }), false);
  assert.equal(links.needsPrompt({ ...base, choice: '0' }), false);
  assert.equal(links.needsPrompt({ ...base, mode: 'always' }), false);
  assert.equal(links.needsPrompt({ ...base, texts: ['no links here'] }), false);
  assert.equal(links.needsPrompt({ ...base, texts: ['https://pd.test/s/abc'] }), false);
});
