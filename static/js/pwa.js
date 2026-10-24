/* PWA glue: registers the root-scope service worker, announces updates through the existing pd-toast event,
 * and drives the "Install app" rows (native prompt where the browser has one, Add to Home Screen steps on iOS). */
(function () {
  'use strict';

  function toast(message) {
    window.dispatchEvent(new CustomEvent('pd-toast', { detail: { message: message, kind: 'success' } }));
  }

  var standalone = (window.matchMedia && window.matchMedia('(display-mode: standalone)').matches) || window.navigator.standalone === true;
  var ua = window.navigator.userAgent || '';
  var isIos = /iphone|ipad|ipod/i.test(ua) || (/macintosh/i.test(ua) && window.navigator.maxTouchPoints > 1);
  var deferredPrompt = null;

  function rows() { return document.querySelectorAll('[data-pd-install]'); }
  function show(row, ios) {
    row.hidden = false;
    var help = row.querySelector('[data-pd-install-ios]');
    if (help) help.hidden = true;
    row.setAttribute('data-pd-install-mode', ios ? 'ios' : 'prompt');
  }

  function refreshRows() {
    if (standalone) return;
    rows().forEach(function (row) {
      if (deferredPrompt) show(row, false);
      else if (isIos) show(row, true);
    });
  }

  window.addEventListener('beforeinstallprompt', function (event) {
    event.preventDefault();
    deferredPrompt = event;
    refreshRows();
  });
  window.addEventListener('appinstalled', function () {
    deferredPrompt = null;
    rows().forEach(function (row) { row.hidden = true; });
  });

  document.addEventListener('click', function (event) {
    var button = event.target.closest && event.target.closest('[data-pd-install-button]');
    if (!button) return;
    var row = button.closest('[data-pd-install]');
    if (deferredPrompt) {
      var prompt = deferredPrompt;
      deferredPrompt = null;
      prompt.prompt();
      if (prompt.userChoice) prompt.userChoice.then(function () { refreshRows(); });
    } else if (row) {
      var help = row.querySelector('[data-pd-install-ios]');
      if (help) {
        help.hidden = !help.hidden;
        button.setAttribute('aria-expanded', String(!help.hidden));
      }
    }
  });

  document.addEventListener('DOMContentLoaded', refreshRows);

  if (!('serviceWorker' in navigator)) return;
  var hadController = !!navigator.serviceWorker.controller;
  var reloadPrompted = false;
  navigator.serviceWorker.addEventListener('controllerchange', function () {
    // The first install also claims the page; only an update of an already-controlled page is worth announcing.
    if (!hadController) { hadController = true; return; }
    if (reloadPrompted) return;
    reloadPrompted = true;
    toast('A new version of PostDelegate is available. Reload to update.');
  });
  window.addEventListener('load', function () {
    navigator.serviceWorker.register('/sw.js', { scope: '/', updateViaCache: 'none' }).catch(function () { /* unsupported or blocked: the site works without it */ });
  });
})();
