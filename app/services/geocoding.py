"""Geocoding / nearby-POI lookups for the facility location picker and the
general San Pedro City map search — both scoped hard to San Pedro, Laguna.

Both providers are free, no-signup OpenStreetMap services — Nominatim for
forward/reverse geocoding, Overpass for category-filtered nearby landmarks.
Called ONLY from the server (never from the browser) so no CSP change is
needed and no provider detail is ever exposed to the client.

Same defensive principle as app/simulation/weather.py: these are a
nice-to-have on top of manual map-click selection. On any failure (timeout,
HTTP error, unexpected shape) a function returns an empty/None result — it
must NEVER raise into the request.

San Pedro boundary: SAN_PEDRO_VIEWBOX is the single source of truth for the
rectangular bounds (used for Nominatim's viewbox+bounded=1 AND as a fast
pre-check). is_within_san_pedro() additionally checks the real municipal
boundary polygon (app/static/data/san_pedro_boundary.geojson, fetched once
from OSM/Nominatim's polygon_geojson export for relation 1552543 — the actual
administrative boundary, not a hand-guessed rectangle) so a point in a
rectangle-adjacent sliver of Biñan/Muntinlupa/Carmona is correctly rejected.
If that file is ever missing, is_within_san_pedro() falls back to the
rectangle only (documented, not silent) rather than rejecting everything.

Each provider is throttled in-process to respect its usage policy (Nominatim:
~1 req/sec; Overpass: be a good citizen on the shared public instance) and
each result is cached briefly so repeated/duplicate searches across users
don't re-hit the network. Single-process cache/throttle — a multi-worker
deployment would move this to a shared store.
"""

import json
import re
import threading
import time
from pathlib import Path

import httpx
from sqlalchemy import or_

_USER_AGENT = "RisKonek-CDRRMO-SanPedro/1.0 (riskonek.capstonepup@gmail.com)"

_NOMINATIM_SEARCH_URL = "https://nominatim.openstreetmap.org/search"
_NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
_OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# Centralized San Pedro, Laguna bounds — the real bounding box of the OSM
# administrative boundary (relation 1552543), NOT a hand-guessed rectangle.
# This is the single source of truth for both the Nominatim viewbox and the
# frontend's maxBounds/pre-check — never duplicate these numbers elsewhere.
SAN_PEDRO_VIEWBOX = {
    "west": 121.0054681,
    "south": 14.3215296,
    "east": 121.1436651,
    "north": 14.4114052,
}

_BOUNDARY_PATH = Path(__file__).resolve().parent.parent / "static" / "data" / "san_pedro_boundary.geojson"

_TIMEOUT = 5.0
_OVERPASS_TIMEOUT = 6.0

_MAX_QUERY_LEN = 120
_MAX_RESULTS = 8
_MAX_LANDMARKS = 30
_MAX_RADIUS_M = 600

_SEARCH_CACHE_TTL = 10 * 60
_LANDMARK_CACHE_TTL = 5 * 60
_REVERSE_CACHE_TTL = 10 * 60

# Nominatim's usage policy caps absolute request rate at ~1/sec for the whole
# app (shared across every concurrent user), independent of Overpass.
_MIN_INTERVAL_NOMINATIM = 1.1
_MIN_INTERVAL_OVERPASS = 1.1

_nominatim_lock = threading.Lock()
_nominatim_last_call = 0.0
_overpass_lock = threading.Lock()
_overpass_last_call = 0.0

# Module-level caches: {key: (expires_at, value)}
_search_cache: dict = {}
_landmark_cache: dict = {}
_reverse_cache: dict = {}

# Lazily-loaded boundary ring: list of (lng, lat) tuples, or [] if the static
# file is missing/unparseable (see is_within_san_pedro's fallback note above).
_boundary_ring: list | None = None

# OSM tag -> human category label, restricted to categories useful as a
# facility-location reference (schools, hospitals, gov offices, etc.). This
# list is ONLY for the automatic nearby-landmarks layer — the general
# location search below is intentionally NOT filtered by this list.
_LANDMARK_CATEGORIES = {
    ("amenity", "school"): "School",
    ("amenity", "hospital"): "Hospital",
    ("amenity", "clinic"): "Health Clinic",
    ("amenity", "doctors"): "Health Clinic",
    ("amenity", "townhall"): "Barangay / Government Office",
    ("amenity", "police"): "Police Station",
    ("amenity", "fire_station"): "Fire Station",
    ("amenity", "place_of_worship"): "Church",
    ("amenity", "community_centre"): "Barangay / Government Office",
    ("leisure", "sports_centre"): "Covered Court / Sports Facility",
    ("leisure", "pitch"): "Covered Court / Sports Facility",
    ("office", "government"): "Barangay / Government Office",
}

