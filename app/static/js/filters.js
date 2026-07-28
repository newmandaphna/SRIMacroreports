/* Submit-on-change for the report filter bar.
 *
 * The form is a plain GET form and works fully without this file: pick a value,
 * press a button, get a shareable URL. This only removes the button press for the
 * dropdowns, because changing "Group by" and then hunting for a submit button is
 * the kind of friction that makes people stop exploring their own data.
 *
 * Date inputs are deliberately excluded: submitting half-typed dates on every
 * keystroke of a date field is worse than the Apply button.
 */

(function () {
  "use strict";

  var forms = document.querySelectorAll("form[data-auto-submit]");
  for (var i = 0; i < forms.length; i++) {
    forms[i].addEventListener("change", function (event) {
      var el = event.target;
      if (el.matches("select, input[type=checkbox]")) {
        // requestSubmit, not submit: it runs validation and, critically, submits
        // with no submitter, so the hidden current-preset input keeps the period.
        event.currentTarget.requestSubmit();
      }
    });
  }
})();
