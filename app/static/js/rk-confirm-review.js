// rk-confirm-review.js — "review before you save" confirmation step.
//
// Opt in per form:   <form data-confirm-review="Review New User Details"> … </form>
// The attribute value is used as the modal's title; leave it empty
// (data-confirm-review) for a generic "Review Before Saving" title.
//
// On submit, instead of posting straight away, the form's own fields are
// read back and shown to the user as a plain-language summary ("Name: Juan
// Dela Cruz", "Role: BDRRMO / Barangay Focal", …) inside a modal, so they
// can catch a typo or a wrong dropdown before it's saved. "Go Back & Edit"
// closes the modal with nothing sent. "Confirm & Save" re-submits the same
// form (preserving whichever submit button was used, e.g. Save Draft vs.
// Submit for Review) and this time it goes through untouched.
//
// Per-field opt-outs/overrides, all optional:
//   data-review-skip                 never show this field in the summary
//   data-review-label="Custom Label" show this label instead of the auto-
//                                     detected one
//   data-review-always                include a checkbox even when unchecked
//                                     (default: unchecked boxes are omitted)
//
// Runs after rk-forms.js's data-validate capture-phase check, so an invalid
// form never reaches the review modal — the browser's normal inline errors
// show first, exactly as before.
(function () {
    "use strict";

    if (typeof document === "undefined") return;

    // Forms currently mid-"confirm and resubmit" — let their next submit
    // event through instead of intercepting it again.
    var bypass = new WeakSet();

    var modalEl = null;
    var bsModal = null;
    var pendingForm = null;
    var pendingSubmitter = null;

    function humanizeName(name) {
        if (!name) return "";
        var base = name.replace(/\[\]$/, "").split(/[_\-\.]/).join(" ");
        return base.replace(/\w\S*/g, function (w) {
            return w.charAt(0).toUpperCase() + w.slice(1);
        });
    }

    // True when the field is currently invisible (e.g. a role-conditional
    // section that's toggled with .d-none / display:none) — such fields
    // hold stale values the user never actually set, so skip them.
    function isHidden(field) {
        return field.offsetParent === null && field.type !== "hidden" &&
            window.getComputedStyle(field).position !== "fixed";
    }

    function labelFor(field) {
        var override = field.getAttribute("data-review-label");
        if (override) return override;

        if (field.id) {
            var byFor = field.ownerDocument.querySelector('label[for="' + CSS.escape(field.id) + '"]');
            if (byFor) return cleanLabelText(byFor);
        }
        var wrapping = field.closest ? field.closest("label") : null;
        if (wrapping) return cleanLabelText(wrapping);

        return humanizeName(field.name) || "Value";
    }

    // Strips the "*" required marker, "(optional)" hints, and helper icons
    // that templates put inside <label> for display, keeping just the text.
    function cleanLabelText(labelEl) {
        var clone = labelEl.cloneNode(true);
        clone.querySelectorAll(".text-danger, .text-muted, small, i").forEach(function (n) {
            n.remove();
        });
        return (clone.textContent || "").replace(/\s+/g, " ").replace(/[:*]\s*$/, "").trim();
    }

    function fileListText(field) {
        if (!field.files || field.files.length === 0) return "(no file selected)";
        var names = [];
        for (var i = 0; i < field.files.length; i++) names.push(field.files[i].name);
        return names.join(", ");
    }

    // Builds the ordered list of {label, value} rows to show for one form.
    // Radio/checkbox groups sharing a name are collapsed into a single row.
    function collectRows(form) {
        var rows = [];
        var seenRadioGroups = {};
        var fields = form.querySelectorAll("input, select, textarea");

        fields.forEach(function (field) {
            if (field.disabled) return;
            if (field.type === "hidden" || field.type === "submit" ||
                field.type === "button" || field.type === "reset") return;
            if (field.hasAttribute("data-review-skip")) return;
            if (isHidden(field)) return;

            if (field.type === "radio") {
                if (seenRadioGroups[field.name]) return;
                seenRadioGroups[field.name] = true;
                var checkedRadio = form.querySelector(
                    'input[type="radio"][name="' + CSS.escape(field.name) + '"]:checked');
                if (!checkedRadio) return;
                var radioLabel = checkedRadio.getAttribute("data-review-label") ||
                    labelFor(checkedRadio.closest("[data-review-group-label]") || checkedRadio);
                rows.push({ label: humanizeName(field.name), value: radioLabel });
                return;
            }

            if (field.type === "checkbox") {
                if (!field.checked && !field.hasAttribute("data-review-always")) return;
                rows.push({
                    label: labelFor(field),
                    value: field.checked ? "Yes" : "No"
                });
                return;
            }

            if (field.type === "file") {
                rows.push({ label: labelFor(field), value: fileListText(field) });
                return;
            }

            if (field.tagName === "SELECT") {
                var texts = [];
                for (var i = 0; i < field.selectedOptions.length; i++) {
                    var opt = field.selectedOptions[i];
                    if (opt.value === "") continue; // "-- Select --" placeholder
                    texts.push((opt.textContent || "").trim());
                }
                if (texts.length === 0) return;
                rows.push({ label: labelFor(field), value: texts.join(", ") });
                return;
            }

            if (field.type === "password") {
                if (!field.value) return; // edit form left blank = keep current
                rows.push({ label: labelFor(field), value: "•".repeat(Math.min(field.value.length, 12)) });
                return;
            }

            var val = (field.value || "").trim();
            if (val === "") return; // nothing entered — no need to review it
            rows.push({ label: labelFor(field), value: val });
        });

        return rows;
    }

    function ensureModal() {
        if (modalEl) return;
        modalEl = document.createElement("div");
        modalEl.className = "modal fade";
        modalEl.id = "rkConfirmReviewModal";
        modalEl.tabIndex = -1;
        modalEl.setAttribute("aria-hidden", "true");
        modalEl.innerHTML =
            '<div class="modal-dialog modal-dialog-scrollable modal-dialog-centered">' +
            '  <div class="modal-content">' +
            '    <div class="modal-header">' +
            '      <h5 class="modal-title"><i class="bi bi-clipboard-check me-1"></i><span data-rk-review-title>Review Before Saving</span></h5>' +
            '      <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>' +
            '    </div>' +
            '    <div class="modal-body">' +
            '      <p class="text-muted small mb-3">Please double-check the details below before saving.</p>' +
            '      <dl class="row mb-0 rk-review-list" data-rk-review-body></dl>' +
            '    </div>' +
            '    <div class="modal-footer">' +
            '      <button type="button" class="btn btn-outline-secondary" data-bs-dismiss="modal">' +
            '        <i class="bi bi-pencil"></i> Go Back &amp; Edit' +
            '      </button>' +
            '      <button type="button" class="btn btn-rk" data-rk-review-confirm>' +
            '        <i class="bi bi-check2"></i> Confirm &amp; Save' +
            '      </button>' +
            '    </div>' +
            '  </div>' +
            '</div>';
        document.body.appendChild(modalEl);

        modalEl.querySelector("[data-rk-review-confirm]").addEventListener("click", function () {
            if (!pendingForm) return;
            var form = pendingForm, submitter = pendingSubmitter;
            pendingForm = null;
            pendingSubmitter = null;
            bypass.add(form);
            if (bsModal) bsModal.hide();
            if (typeof form.requestSubmit === "function") {
                try {
                    form.requestSubmit(submitter || undefined);
                } catch (err) {
                    form.submit();
                }
            } else {
                form.submit();
            }
        });

        // If the user dismisses without confirming, forget the pending form
        // so a later, unrelated submit doesn't accidentally bypass review.
        modalEl.addEventListener("hidden.bs.modal", function () {
            pendingForm = null;
            pendingSubmitter = null;
        });
    }

    function showReview(form, submitter) {
        ensureModal();
        var title = form.getAttribute("data-confirm-review");
        modalEl.querySelector("[data-rk-review-title]").textContent =
            title && title.trim() ? title.trim() : "Review Before Saving";

        var body = modalEl.querySelector("[data-rk-review-body]");
        body.innerHTML = "";
        var rows = collectRows(form);

        if (rows.length === 0) {
            var empty = document.createElement("p");
            empty.className = "text-muted small mb-0";
            empty.textContent = "No details to show — go ahead and confirm.";
            body.appendChild(empty);
        } else {
            rows.forEach(function (row) {
                var dt = document.createElement("dt");
                dt.className = "col-5 col-sm-4 text-muted fw-normal small text-truncate";
                dt.textContent = row.label;
                var dd = document.createElement("dd");
                dd.className = "col-7 col-sm-8 mb-2";
                dd.style.whiteSpace = "pre-wrap";
                dd.style.wordBreak = "break-word";
                dd.textContent = row.value;
                body.appendChild(dt);
                body.appendChild(dd);
            });
        }

        pendingForm = form;
        pendingSubmitter = submitter || null;

        if (window.bootstrap && window.bootstrap.Modal) {
            bsModal = window.bootstrap.Modal.getOrCreateInstance(modalEl);
            bsModal.show();
        } else {
            // Bootstrap JS didn't load for some reason — fail safe by letting
            // the save go through rather than trapping the user.
            form.submit();
        }
    }

    // Remember which control triggered the submit so we can honor multi-
    // submit-button forms (e.g. "Save Draft" vs "Submit for Review") when
    // we re-submit after confirmation.
    var lastSubmitter = null;
    document.addEventListener("click", function (e) {
        var btn = e.target.closest ?
            e.target.closest('button[type="submit"], input[type="submit"]') : null;
        if (btn) lastSubmitter = btn;
    }, true);

    document.addEventListener("submit", function (e) {
        var form = e.target;
        if (!form || !form.matches || !form.matches("form[data-confirm-review]")) return;

        // Some forms carry their own inline onsubmit/native confirm (e.g. a
        // "mark as resolved?" prompt) that fires before this delegated
        // listener does. If that already cancelled the submit, respect it —
        // don't show a review modal for a submission the user just declined.
        if (e.defaultPrevented) return;

        if (bypass.has(form)) {
            bypass.delete(form);
            return; // this is the confirmed resubmission — let it go through
        }

        var submitter = e.submitter ||
            (form.contains(lastSubmitter) ? lastSubmitter : null) ||
            form.querySelector('button[type="submit"], input[type="submit"]');

        e.preventDefault();
        showReview(form, submitter);
    }, false); // bubble phase — runs after rk-forms.js's capture-phase validation
})();
