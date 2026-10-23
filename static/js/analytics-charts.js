/* Analytics charts. Self-hosted Chart.js (static/js/vendor/chart.umd.min.js) draws
   every <canvas data-chart="<id>"> from the JSON in <script id="<id>">:
   {labels: [ISO dates], datasets: [{label, values, muted?}], kind: "count"|"percent"}.
   Series colours come from the --chart-N theme tokens, in dataset order, never
   cycled; a dataset marked muted (the previous period) is dashed and neutral.
   The legend, summary and data table are server-rendered, so nothing here is
   needed to read the numbers. */
(function () {
  'use strict';

  var SERIES_TOKENS = ['--chart-1', '--chart-2', '--chart-3', '--chart-4'];

  function token(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function formatValue(value, kind) {
    if (value === null || value === undefined) return 'No data';
    if (kind === 'percent') return value.toFixed(1) + '%';
    return Math.round(value).toLocaleString();
  }

  function formatDay(iso) {
    var parts = iso.split('-');
    var date = new Date(Number(parts[0]), Number(parts[1]) - 1, Number(parts[2]));
    return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  }

  // A vertical hairline under the pointer that snaps to the active day.
  var crosshair = {
    id: 'analyticsCrosshair',
    afterDatasetsDraw: function (chart) {
      var active = chart.tooltip && chart.tooltip.getActiveElements();
      if (!active || !active.length) return;
      var x = active[0].element.x;
      var area = chart.chartArea;
      var ctx = chart.ctx;
      ctx.save();
      ctx.beginPath();
      ctx.moveTo(x, area.top);
      ctx.lineTo(x, area.bottom);
      ctx.lineWidth = 1;
      ctx.strokeStyle = token('--border-hover');
      ctx.stroke();
      ctx.restore();
    },
  };

  function build(canvas, config) {
    var surface = token('--surface-1');
    var seriesIndex = 0;
    var single = config.datasets.filter(function (d) { return !d.muted; }).length === 1;
    var datasets = config.datasets.map(function (dataset) {
      var color = dataset.muted ? token('--text-tertiary') : token(SERIES_TOKENS[seriesIndex++ % SERIES_TOKENS.length]);
      var values = dataset.values;
      return {
        label: dataset.label,
        data: values,
        borderColor: color,
        backgroundColor: color + '1f',
        fill: single && !dataset.muted,
        borderDash: dataset.muted ? [5, 4] : [],
        borderWidth: 2,
        tension: 0.25,
        spanGaps: false,
        // A reported day with gaps on both sides would vanish without a dot.
        pointRadius: values.map(function (v, i) {
          return v !== null && (i === 0 || values[i - 1] === null) && (i === values.length - 1 || values[i + 1] === null) ? 3 : 0;
        }),
        pointHoverRadius: 4,
        pointHoverBackgroundColor: color,
        pointHoverBorderColor: surface,
        pointHoverBorderWidth: 2,
      };
    });
    var muted = token('--text-tertiary');
    return new Chart(canvas.getContext('2d'), {
      type: 'line',
      data: { labels: config.labels, datasets: datasets },
      plugins: [crosshair],
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: token('--surface-3'),
            titleColor: token('--text-secondary'),
            bodyColor: token('--text-primary'),
            borderColor: token('--border-hover'),
            borderWidth: 1,
            padding: 10,
            boxPadding: 4,
            usePointStyle: false,
            callbacks: {
              // The series' own colour, solid: the default reads the 12% area fill and shows a blank box.
              labelColor: function (item) {
                var color = item.dataset.borderColor;
                return { borderColor: color, backgroundColor: color, borderWidth: 0, borderRadius: 2 };
              },
              title: function (items) { return items.length ? formatDay(config.labels[items[0].dataIndex]) : ''; },
              label: function (item) {
                // An aligned earlier period (compare) names its own day: the axis shows this period's.
                var dates = config.datasets[item.datasetIndex].dates;
                var when = dates ? ' (' + formatDay(dates[item.dataIndex]) + ')' : '';
                return ' ' + item.dataset.label + when + ': ' + formatValue(item.raw, config.kind);
              },
            },
          },
        },
        scales: {
          x: {
            grid: { display: false },
            border: { display: false },
            ticks: {
              maxTicksLimit: 6,
              maxRotation: 0,
              color: muted,
              callback: function (value) { return formatDay(config.labels[value]); },
            },
          },
          y: {
            beginAtZero: true,
            grid: { color: token('--border') },
            border: { display: false },
            ticks: {
              color: muted,
              maxTicksLimit: 5,
              callback: function (value) {
                return config.kind === 'percent' ? value + '%' : new Intl.NumberFormat(undefined, { notation: 'compact' }).format(value);
              },
            },
          },
        },
      },
    });
  }

  function init(root) {
    if (typeof Chart === 'undefined') return;
    (root || document).querySelectorAll('canvas[data-chart]').forEach(function (canvas) {
      var source = document.getElementById(canvas.dataset.chart);
      if (!source) return;
      var existing = Chart.getChart(canvas);
      if (existing) existing.destroy();
      var config;
      try { config = JSON.parse(source.textContent); } catch (e) { return; }
      build(canvas, config);
    });
  }

  document.addEventListener('DOMContentLoaded', function () { init(document); });
  // HTMX swaps (metric chips on the account page) bring a fresh canvas + data.
  document.body.addEventListener('htmx:afterSwap', function (event) { init(event.detail.target); });
})();
