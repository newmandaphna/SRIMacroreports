/* A show/hide toggle on every password field.
 *
 * Built in JavaScript rather than in the templates for two reasons. It finds every
 * password input on the page, so a field added later gets the toggle without anyone
 * remembering to wire it up. And a button that only exists when the script that drives
 * it has run cannot become a dead control for a user with JavaScript turned off, which
 * a button in the markup would be.
 *
 * NIST 800-63B recommends allowing this: a password you can check is a password you can
 * make longer, and length is what this application asks for (12 characters minimum, no
 * symbol requirements). Everything below is about making the revealed state obvious and
 * short lived, because this is a clinical setting and a screen is often not private.
 *
 * Revealed text is never sent anywhere, stored, or logged. Switching the input type is
 * a display change in the browser and nothing else.
 */

(function () {
  "use strict";

  var HIDDEN_LABEL = "Show password";
  var SHOWN_LABEL = "Hide password";

  function setState(input, button, reveal) {
    input.type = reveal ? "text" : "password";
    button.setAttribute("aria-pressed", reveal ? "true" : "false");
    button.setAttribute("aria-label", reveal ? SHOWN_LABEL : HIDDEN_LABEL);
    button.title = reveal ? SHOWN_LABEL : HIDDEN_LABEL;
    button.textContent = reveal ? "Hide" : "Show";
  }

  function enhance(input) {
    if (input.dataset.revealReady) {
      return;
    }
    input.dataset.revealReady = "1";

    var wrapper = document.createElement("div");
    wrapper.className = "password-field";
    input.parentNode.insertBefore(wrapper, input);
    wrapper.appendChild(input);

    var button = document.createElement("button");
    // Not a submit button. Inside a form the default type is "submit", so leaving this
    // off would make the toggle submit the login form instead of revealing anything.
    button.type = "button";
    button.className = "password-field__toggle";
    // The visible word is "Show" or "Hide", which does not say what it acts on when
    // there are three password fields on the change password page. The aria-label
    // carries the full phrase and aria-pressed carries the state.
    setState(input, button, false);
    wrapper.appendChild(button);

    button.addEventListener("click", function () {
      var reveal = button.getAttribute("aria-pressed") !== "true";
      setState(input, button, reveal);
      // Put the caret back where the user was. Reading the value and writing it
      // straight back moves the caret to the end, which is where someone who is still
      // typing wants it.
      input.focus();
      var value = input.value;
      input.value = "";
      input.value = value;
    });

    var form = input.form;
    if (form) {
      // Never leave a password on screen after the form is sent. The next page may be
      // a validation error that re-renders the field, and the browser can restore a
      // revealed value on a back navigation.
      form.addEventListener("submit", function () {
        setState(input, button, false);
      });
    }
  }

  function enhanceAll(root) {
    var inputs = (root || document).querySelectorAll('input[type="password"]');
    for (var i = 0; i < inputs.length; i++) {
      enhance(inputs[i]);
    }
  }

  enhanceAll(document);

  // Password fields are on full page loads today, but the rest of the application
  // swaps content in with htmx, so anything arriving that way is picked up too.
  document.body.addEventListener("htmx:afterSwap", function (event) {
    enhanceAll(event.target);
  });

  // Leaving the page with a password on screen and coming back to it revealed is the
  // one way the browser can undo the submit handler above.
  window.addEventListener("pagehide", function () {
    var shown = document.querySelectorAll(".password-field__toggle[aria-pressed=true]");
    for (var i = 0; i < shown.length; i++) {
      var input = shown[i].parentNode.querySelector("input");
      if (input) {
        setState(input, shown[i], false);
      }
    }
  });
})();
