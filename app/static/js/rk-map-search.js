/*
 * Shared Leaflet helpers for the facility location picker (search) and the
 * nearby-landmarks reference layer, used by both bdrrmo/facilities.html
 * (Add/Edit Facility picker) and admin/map.html (read-only layer toggle).
 *
 * No build step / bundler in this project — a plain global-attaching IIFE,
 * loaded via a same-origin <script src> tag (CSP script-src 'self' already
 * covers it, no nonce needed — same as the unpkg Leaflet <script> tags).
 */
(function (global) {
    'use strict';

    function esc(v) {
        if (v === null || v === undefined) return '';
        return String(v)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    function debounce(fn, waitMs) {
        let timer = null;
        function debounced(...args) {
            if (timer) clearTimeout(timer);
            timer = setTimeout(() => { timer = null; fn.apply(null, args); }, waitMs);
        }
        debounced.cancel = function () {
            if (timer) { clearTimeout(timer); timer = null; }
        };
        return debounced;
    }

    // Standard PNPOLY ray-casting point-in-polygon test, mirroring the
    // server-side check in app/services/geocoding.py (is_within_san_pedro).
    // This is a fast CLIENT-SIDE pre-check only — the backend re-validates
    // every create/edit/reverse-geocode, so this is never the sole gate.
    // ring: [[lat, lng], ...] (as returned by get_san_pedro_boundary()).
    function pointInPolygon(lat, lng, ring) {
        if (!ring || ring.length < 3) return true; // no polygon loaded — don't block
        let inside = false;
        for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
            const yi = ring[i][0], xi = ring[i][1];
            const yj = ring[j][0], xj = ring[j][1];
            const intersect = ((yi > lat) !== (yj > lat)) &&
                (lng < (xj - xi) * (lat - yi) / (yj - yi) + xi);
            if (intersect) inside = !inside;
        }
        return inside;
    }

    // ── Location search combobox ────────────────────────────────────────
    // opts: { input, resultsEl, statusEl, buildUrl(query), onSelect(result),
    //         minChars, debounceMs, onLoadingChange(bool) }
    function RkLocationSearch(opts) {
        const input = opts.input;
        const resultsEl = opts.resultsEl;
        const statusEl = opts.statusEl || null;
        const minChars = opts.minChars || 3;
        const debounceMs = opts.debounceMs || 450;

        let results = [];
        let activeIndex = -1;
        let controller = null;
        const cache = new Map();

        function setLoading(isLoading) {
            if (opts.onLoadingChange) opts.onLoadingChange(isLoading);
        }

        function announce(text) {
            if (statusEl) statusEl.textContent = text;
        }

        function closeList() {
            resultsEl.classList.add('d-none');
            resultsEl.innerHTML = '';
            input.setAttribute('aria-expanded', 'false');
            input.removeAttribute('aria-activedescendant');
            activeIndex = -1;
        }

        function optionId(i) {
            return (input.id || 'rk-location-search') + '-opt-' + i;
        }

        function render() {
            if (results.length === 0) {
                resultsEl.innerHTML = '<li class="picker-search-empty" role="presentation">No matching locations found within San Pedro City.</li>';
                resultsEl.classList.remove('d-none');
                input.setAttribute('aria-expanded', 'true');
                input.removeAttribute('aria-activedescendant');
                return;
            }
            resultsEl.innerHTML = results.map((r, i) => {
                const main = r.primary_name || r.display_name || '';
                const sub = r.secondary_text || '';
                return `<li id="${optionId(i)}" role="option" class="picker-search-result" data-index="${i}">
                    <div class="picker-search-result-name">${esc(main)}</div>
                    ${sub ? `<div class="picker-search-result-sub">${esc(sub)}</div>` : ''}
                    ${r.category ? `<div class="picker-search-result-cat">${esc(r.category)}</div>` : ''}
                </li>`;
            }).join('');
            resultsEl.classList.remove('d-none');
            input.setAttribute('aria-expanded', 'true');
            setActive(0);
        }

        function setActive(i) {
            const items = resultsEl.querySelectorAll('.picker-search-result');
            items.forEach(el => el.classList.remove('active'));
            if (i >= 0 && i < items.length) {
                items[i].classList.add('active');
                input.setAttribute('aria-activedescendant', optionId(i));
                items[i].scrollIntoView({ block: 'nearest' });
                activeIndex = i;
            } else {
                activeIndex = -1;
            }
        }

        function select(i) {
            const result = results[i];
            if (!result) return;
            closeList();
            input.value = result.primary_name || result.display_name || '';
            if (opts.onSelect) opts.onSelect(result);
        }

        const UNAVAILABLE_MSG = 'Location search is temporarily unavailable. You can still click the map.';

        const runSearch = debounce(function (query) {
            if (controller) controller.abort();
            controller = new AbortController();
            setLoading(true);
            announce('Searching…');

            fetch(opts.buildUrl(query), { signal: controller.signal })
                .then(r => r.json())
                .then(data => {
                    setLoading(false);
                    results = Array.isArray(data.results) ? data.results : [];
                    cache.set(query.toLowerCase(), results);
                    if (data.available === false) {
                        resultsEl.innerHTML = `<li class="picker-search-empty" role="presentation">${esc(UNAVAILABLE_MSG)}</li>`;
                        resultsEl.classList.remove('d-none');
                        announce(UNAVAILABLE_MSG);
                        return;
                    }
                    render();
                    announce(results.length ? `${results.length} result${results.length === 1 ? '' : 's'} found` : 'No matching locations found within San Pedro City.');
                })
                .catch(err => {
                    if (err.name === 'AbortError') return;
                    setLoading(false);
                    resultsEl.innerHTML = `<li class="picker-search-empty" role="presentation">${esc(UNAVAILABLE_MSG)}</li>`;
                    resultsEl.classList.remove('d-none');
                    announce(UNAVAILABLE_MSG);
                });
        }, debounceMs);

        input.addEventListener('input', function () {
            const query = input.value.trim();
            if (query.length < minChars) {
                runSearch.cancel();
                if (controller) controller.abort();
                setLoading(false);
                closeList();
                announce('');
                return;
            }
            const cached = cache.get(query.toLowerCase());
            if (cached) {
                results = cached;
                render();
                announce(results.length ? `${results.length} result${results.length === 1 ? '' : 's'} found` : 'No matching locations found within San Pedro City.');
                return;
            }
            runSearch(query);
        });

        input.addEventListener('keydown', function (e) {
            const open = !resultsEl.classList.contains('d-none');
            if (e.key === 'ArrowDown') {
                if (!open || results.length === 0) return;
                e.preventDefault();
                setActive((activeIndex + 1) % results.length);
            } else if (e.key === 'ArrowUp') {
                if (!open || results.length === 0) return;
                e.preventDefault();
                setActive((activeIndex - 1 + results.length) % results.length);
            } else if (e.key === 'Enter') {
                if (open && activeIndex >= 0) {
                    e.preventDefault();
                    select(activeIndex);
                }
            } else if (e.key === 'Escape') {
                if (open) {
                    e.preventDefault();
                    closeList();
                }
            }
        });

        resultsEl.addEventListener('click', function (e) {
            const li = e.target.closest('.picker-search-result');
            if (!li) return;
            select(Number(li.dataset.index));
        });

        document.addEventListener('click', function (e) {
            if (!input.contains(e.target) && !resultsEl.contains(e.target)) closeList();
        });

        return {
            reset() {
                runSearch.cancel();
                if (controller) controller.abort();
                setLoading(false);
                input.value = '';
                closeList();
                announce('');
            },
        };
    }

    // ── Nearby landmarks reference layer ────────────────────────────────
    // opts: { map, layerGroup, buildUrl(lat,lng,radius), radius,
    //         onUseAsLocation(landmark) (optional — only in picker mode) }
    function RkNearbyLandmarks(opts) {
        const map = opts.map;
        const layerGroup = opts.layerGroup;
        const radius = opts.radius || 400;
        let enabled = false;
        let controller = null;
        const fetchedBoxes = new Set();

        function bboxKey() {
            const c = map.getCenter();
            return Math.round(c.lat * 1000) + ',' + Math.round(c.lng * 1000) + ',' + radius;
        }

        function popupHtml(landmark) {
            const rows = [
                `<div class="popup-name">${esc(landmark.name)}</div>`,
                `<div class="popup-sub">${esc(landmark.category)}</div>`,
            ];
            if (landmark.address) rows.push(`<div class="rk-fs-11 text-muted">${esc(landmark.address)}</div>`);
            rows.push('<div class="landmark-ref-label">Reference landmark — not registered as a critical facility</div>');
            let html = rows.join('');
            if (opts.onUseAsLocation) {
                html += `<div class="popup-actions">
                    <button type="button" class="btn btn-outline-secondary btn-sm landmark-use-btn">Use as selected location</button>
                </div>`;
            }
            return html;
        }

        function render(landmarks) {
            layerGroup.clearLayers();
            landmarks.forEach(landmark => {
                const marker = L.circleMarker([landmark.latitude, landmark.longitude], {
                    radius: 5,
                    color: '#ffffff',
                    weight: 1,
                    fillColor: '#9aa0a6',
                    fillOpacity: 0.85,
                }).addTo(layerGroup);
                marker.bindPopup(popupHtml(landmark), { maxWidth: 260 });
                if (opts.onUseAsLocation) {
                    marker.on('popupopen', function (e) {
                        const btn = e.popup.getElement().querySelector('.landmark-use-btn');
                        if (btn) btn.addEventListener('click', function () {
                            opts.onUseAsLocation(landmark);
                            marker.closePopup();
                        });
                    });
                }
            });
        }

        function fetchForCurrentView() {
            if (!enabled) return;
            const key = bboxKey();
            if (fetchedBoxes.has(key)) return;
            if (controller) controller.abort();
            controller = new AbortController();
            const c = map.getCenter();
            fetch(opts.buildUrl(c.lat, c.lng, radius), { signal: controller.signal })
                .then(r => r.json())
                .then(data => {
                    if (!enabled) return;
                    fetchedBoxes.add(key);
                    render(Array.isArray(data.results) ? data.results : []);
                })
                .catch(err => {
                    if (err.name === 'AbortError') return;
                    // Silent failure — nearby landmarks are reference-only;
                    // never block the registered facility markers or picker.
                });
        }

        const onMoveEnd = debounce(fetchForCurrentView, 500);

        return {
            enable() {
                if (enabled) return;
                enabled = true;
                fetchedBoxes.clear();
                layerGroup.addTo(map);
                map.on('moveend', onMoveEnd);
                fetchForCurrentView();
            },
            disable() {
                enabled = false;
                onMoveEnd.cancel();
                if (controller) controller.abort();
                map.off('moveend', onMoveEnd);
                layerGroup.clearLayers();
                map.removeLayer(layerGroup);
                fetchedBoxes.clear();
            },
        };
    }

    global.RkLocationSearch = RkLocationSearch;
    global.RkNearbyLandmarks = RkNearbyLandmarks;
    global.rkPointInPolygon = pointInPolygon;
})(window);
