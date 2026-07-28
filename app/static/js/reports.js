/* Wires the report pages to the shared chart helper.
 *
 * All chart configuration lives in charts.js. This file only reads the data the
 * server rendered into the page and says which question each chart answers.
 */

(function () {
  "use strict";

  function readJson(id) {
    var el = document.getElementById(id);
    if (!el) {
      return null;
    }
    try {
      return JSON.parse(el.textContent);
    } catch (e) {
      return null;
    }
  }

  function drawTrend() {
    var data = readJson("revenue-trend-data");
    if (!data || !window.SRICharts) {
      return;
    }

    SRICharts.render("revenue-trend", {
      type: "line",
      labels: data.labels,
      valueFormat: "currency",
      forceLegend: true,
      datasets: [
        { label: "Billed", data: data.billed },
        { label: "Collected", data: data.collected },
        { label: "Outstanding", data: data.outstanding },
      ],
    });

    var sessionDatasets = [{ label: "Sessions", data: data.sessions }];
    if (data.sessions.length >= 8) {
      sessionDatasets.push({
        label: "4 period average",
        data: trailingAverage(data.sessions, 4),
        color: SRICharts.palette()[1],
        overlay: true,
      });
    }
    SRICharts.render("sessions-trend", {
      type: "bar",
      labels: data.labels,
      valueFormat: "count",
      forceLegend: data.sessions.length >= 8,
      datasets: sessionDatasets,
    });
  }

  /* Trailing mean over the last `window` points, so the line smooths noise without
   * ever using data from the future. */
  function trailingAverage(values, window) {
    return values.map(function (_, i) {
      var start = Math.max(0, i - window + 1);
      var slice = values.slice(start, i + 1);
      var sum = slice.reduce(function (a, b) {
        return a + b;
      }, 0);
      return Math.round((sum / slice.length) * 10) / 10;
    });
  }

  function drawUtilizationBars() {
    var data = readJson("utilization-data");
    if (!data || !window.SRICharts) {
      return;
    }
    var datasets = [{ label: "Sessions per week", data: data.perWeek }];
    if (data.threshold) {
      datasets.push({
        label: "Threshold",
        data: data.labels.map(function () {
          return data.threshold;
        }),
        reference: true,
      });
    }
    SRICharts.render("utilization-bars", {
      type: "bar",
      horizontal: true,
      labels: data.labels,
      valueFormat: "count",
      forceLegend: true,
      datasets: datasets,
    });
  }

  function drawTherapistHistory() {
    var data = readJson("therapist-history-data");
    if (!data || !window.SRICharts) {
      return;
    }
    var datasets = [{ label: "Sessions", data: data.sessions }];
    if (data.threshold) {
      datasets.push({
        label: "Threshold",
        data: data.labels.map(function () {
          return data.threshold;
        }),
        reference: true,
      });
    }
    SRICharts.render("therapist-history", {
      type: "bar",
      labels: data.labels,
      valueFormat: "count",
      forceLegend: data.threshold > 0,
      datasets: datasets,
    });
  }

  function drawWeeklyCounts() {
    var data = readJson("weekly-counts-data");
    if (!data || !window.SRICharts) {
      return;
    }
    var base = SRICharts.palette()[0];
    var colors = data.sessions.map(function (_, i) {
      // The half finished current week is muted so it never reads as a collapse.
      return data.inProgress && data.inProgress[i]
        ? SRICharts.withAlpha(base, 0.4)
        : base;
    });
    var datasets = [{ label: "Sessions", data: data.sessions, colors: colors }];
    if (data.average) {
      datasets.push({
        label: "Average completed week",
        data: data.sessions.map(function () {
          return data.average;
        }),
        reference: true,
      });
    }
    SRICharts.render("weekly-counts", {
      type: "bar",
      labels: data.labels,
      valueFormat: "count",
      forceLegend: Boolean(data.average),
      datasets: datasets,
    });
  }

  function drawSparklines() {
    if (!window.SRICharts) {
      return;
    }
    var nodes = document.querySelectorAll("canvas[data-spark]");
    Array.prototype.forEach.call(nodes, function (canvas) {
      var raw = canvas.getAttribute("data-spark") || "";
      var values = raw
        .split(",")
        .map(function (v) {
          return parseFloat(v);
        })
        .filter(function (v) {
          return !isNaN(v);
        });
      if (values.length > 1) {
        SRICharts.sparkline(canvas.id, values);
      }
    });
  }

  function draw() {
    drawTrend();
    drawUtilizationBars();
    drawTherapistHistory();
    drawWeeklyCounts();
    drawSparklines();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", draw);
  } else {
    draw();
  }

})();
