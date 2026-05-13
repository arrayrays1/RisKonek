"""
Coordinate parser for the CDRRMO critical-facility Excel.

The source file mixes several formats in a single column:
    "Lat 14.336771\nLong 121.048821"
    "Lat 14.3675\nLong 121.0507"
    "14°19'46.6\"N 121°01'25.4\"E"
    "Lat 14. 333917\nLong 121.018022\n"   (stray space)
    "Lat 14.2126.45\nLong 121.31339"      (typo — invalid)

San Pedro, Laguna sits roughly inside:
    lat 14.30 – 14.40    lng 121.00 – 121.10
Anything outside that envelope is treated as a parse failure so we can
fall back to the barangay centroid and flag the row for review.
"""
import re
from typing import Optional, Tuple

# Generous bounding box around San Pedro. Used to reject typos like
# "14.034357" (off by 0.3°) and "14.2126.45" (extra dot).
SAN_PEDRO_BBOX = {
    "lat_min": 14.28,
    "lat_max": 14.42,
    "lng_min": 120.98,
    "lng_max": 121.12,
}

# The Excel was exported on a non-UTF8 codepage in places, so the degree
# sign sometimes round-trips as � or '*'. Accept all of those.
_DEG_CHARS = "°ºo�*"

_DECIMAL_RE = re.compile(
    r"lat[\s:]*([+-]?\d[\d\s.]*)[,\s\n]+long?[\s:]*([+-]?\d[\d\s.]*)",
    re.IGNORECASE,
)

_DMS_RE = re.compile(
    r"(\d{1,3})[" + _DEG_CHARS + r"]\s*"
    r"(\d{1,2})'\s*"
    r"(\d{1,2}(?:\.\d+)?)\"?\s*"
    r"([NS])\s*"
    r"(\d{1,3})[" + _DEG_CHARS + r"]\s*"
    r"(\d{1,2})'\s*"
    r"(\d{1,2}(?:\.\d+)?)\"?\s*"
    r"([EW])",
    re.IGNORECASE,
)


def _clean_number(raw: str) -> Optional[float]:
    """Strip stray whitespace inside a number ('14. 333917' → 14.333917).
    Returns None if more than one decimal point is present after cleaning
    (e.g. '14.2126.45' — a typo in the source file).
    """
    if raw is None:
        return None
    compact = re.sub(r"\s+", "", raw)
    if compact.count(".") > 1:
        return None
    try:
        return float(compact)
    except ValueError:
        return None


def _in_bbox(lat: float, lng: float) -> bool:
    return (
        SAN_PEDRO_BBOX["lat_min"] <= lat <= SAN_PEDRO_BBOX["lat_max"]
        and SAN_PEDRO_BBOX["lng_min"] <= lng <= SAN_PEDRO_BBOX["lng_max"]
    )


def parse_coordinates(text) -> Tuple[Optional[float], Optional[float], Optional[str]]:
    """Return (lat, lng, reason).

    If parsing succeeds and the point falls inside the San Pedro bbox,
    reason is None. Otherwise (lat, lng) are None and reason explains
    why the caller should fall back to the barangay centroid.
    """
    if text is None:
        return None, None, "empty"
    s = str(text).strip()
    if not s or s.lower() == "nan":
        return None, None, "empty"

    # DMS first — it would otherwise be partially matched by the decimal
    # regex on the leading "14" digits.
    m = _DMS_RE.search(s)
    if m:
        d1, m1, s1, h1, d2, m2, s2, h2 = m.groups()
        lat = int(d1) + int(m1) / 60 + float(s1) / 3600
        lng = int(d2) + int(m2) / 60 + float(s2) / 3600
        if h1.upper() == "S":
            lat = -lat
        if h2.upper() == "W":
            lng = -lng
        if _in_bbox(lat, lng):
            return round(lat, 6), round(lng, 6), None
        return None, None, f"out-of-bbox ({lat:.4f}, {lng:.4f})"

    m = _DECIMAL_RE.search(s)
    if m:
        lat = _clean_number(m.group(1))
        lng = _clean_number(m.group(2))
        if lat is None or lng is None:
            return None, None, "malformed-number"
        if _in_bbox(lat, lng):
            return round(lat, 6), round(lng, 6), None
        return None, None, f"out-of-bbox ({lat:.4f}, {lng:.4f})"

    return None, None, "unrecognized-format"
