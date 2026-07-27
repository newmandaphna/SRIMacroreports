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

    SRICharts.render("sessions-trend", {
      type: "bar",
      labels: data.labels,
      valueFormat: "count",
      datasets: [{ label: "Sessions", data: data.sessions }],
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
    drawSparklines();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", draw);
  } else {
    draw();
  }

  // htmx swaps sections in without a page load, so redraw after a swap.
  document.body.addEventListener("htmx:afterSwap", draw);
})();
