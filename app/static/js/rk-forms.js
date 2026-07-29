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

    // Copy-to-clipboard for <button data-copy="0917…">. Briefly flips the
    // button's icon to a check so the user sees the copy landed. Used by the
    // Contact Directory's copy-number buttons.
    document.addEventListener("click", function (e) {
        var btn = e.target.closest ? e.target.closest("[data-copy]") : null;
        if (!btn) return;
        var value = btn.getAttribute("data-copy");
        if (!value) return;

        function flash() {
            var icon = btn.querySelector("i");
            if (!icon) return;
            var prev = icon.className;
            icon.className = "bi bi-check-lg";
            btn.classList.add("text-success");
            setTimeout(function () {
                icon.className = prev;
                btn.classList.remove("text-success");
            }, 1200);
        }

        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(value).then(flash, function () {});
        } else {
            // Fallback for older browsers / non-secure contexts.
            var ta = document.createElement("textarea");
            ta.value = value;
            ta.style.position = "fixed";
            ta.style.opacity = "0";
            document.body.appendChild(ta);
            ta.select();
            try { document.execCommand("copy"); flash(); } catch (err) { /* ignore */ }
            document.body.removeChild(ta);
        }
    });

    // ── Client-side form validation (opt-in via <form data-validate>) ──────
    //
    // Replaces the browser's default validation bubbles with Bootstrap-styled
    // inline messages, using the native Constraint Validation API plus a small
    // set of custom rules. Only forms carrying data-validate are touched, so
    // GET filter forms, single-button action forms, and the self-contained
    // facility modal (rk-facility-form.js) are left alone.
    //
    // Declare on a form:   <form method="POST" data-validate> … </form>
    // Extra per-field:     data-rule="phone-ph"   custom format check
    //                      data-error="…"         override the shown message
    //                      data-require-if="name" data-require-if-value="a,b"
    //                                             required only while the named
    //                                             sibling control holds one of
    //                                             those values
    //
    // A rule's `test` receives (value, field) so a rule can read the field's
    // own data-* attributes or other controls in the same form; `message` may
    // be a string or a function(value, field) for a value-dependent message.
    var CUSTOM_RULES = {
        // Philippine mobile number: 11 digits starting 09 (e.g. 09171234567).
        "phone-ph": {
            test: function (v) { return /^09\d{9}$/.test(v); },
            message: "Enter an 11-digit PH mobile number (e.g. 09171234567)."
        },
        // Equipment plate/serial: 3 letters then 3-4 digits, dash optional
        // (e.g. ABC-1234, SAA123).
        "plate-serial": {
            test: function (v) { return /^[A-Za-z]{3}-?[0-9]{3,4}$/.test(v); },
            message: "Use 3 letters then 3–4 digits (e.g. ABC-1234)."
        },
        // Logistics stock movement: a positive whole number that, when the
        // form's action is "deduct", never exceeds the current stock held in
        // data-max-stock. Adding stock has no ceiling.
        "stock-amount": {
            test: function (v, field) {
                var n = Number(v);
                if (!isFinite(n) || n <= 0 || Math.floor(n) !== n) return false;
                if (!isDeduction(field)) return true;
                var max = Number(field.getAttribute("data-max-stock"));
                return !isFinite(max) || n <= max;
            },
            message: function (v, field) {
                var n = Number(v);
                if (!isFinite(n) || n <= 0 || Math.floor(n) !== n) {
                    return "Enter a whole number greater than 0.";
                }
                return "Cannot deduct more than the current stock (" +
                    field.getAttribute("data-max-stock") + ").";
            }
        },
        // A movement can be backdated but never postdated.
        "not-future": {
            test: function (v) {
                var t = Date.parse(v);
                // Unparseable values are left to the native type check.
                if (isNaN(t)) return true;
                return t <= Date.now() + 60000;   // 1 min clock-skew allowance
            },
            message: "Date and time cannot be in the future."
        }
    };

    // True when the stock modal owning `field` is set to deduct.
    function isDeduction(field) {
        var form = field.form;
        var action = form && form.elements ? form.elements["action"] : null;
        return !!(action && action.value === "deduct");
    }

    // Apply data-require-if: toggle the native `required` attribute so the
    // conditional field is validated by the same code path as everything else.
    // Clears a stale error when the condition stops applying.
    function syncConditionalRequired(form) {
        form.querySelectorAll("[data-require-if]").forEach(function (field) {
            var other = form.elements[field.getAttribute("data-require-if")];
            var wanted = (field.getAttribute("data-require-if-value") || "").split(",");
            if (other && wanted.indexOf(other.value) !== -1) {
                field.setAttribute("required", "");
            } else {
                field.removeAttribute("required");
                clearInvalid(field);
            }
        });
    }

    // Find (or lazily create) the .invalid-feedback element that shows a field's
    // message. Templates need not declare one — if absent it is injected after
    // the field, or as the last child of an .input-group so Bootstrap displays it.
    function ensureFeedback(field) {
        var group = field.closest ? field.closest(".input-group") : null;
        var anchor = group || field;
        var sib = anchor.nextElementSibling;
        while (sib) {
            if (sib.classList && sib.classList.contains("invalid-feedback")) return sib;
            sib = sib.nextElementSibling;
        }
        if (group) {
            var existing = group.querySelector(".invalid-feedback");
            if (existing) return existing;
        }
        var fb = document.createElement("div");
        fb.className = "invalid-feedback";
        if (group) {
            group.classList.add("has-validation");
            group.appendChild(fb);
        } else if (anchor.parentNode) {
            anchor.parentNode.insertBefore(fb, anchor.nextSibling);
        }
        return fb;
    }

    function setInvalid(field, message) {
        field.classList.add("is-invalid");
        var fb = ensureFeedback(field);
        if (fb) fb.textContent = message;
    }

    function clearInvalid(field) {
        field.classList.remove("is-invalid");
    }

    // Returns true when the field is valid. Custom data-rule checks run first,
    // then native constraints (required / type / min / max / minlength / …).
    function validateField(field) {
        if (field.disabled || field.type === "hidden" ||
            field.type === "submit" || field.type === "button") return true;

        var rule = CUSTOM_RULES[field.getAttribute("data-rule")];
        if (rule) {
            var v = field.value.trim();
            // Custom format rules apply only to non-empty values; presence is
            // the `required` attribute's job (checked natively below).
            if (v !== "" && !rule.test(v, field)) {
                var msg = typeof rule.message === "function"
                    ? rule.message(v, field) : rule.message;
                setInvalid(field, field.getAttribute("data-error") || msg);
                return false;
            }
        }

        if (field.checkValidity && !field.checkValidity()) {
            setInvalid(field, field.getAttribute("data-error") || field.validationMessage);
            return false;
        }

        clearInvalid(field);
        return true;
    }

    function validateForm(form) {
        syncConditionalRequired(form);
        var fields = form.querySelectorAll("input, select, textarea");
        var ok = true, first = null;
        for (var i = 0; i < fields.length; i++) {
            if (!validateField(fields[i])) {
                ok = false;
                if (!first) first = fields[i];
            }
        }
        if (first) {
            try { first.focus(); } catch (e) { /* ignore */ }
            if (first.scrollIntoView) first.scrollIntoView({ block: "center", behavior: "smooth" });
        }
        return ok;
    }

    // Suppress native bubbles on opt-in forms so our inline messages are the
    // only feedback the user sees.
    document.querySelectorAll("form[data-validate]").forEach(function (form) {
        form.setAttribute("novalidate", "");
    });

    // Validate on submit-button click in the CAPTURE phase, before the bubbling
    // data-confirm handler above — so an invalid form never reaches its confirm
    // dialog. Covers forms with multiple submit buttons (draft / submit).
    document.addEventListener("click", function (e) {
        var btn = e.target.closest ?
            e.target.closest('button[type="submit"], input[type="submit"]') : null;
        if (!btn) return;
        var form = btn.form || (btn.closest ? btn.closest("form") : null);
        if (!form || !form.matches("form[data-validate]")) return;
        if (!validateForm(form)) {
            e.preventDefault();
            e.stopImmediatePropagation();
        }
    }, true);

    // Also catch Enter-key submits (no button click) in the capture phase.
    document.addEventListener("submit", function (e) {
        var form = e.target;
        if (form && form.matches && form.matches("form[data-validate]")) {
            if (!validateForm(form)) {
                e.preventDefault();
                e.stopImmediatePropagation();
            }
        }
    }, true);

    // Live re-validation: once a field is flagged, clear/update it as the user
    // fixes it, so the error doesn't linger until the next submit.
    function liveRevalidate(e) {
        var f = e.target;
        if (!f || !f.matches ||
            !f.matches("form[data-validate] input, form[data-validate] select, form[data-validate] textarea")) {
            return;
        }
        // Changing the controller of a data-require-if field (Action / New
        // status) flips which fields are mandatory and can invalidate an
        // already-typed amount, so re-check the whole form's flagged fields.
        if (f.form && f.form.querySelector('[data-require-if="' + f.name + '"]')) {
            syncConditionalRequired(f.form);
            f.form.querySelectorAll(".is-invalid").forEach(validateField);
            return;
        }
        if (f.classList.contains("is-invalid")) {
            validateField(f);
        }
    }
    document.addEventListener("input", liveRevalidate);
    document.addEventListener("change", liveRevalidate);
})();
