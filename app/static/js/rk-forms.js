// rk-forms.js — CSP-safe replacements for inline on* event handlers.
//
// Loading this file (instead of inline onclick/onsubmit/onchange attributes)
// lets the Content-Security-Policy use script-src 'self' 'nonce-…' with NO
// 'unsafe-inline'. Behaviour is wired declaratively through data-* attributes:
//
//   <form data-confirm="Archive this?">              → confirm() before submit
//   <button data-confirm="Submit? No more edits.">   → confirm() before the
//                                                       button's submit fires
//   <select data-submit-on-change>                   → submit the form on change
//
// All listeners are delegated on document, so they also cover content rendered
// after load.
(function () {
    "use strict";

    // Confirm-before-submit at the FORM level (replaces onsubmit="return confirm()").
    document.addEventListener("submit", function (e) {
        var form = e.target;
        if (form && form.matches && form.matches("form[data-confirm]")) {
            if (!window.confirm(form.getAttribute("data-confirm"))) {
                e.preventDefault();
            }
        }
    });

    // Confirm-before-submit at the BUTTON level (replaces onclick="return confirm()").
    // Used where a single form has multiple submit buttons and only one needs a
    // prompt. Cancelling stops the click from triggering the form submission.
    document.addEventListener("click", function (e) {
        var btn = e.target.closest ? e.target.closest("[data-confirm]") : null;
        if (btn && btn.tagName !== "FORM") {
            if (!window.confirm(btn.getAttribute("data-confirm"))) {
                e.preventDefault();
                e.stopPropagation();
            }
        }
    });

    // Auto-submit the owning form when the control changes
    // (replaces onchange="this.form.submit()").
    document.addEventListener("change", function (e) {
        var el = e.target;
        if (el && el.matches && el.matches("[data-submit-on-change]") && el.form) {
            el.form.submit();
        }
    });
})();
