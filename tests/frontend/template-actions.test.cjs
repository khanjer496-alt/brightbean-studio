const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');
function template(file) { return fs.readFileSync(path.join(root, file), 'utf8').replace(/{%[\s\S]*?%}/g, '/test/'); }
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
for (const file of ['templates/composer/compose.html', 'templates/calendar/partials/_delete_post_modal.html']) {
  for (const result of ['success', 'http-error', 'network-error']) {
    test(`${file}: deletion ${result} reports accurate state`, async () => {
      const handler = template(file).match(/@click="\s*(deleting = true;[\s\S]*?)"/)[1];
      let deletedEvents = 0;
      const state = { deleting: false, deleteError: '', showDeleteModal: true, deleteAccountId: null, deletePostId: 1,
        window: { location: { search: '', href: '' }, dispatchEvent() { deletedEvents++; } },
        CustomEvent: class {},
        fetch: async () => { if (result === 'network-error') throw new Error('offline'); return { ok: result === 'success' }; },
      };
      await new AsyncFunction('state', `with (state) { ${handler.replace('fetch(', 'return fetch(')} }`)(state);
      if (result === 'success') {
        assert.equal(state.deleteError, '');
        assert.ok(deletedEvents === 1 || state.window.location.href);
      } else {
        assert.equal(state.deleting, false);
        assert.equal(state.showDeleteModal, true);
        assert.ok(state.deleteError);
        assert.equal(deletedEvents, 0);
        assert.equal(state.window.location.href, '');
      }
    });
  }
}
for (const result of ['success', 'denied', 'unavailable']) {
  test(`clipboard ${result} reports success only after write resolves`, async () => {
    const handler = template('templates/onboarding/partials/_connection_link_created.html').match(/@click="(copied = false;[\s\S]*?)"/)[1];
    let selected = false;
    const state = { copied: false, copyError: false, setTimeout() {}, navigator: {},
      $refs: { connectionLink: { value: 'https://example.test/client/token', focus() {}, select() { selected = true; } } },
    };
    if (result !== 'unavailable') state.navigator.clipboard = { writeText: async () => { if (result === 'denied') throw new Error('denied'); } };
    await new AsyncFunction('state', `with (state) { ${handler.replace('Promise.resolve()', 'return Promise.resolve()')} }`)(state);
    assert.equal(state.copied, result === 'success');
    assert.equal(state.copyError, result !== 'success');
    assert.equal(selected, result !== 'success');
  });
}
