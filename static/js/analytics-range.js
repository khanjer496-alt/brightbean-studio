/* Calendar for the custom date range. The inputs are plain <input type="date">
   with min/max, so the form works without this; flatpickr (already loaded by
   base.html) only swaps in the themed calendar on desktop. */
(function () {
  'use strict';
  document.addEventListener('DOMContentLoaded', function () {
    if (typeof flatpickr === 'undefined') return;
    document.querySelectorAll('input[data-range-date]').forEach(function (input) {
      flatpickr(input, { dateFormat: 'Y-m-d', maxDate: input.getAttribute('max') || 'today', allowInput: true });
    });
  });
})();
