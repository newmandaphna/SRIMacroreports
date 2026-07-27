/* Idle session warning.
 *
 * This is a courtesy, not a control. The server enforces the timeout against
 * last_seen_at on every authenticated request; a browser with JavaScript disabled,
 * a stopped timer, or a sleeping laptop still gets signed out.
 *
 * Deliberately does NOT poll on a short interval to stay alive: polling that touched
 * the session would mean the idle timeout never fired. /session/status reads the
 * remaining time without extending it. Only the explicit "Stay signed in" button, and
 * ordinary page activity, extend the session.
 */

(function () {
  "use strict";

  var script = document.currentScript;
  var csrfToken = script ? script.getAttribute("data-csrf-token") : "";

  var dialog = document.getElementById("session-warning");
  var countdownEl = document.getElementById("session-warning-countdown");
  var extendButton = document.getElementById("session-warning-extend");

  if (!dialog || !countdownEl || !extendButton) {
    return;
  }

  var POLL_MS = 30000;
  var timer = null;
  var deadline = null;
  var warnAtSeconds = 120;

  function secondsRemaining() {
    if (deadline === null) {
      return null;
    }
    return Math.max(0, Math.round((deadline - Date.now()) / 1000));
  }

  function formatDuration(seconds) {
    var m = Math.floor(seconds / 60);
    var s = seconds % 60;
    if (m > 0) {
      return m + " min " + (s < 10 ? "0" : "") + s + " sec";
    }
    return s + " seconds";
  }

  function tick() {
    var remaining = secondsRemaining();
    if (remaining === null) {
      return;
    }

    if (remaining <= 0) {
      // The server has already expired it. Go to the login page rather than leave a
      // dead screen that silently fails on the next click.
      window.location.href = "/login";
      return;
    }

    if (remaining <= warnAtSeconds) {
      dialog.hidden = false;
      countdownEl.textContent = formatDuration(remaining);
    } else {
      dialog.hidden = true;
    }
  }

  function refresh() {
    fetch("/session/status", { credentials: "same-origin" })
      .then(function (r) {
        return r.ok ? r.json() : null;
      })
      .then(function (data) {
        if (!data) {
          return;
        }
        if (!data.authenticated) {
          window.location.href = "/login";
          return;
        }
        deadline = Date.now() + data.seconds_remaining * 1000;
        if (typeof data.warn_at_seconds === "number") {
          warnAtSeconds = data.warn_at_seconds;
        }
        tick();
      })
      .catch(function () {
        // Network blip. Keep the last known deadline and try again next poll.
      });
  }

  extendButton.addEventListener("click", function () {
    fetch("/session/extend", {
      method: "POST",
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrfToken },
    })
      .then(function (r) {
        if (!r.ok) {
          window.location.href = "/login";
          return null;
        }
        return r.json();
      })
      .then(function (data) {
        if (data && data.extended) {
          deadline = Date.now() + data.seconds_remaining * 1000;
          dialog.hidden = true;
        }
      })
      .catch(function () {
        window.location.href = "/login";
      });
  });

  refresh();
  setInterval(refresh, POLL_MS);
  timer = setInterval(tick, 1000);

  window.addEventListener("pagehide", function () {
    if (timer) {
      clearInterval(timer);
    }
  });
})();
