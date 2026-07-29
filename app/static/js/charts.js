/* Shared Chart.js helper.
 *
 * Every chart in the application goes through this file. It reads the design tokens
 * off the document root, so a colour change in tokens.css moves the charts too and
 * the two cannot drift apart.
 *
 * House rules enforced here, so no individual chart has to remember them:
 *   no 3D, no gradients, no drop shadows
 *   gridlines at 10 percent opacity, y axis only
 *   currency formatted axes and tooltips
 *   weeks labelled by their Monday
 *   legend hidden for single series charts, since the title already says what it is
 */

(function (global) {
  "use strict";

  function token(name, fallback) {
    var value = getComputedStyle(document.documentElement)
      .getPropertyValue(name)
      .trim();
    return value || fallback;
  }

  function palette() {
    return [
      token("--chart-1", "#0f5c5c"),
      token("--chart-2", "#4a7fa5"),
      token("--chart-3", "#8a6fa8"),
      token("--chart-4", "#a34d8c"),
      token("--chart-5", "#6b7f4a"),
      token("--chart-6", "#a06070"),
    ];
  }

  var currency = new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  });

  var currencyExact = new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });

  var integer = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });

  var FORMATS = {
    currency: { axis: currency.format, tooltip: currencyExact.format },
    count: { axis: integer.format, tooltip: integer.format },
  };

  function baseOptions(opts) {
    var fmt = FORMATS[opts.valueFormat] || FORMATS.count;
    var horizontal = opts.horizontal === true;
    var valueAxis = horizontal ? "x" : "y";
    var categoryAxis = horizontal ? "y" : "x";

    var scales = {};
    scales[valueAxis] = {
      beginAtZero: true,
      border: { display: false },
      grid: {
        color: token("--chart-grid", "rgba(26,29,33,0.1)"),
        drawTicks: false,
      },
      ticks: {
        color: token("--chart-axis-ink", "#5c6470"),
        padding: 8,
        callback: function (value) {
          return fmt.axis(value);
        },
      },
    };
    scales[categoryAxis] = {
      border: { color: token("--color-border", "#e2ded6") },
      // Category gridlines are chart junk. The value axis carries the reading.
      grid: { display: false },
      ticks: { color: token("--chart-axis-ink", "#5c6470"), padding: 6 },
    };

    return {
      responsive: true,
      maintainAspectRatio: false,
      indexAxis: horizontal ? "y" : "x",
      interaction: { mode: "index", intersect: false },
      layout: { padding: { top: 4 } },
      font: { family: token("--font-sans", "Inter, sans-serif") },
      plugins: {
        legend: {
          // A single series chart already names itself in its title.
          display: opts.forceLegend === true || opts.datasets.length > 1,
          position: "bottom",
          align: "start",
          labels: {
            boxWidth: 10,
            boxHeight: 10,
            usePointStyle: true,
            pointStyle: "circle",
            color: token("--color-ink-muted", "#5c6470"),
          },
        },
        tooltip: {
          backgroundColor: token("--color-ink", "#1a1d21"),
          padding: 10,
          cornerRadius: 4,
          displayColors: opts.datasets.length > 1,
          callbacks: {
            label: function (ctx) {
              var value = horizontal ? ctx.parsed.x : ctx.parsed.y;
              var prefix = ctx.dataset.label ? ctx.dataset.label + ": " : "";
              return prefix + fmt.tooltip(value);
            },
          },
        },
      },
      scales: scales,
    };
  }

  /* Hex token to rgba, for muting individual bars (e.g. the in-progress week)
     without inventing a second palette. */
  function withAlpha(hex, alpha) {
    var m = /^#?([0-9a-f]{6})$/i.exec(hex.trim());
    if (!m) {
      return hex;
    }
    var n = parseInt(m[1], 16);
    return (
      "rgba(" + (n >> 16) + "," + ((n >> 8) & 255) + "," + (n & 255) + "," + alpha + ")"
    );
  }

  function buildDatasets(type, datasets) {
    var colors = palette();
    return datasets.map(function (ds, i) {
      var color = ds.color || colors[i % colors.length];
      var common = {
        label: ds.label,
        data: ds.data,
        borderColor: color,
        // An array mutes individual bars (the in-progress week) without a second hue.
        backgroundColor: ds.colors || color,
      };
      if (ds.overlay) {
        // An analytic line drawn over bars, e.g. a moving average. Solid but thin,
        // no points: it is context for the bars, not a series competing with them.
        return Object.assign(common, {
          type: "line",
          borderWidth: 2,
          backgroundColor: "transparent",
          pointRadius: 0,
          pointHoverRadius: 3,
          fill: false,
          tension: 0.3,
          order: 0,
        });
      }
      if (ds.reference) {
        // A threshold line: dashed, flat, no points, and deliberately quieter than
        // the series it is there to judge.
        return Object.assign(common, {
          type: "line",
          borderWidth: 1.5,
          borderDash: [5, 4],
          borderColor: token("--color-ink-subtle", "#6e7683"),
          backgroundColor: "transparent",
          pointRadius: 0,
          pointHoverRadius: 0,
          fill: false,
          order: 0,
        });
      }
      if (type === "line") {
        return Object.assign(common, {
          borderWidth: 2,
          // No area fill: gradients and fills are chart junk here.
          fill: false,
          tension: 0.15,
          pointRadius: ds.data.length > 26 ? 0 : 3,
          pointHoverRadius: 5,
        });
      }
      return Object.assign(common, {
        borderWidth: 0,
        borderRadius: 3,
        maxBarThickness: 28,
      });
    });
  }

  /**
   * Render a chart.
   *
   * @param {string} canvasId  id of the canvas element
   * @param {object} opts
   *   type         "line" or "bar"
   *   labels       array of category labels (weeks are labelled by their Monday)
   *   datasets     [{label, data, color?}]
   *   valueFormat  "currency" or "count"
   *   horizontal   true for therapist comparison bars
   *   forceLegend  show the legend even for a single series
   */
  function render(canvasId, opts) {
    var canvas = document.getElementById(canvasId);
    if (!canvas) {
      return null;
    }
    if (typeof global.Chart === "undefined") {
      // The CDN is blocked or offline. Say so rather than leaving a blank box.
      canvas.insertAdjacentHTML(
        "afterend",
        '<p class="state state--error">Charts could not load.</p>'
      );
      return null;
    }

    var existing = global.Chart.getChart(canvas);
    if (existing) {
      existing.destroy();
    }

    return new global.Chart(canvas, {
      type: opts.type || "line",
      data: {
        labels: opts.labels,
        datasets: buildDatasets(opts.type || "line", opts.datasets),
      },
      options: baseOptions(opts),
    });
  }

  /**
   * A sparkline: shape only, no axes, no grid, no labels.
   *
   * Deliberately minimal. A sparkline on a KPI card answers "which way is this
   * going", and axes on something 40 pixels tall answer nothing while adding noise.
   */
  function sparkline(canvasId, values, opts) {
    var canvas = document.getElementById(canvasId);
    if (!canvas || typeof global.Chart === "undefined" || values.length < 2) {
      return null;
    }
    opts = opts || {};

    var existing = global.Chart.getChart(canvas);
    if (existing) {
      existing.destroy();
    }

    return new global.Chart(canvas, {
      type: "line",
      data: {
        labels: values.map(function () {
          return "";
        }),
        datasets: [
          {
            data: values,
            borderColor: opts.color || token("--chart-1", "#0f5c5c"),
            borderWidth: 1.5,
            pointRadius: 0,
            pointHoverRadius: 0,
            fill: false,
            tension: 0.2,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        events: [],
        plugins: { legend: { display: false }, tooltip: { enabled: false } },
        scales: {
          x: { display: false },
          y: { display: false, beginAtZero: true },
        },
        layout: { padding: 0 },
      },
    });
  }

  global.SRICharts = {
    render: render,
    sparkline: sparkline,
    palette: palette,
    withAlpha: withAlpha,
    formatCurrency: currencyExact.format,
    formatCount: integer.format,
  };
})(window);
