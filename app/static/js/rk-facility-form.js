/*
 * Shared client-side helpers for the Add/Edit Critical Facility modal, used
 * by both bdrrmo/facilities.html and admin/map.html so the two pages don't
 * duplicate the same validation/reset logic. Each page keeps its own
 * Leaflet/map-picker wiring inline — this module only owns the parts that
 * are identical everywhere: numeric/length validation for the capacity,
 * floor-area, reference and notes fields, and the "switch modal between
 * Add and Edit mode" bookkeeping.
 *
 * Server-side validation (app/services/facility_details.py) remains the
 * source of truth — this is a lightweight first line of feedback only.
 * No build step / bundler in this project — a plain global-attaching IIFE,
 * loaded via a same-origin <script src> tag (no nonce needed, same as
 * rk-map-search.js).
 */
(function (global) {
    'use strict';

    // Mirrors the server's `_LEGACY_RANGE_RE` / `_WHOLE_NUMBER_RE` in
    // app/services/facility_details.py — a plain whole number, or an
    // already-imported range like "40-80" left untouched.
    const LEGACY_RANGE_RE = /^\d+\s*-\s*\d+$/;
    const WHOLE_NUMBER_RE = /^\d+$/;

    function setFieldValidity(input, ok, message) {
        input.classList.toggle('is-invalid', !ok);
        input.classList.toggle('is-valid', ok && input.value.trim() !== '');
        const feedback = input.parentElement && input.parentElement.querySelector('.invalid-feedback');
        if (feedback && message) feedback.textContent = message;
        return ok;
    }

    function validateCapacityField(input) {
        const v = input.value.trim();
        if (v === '') return setFieldValidity(input, true);
        const ok = LEGACY_RANGE_RE.test(v) || WHOLE_NUMBER_RE.test(v);
        return setFieldValidity(input, ok, 'Enter a whole number (0 or greater).');
    }

    function validateFloorArea(input) {
        const v = input.value.trim();
        if (v === '') return setFieldValidity(input, true);
        const n = Number(v);
        const decimals = (v.split('.')[1] || '').length;
        const ok = v !== '' && !Number.isNaN(n) && n >= 0 && decimals <= 2;
        return setFieldValidity(input, ok, 'Enter a non-negative number with up to 2 decimal places.');
    }

    function validateMaxLen(input, maxLen) {
        const ok = input.value.length <= maxLen;
        return setFieldValidity(input, ok, `Must be ${maxLen} characters or fewer.`);
    }

    function validateName(input) {
        const v = input.value.trim();
        const ok = v.length >= 3 && v.length <= 100;
        return setFieldValidity(input, ok, 'Enter between 3 and 100 characters.');
    }

    function bindCharCounter(textarea, counterEl, maxLen) {
        function update() {
            counterEl.textContent = `${textarea.value.length} / ${maxLen}`;
        }
        textarea.addEventListener('input', update);
        update();
    }

    // Clears every field in `fields` back to its Add-mode default. Never
    // uses form.reset() — when the modal loaded in Edit mode, the fields'
    // baked-in HTML `value` attributes ARE the edited facility's data, so
    // native reset() would revert to that instead of blank.
    function clearFields(fields) {
        Object.keys(fields).forEach(function (key) {
            const el = fields[key];
            if (!el) return;
            if (el.type === 'checkbox') {
                el.checked = false;
            } else if (el.tagName === 'SELECT') {
                el.selectedIndex = 0;
            } else {
                el.value = '';
            }
            el.classList.remove('is-invalid', 'is-valid');
        });
    }

    // Switches the modal chrome (title / submit label / form action)
    // between Add and Edit mode.
    function configureModal(opts) {
        const isEdit = opts.mode === 'edit';
        opts.titleEl.textContent = isEdit ? opts.editTitle : opts.addTitle;
        opts.submitBtnEl.innerHTML = isEdit ? opts.editSubmitHtml : opts.addSubmitHtml;
        opts.formEl.action = isEdit ? opts.editAction : opts.addAction;
    }

    function validateDetailFields(fields) {
        let ok = true;
        if (fields.name) ok = validateName(fields.name) && ok;
        if (fields.floorArea) ok = validateFloorArea(fields.floorArea) && ok;
        ['capacityFamilies', 'capacityIndividuals', 'ereidFamilies', 'ereidIndividuals'].forEach(function (key) {
            if (fields[key]) ok = validateCapacityField(fields[key]) && ok;
        });
        if (fields.hazardReference) ok = validateMaxLen(fields.hazardReference, 255) && ok;
        if (fields.eoMoaMouReference) ok = validateMaxLen(fields.eoMoaMouReference, 150) && ok;
        if (fields.notes) ok = validateMaxLen(fields.notes, 1000) && ok;
        return ok;
    }

    function bindLiveValidation(fields) {
        if (fields.name) fields.name.addEventListener('input', function () { validateName(fields.name); });
        if (fields.floorArea) fields.floorArea.addEventListener('input', function () { validateFloorArea(fields.floorArea); });
        ['capacityFamilies', 'capacityIndividuals', 'ereidFamilies', 'ereidIndividuals'].forEach(function (key) {
            if (fields[key]) fields[key].addEventListener('input', function () { validateCapacityField(fields[key]); });
        });
        if (fields.hazardReference) fields.hazardReference.addEventListener('input', function () { validateMaxLen(fields.hazardReference, 255); });
        if (fields.eoMoaMouReference) fields.eoMoaMouReference.addEventListener('input', function () { validateMaxLen(fields.eoMoaMouReference, 150); });
        if (fields.notes) fields.notes.addEventListener('input', function () { validateMaxLen(fields.notes, 1000); });
    }

    global.RkFacilityForm = {
        validateCapacityField: validateCapacityField,
        validateFloorArea: validateFloorArea,
        validateMaxLen: validateMaxLen,
        validateName: validateName,
        validateDetailFields: validateDetailFields,
        bindLiveValidation: bindLiveValidation,
        bindCharCounter: bindCharCounter,
        clearFields: clearFields,
        configureModal: configureModal,
    };
})(window);