# Address fields Nominatim may return (addressdetails=1) that indicate a
# result genuinely sits in San Pedro — used only to RANK results, never to
# admit them (coordinate boundary validation is always authoritative).
_ADDRESS_FIELDS_FOR_RANKING = ("city", "municipality", "town", "city_district", "county", "state")


def _throttle_nominatim():
    """Block just long enough to keep Nominatim calls >= 1.1s apart,
    across all concurrent requests (its usage policy caps ~1 req/sec)."""
    global _nominatim_last_call
    with _nominatim_lock:
        wait = _MIN_INTERVAL_NOMINATIM - (time.time() - _nominatim_last_call)
        if wait > 0:
            time.sleep(wait)
        _nominatim_last_call = time.time()


def _throttle_overpass():
    """Same throttle as Nominatim's, but for Overpass's shared instance."""
    global _overpass_last_call
    with _overpass_lock:
        wait = _MIN_INTERVAL_OVERPASS - (time.time() - _overpass_last_call)
        if wait > 0:
            time.sleep(wait)
        _overpass_last_call = time.time()


def _cache_get(cache: dict, key):
    entry = cache.get(key)
    if entry is None:
        return None
    expires_at, value = entry
    if time.time() >= expires_at:
        cache.pop(key, None)
        return None
    return value


def _cache_set(cache: dict, key, value, ttl: float):
    cache[key] = (time.time() + ttl, value)


def _valid_latlng(lat, lng) -> bool:
    try:
        lat = float(lat)
        lng = float(lng)
    except (TypeError, ValueError):
        return False
    return -90 <= lat <= 90 and -180 <= lng <= 180


def _load_boundary_ring() -> list:
    """Load+cache the San Pedro boundary polygon's outer ring as (lng, lat)
    tuples (GeoJSON coordinate order). Returns [] if the file is missing or
    unparseable — callers fall back to the rectangular bounds only."""
    global _boundary_ring
    if _boundary_ring is not None:
        return _boundary_ring
    try:
        with open(_BOUNDARY_PATH, encoding="utf-8") as f:
            feature = json.load(f)
        ring = feature["geometry"]["coordinates"][0]
        _boundary_ring = [(pt[0], pt[1]) for pt in ring]
    except Exception as exc:
        print(f"[geocoding] boundary polygon unavailable, using rectangle bounds only: {type(exc).__name__}")
        _boundary_ring = []
    return _boundary_ring


def get_san_pedro_bounds() -> dict:
    """The centralized rectangular bounds (see SAN_PEDRO_VIEWBOX)."""
    return dict(SAN_PEDRO_VIEWBOX)


def get_san_pedro_boundary() -> list:
    """The San Pedro boundary polygon ring as [[lat, lng], ...] (Leaflet
    coordinate order), for drawing the outline and doing a fast client-side
    pre-check. Empty list if the static boundary file is unavailable."""
    return [[lat, lng] for (lng, lat) in _load_boundary_ring()]


def _point_in_ring(x: float, y: float, ring: list) -> bool:
    """Standard PNPOLY ray-casting point-in-polygon test.
    ring: list of (x, y) tuples forming a closed (or auto-closing) polygon."""
    n = len(ring)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def is_within_san_pedro(lat, lng) -> bool:
    """Authoritative check: is (lat, lng) inside San Pedro, Laguna?

    Always checks the rectangular bounds first (cheap reject). If the real
    boundary polygon loaded successfully, also requires the point to be
    inside that polygon (rejects rectangle-corner slivers that actually
    belong to a neighboring city). Falls back to rectangle-only if the
    polygon file is missing — see module docstring."""
    if not _valid_latlng(lat, lng):
        return False
    lat, lng = float(lat), float(lng)
    b = SAN_PEDRO_VIEWBOX
    if not (b["west"] <= lng <= b["east"] and b["south"] <= lat <= b["north"]):
        return False
    ring = _load_boundary_ring()
    if not ring:
        return True
    return _point_in_ring(lng, lat, ring)


def _humanize(s) -> str:
    if not s:
        return ""
    return re.sub(r"_", " ", str(s)).title()


def search_san_pedro_locations(query: str, limit: int = 8) -> dict:
    """General-purpose place search — any OSM-known establishment, address,
    street, subdivision, barangay, or landmark — hard-restricted to San
    Pedro, Laguna. NOT filtered by the nearby-landmark category whitelist.

    Returns {"available": bool, "results": [...]}. `available` is False only
    when the provider itself could not be reached/parsed (vs. a genuine
    zero-result search, which is `available: True, results: []`).
    """
    query = (query or "").strip()
    if len(query) < 3:
        return {"available": True, "results": []}
    query = query[:_MAX_QUERY_LEN]
    limit = max(1, min(int(limit or 8), _MAX_RESULTS))

    cache_key = (query.lower(), limit)
    cached = _cache_get(_search_cache, cache_key)
    if cached is not None:
        return {"available": True, "results": cached}

    b = SAN_PEDRO_VIEWBOX
    viewbox = f"{b['west']},{b['north']},{b['east']},{b['south']}"

    try:
        _throttle_nominatim()
        resp = httpx.get(
            _NOMINATIM_SEARCH_URL,
            params={
                "format": "jsonv2",
                "q": query,
                "viewbox": viewbox,
                "bounded": 1,
                "countrycodes": "ph",
                "limit": max(limit * 2, 16),  # over-fetch; boundary/dedupe filtering below narrows it
                "addressdetails": 1,
            },
            headers={"User-Agent": _USER_AGENT},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        print(f"[geocoding] search_san_pedro_locations unavailable: {type(exc).__name__}")
        return {"available": False, "results": []}

    candidates = []
    seen = set()
    for item in payload:
        lat, lng = item.get("lat"), item.get("lon")
        if not _valid_latlng(lat, lng):
            continue
        lat, lng = float(lat), float(lng)
        if not is_within_san_pedro(lat, lng):
            continue

        display_name = str(item.get("display_name") or "")
        parts = [p.strip() for p in display_name.split(",")]
        primary_name = parts[0] if parts else display_name
        secondary_text = ", ".join(parts[1:]) if len(parts) > 1 else ""

        dedupe_key = (primary_name.lower(), round(lat, 4), round(lng, 4))
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        address = item.get("address") or {}
        address_confirms_san_pedro = any(
            "san pedro" in str(address.get(f, "")).lower() for f in _ADDRESS_FIELDS_FOR_RANKING
        )

        candidates.append({
            "display_name": display_name[:255],
            "primary_name": primary_name[:150],
            "secondary_text": secondary_text[:255],
            "latitude": lat,
            "longitude": lng,
            "category": _humanize(item.get("type")) or "Place",
            "type": str(item.get("class") or "place"),
            "_rank_boost": 1 if address_confirms_san_pedro else 0,
        })

    # Stable sort: address-confirmed-San-Pedro results first, otherwise keep
    # Nominatim's own relevance ordering.
    candidates.sort(key=lambda r: -r["_rank_boost"])
    results = [{k: v for k, v in r.items() if k != "_rank_boost"} for r in candidates[:limit]]

    _cache_set(_search_cache, cache_key, results, _SEARCH_CACHE_TTL)
    return {"available": True, "results": results}


def _search_registered_facilities(db, query: str, limit: int) -> list:
    """Name/address substring match against our OWN facilities table, so a
    registered facility (e.g. "Villa Olympia 1A Covered Court") is findable by
    the words in its name — something OSM/Nominatim can't do since it has never
    heard of our facilities. Citywide (not barangay-scoped): the picker uses
    these as location references. Returns [] on any DB error (never raises)."""
    from app.models import Facility  # local import: avoids import-time coupling

    like = f"%{query}%"
    try:
        rows = (
            db.query(Facility)
            .filter(
                Facility.is_active.is_(True),
                Facility.is_archived.is_(False),
                or_(Facility.name.ilike(like), Facility.address.ilike(like)),
            )
            .order_by(Facility.name)
            .limit(limit)
            .all()
        )
    except Exception as exc:
        print(f"[geocoding] registered-facility search failed: {type(exc).__name__}")
        return []

    results = []
    for f in rows:
        if not _valid_latlng(f.latitude, f.longitude):
            continue
        results.append({
            "display_name": (f"{f.name}, {f.address}" if f.address else f.name)[:255],
            "primary_name": str(f.name)[:150],
            "secondary_text": str(f.address or "")[:255],
            "latitude": float(f.latitude),
            "longitude": float(f.longitude),
            "category": "Registered Facility",
            "type": "facility",
        })
    return results


def search_locations(db, query: str, limit: int = _MAX_RESULTS) -> dict:
    """Combined location-picker search: our OWN registered facilities first
    (matched by name/address), then general OSM places. This is what the
    /api/location-search routes call; search_san_pedro_locations() remains the
    OSM-only building block. Registered facilities are tagged
    category="Registered Facility" so the frontend can label them."""
    query = (query or "").strip()
    if len(query) < 3:
        return {"available": True, "results": []}
    limit = max(1, min(int(limit or _MAX_RESULTS), _MAX_RESULTS))

    facility_results = _search_registered_facilities(db, query, limit)

    osm = search_san_pedro_locations(query, limit)
    osm_results = osm.get("results", [])

    # Drop OSM entries sitting on top of a facility we already listed.
    seen = {(round(r["latitude"], 4), round(r["longitude"], 4)) for r in facility_results}
    merged = list(facility_results)
    for r in osm_results:
        key = (round(r["latitude"], 4), round(r["longitude"], 4))
        if key in seen:
            continue
        seen.add(key)
        merged.append(r)

    return {"available": osm.get("available", True), "results": merged[:limit]}


def reverse_geocode_san_pedro(lat, lng) -> dict:
    """Best-effort reverse geocode for a manually-picked point, INSIDE San
    Pedro only. Returns a structured error (not a display_name) if the point
    is outside — coordinate boundary validation is authoritative here, not
    whatever text the reverse-geocoder happens to return.

    Returns {"available": bool, "display_name": str | None, "error": str?}.
    """
    if not _valid_latlng(lat, lng):
        return {"available": True, "display_name": None}
    lat, lng = float(lat), float(lng)

    if not is_within_san_pedro(lat, lng):
        return {
            "available": False,
            "display_name": None,
            "error": "Location must be within San Pedro City, Laguna.",
        }

    cache_key = (round(lat, 6), round(lng, 6))
    cached = _cache_get(_reverse_cache, cache_key)
    if cached is not None:
        return {"available": True, "display_name": cached}

    try:
        _throttle_nominatim()
        resp = httpx.get(
            _NOMINATIM_REVERSE_URL,
            params={"format": "jsonv2", "lat": lat, "lon": lng, "zoom": 18},
            headers={"User-Agent": _USER_AGENT},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        print(f"[geocoding] reverse_geocode_san_pedro unavailable: {type(exc).__name__}")
        return {"available": False, "display_name": None}

    display_name = payload.get("display_name")
    display_name = str(display_name)[:255] if display_name else None
    _cache_set(_reverse_cache, cache_key, display_name, _REVERSE_CACHE_TTL)
    return {"available": True, "display_name": display_name}


def nearby_landmarks(lat: float, lng: float, radius_m: int = 400) -> dict:
    """Category-filtered nearby reference landmarks around a point (schools,
    hospitals, gov offices, etc. only — see _LANDMARK_CATEGORIES). Distinct
    from search_san_pedro_locations(), which is general-purpose and
    unfiltered by category.

    Returns {"available": bool, "results": [...]}."""
    if not _valid_latlng(lat, lng):
        return {"available": True, "results": []}
    lat, lng = float(lat), float(lng)
    if not is_within_san_pedro(lat, lng):
        # Not an error — this is an optional reference layer, not a
        # location-confirmation gate. Also keeps this authenticated proxy
        # from being used to query arbitrary far-away points via Overpass.
        return {"available": True, "results": []}
    try:
        radius_m = int(radius_m)
    except (TypeError, ValueError):
        radius_m = 400
    radius_m = max(50, min(radius_m, _MAX_RADIUS_M))

    cache_key = (round(lat, 3), round(lng, 3), radius_m)
    cached = _cache_get(_landmark_cache, cache_key)
    if cached is not None:
        return {"available": True, "results": cached}

    tag_filters = "".join(
        f'node["{tag}"="{value}"](around:{radius_m},{lat},{lng});'
        for tag, value in _LANDMARK_CATEGORIES
    )
    query = f"[out:json][timeout:{int(_OVERPASS_TIMEOUT)}];({tag_filters});out center {_MAX_LANDMARKS};"

    try:
        _throttle_overpass()
        resp = httpx.post(
            _OVERPASS_URL,
            data={"data": query},
            headers={"User-Agent": _USER_AGENT},
            timeout=_OVERPASS_TIMEOUT,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        print(f"[geocoding] nearby_landmarks unavailable: {type(exc).__name__}")
        return {"available": False, "results": []}

    results = []
    for el in payload.get("elements", [])[:_MAX_LANDMARKS]:
        el_lat, el_lng = el.get("lat"), el.get("lon")
        if not _valid_latlng(el_lat, el_lng):
            continue
        tags = el.get("tags", {})
        category = None
        for (tag, value), label in _LANDMARK_CATEGORIES.items():
            if tags.get(tag) == value:
                category = label
                break
        name = tags.get("name")
        if not name:
            continue
        address_parts = [tags.get("addr:street"), tags.get("addr:city")]
        address = ", ".join(p for p in address_parts if p) or None
        results.append({
            "name": str(name)[:150],
            "category": category or "Landmark",
            "latitude": float(el_lat),
            "longitude": float(el_lng),
            "address": str(address)[:255] if address else None,
        })

    _cache_set(_landmark_cache, cache_key, results, _LANDMARK_CACHE_TTL)
    return {"available": True, "results": results}
