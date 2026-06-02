"""Rule-based row → Silver-layer field mapping.

The goal is to take the loose rows returned by extract_excel/extract_csv
(or text lines from extract_pdf) and produce a normalized list of
dicts that match the Admin review screen and the Incident model.

Output schema (per row):
    barangay              str
    disaster_type         str   (one of DisasterType values; falls back to 'other')
    date_occurred         str   ('YYYY-MM-DD' or '')
    affected_families     int
    affected_individuals  int   (stored in extracted_data only — no Incident column)
    casualties            int
    description           str
    resources_used        str   (stored in extracted_data only — no Incident column)
"""

from typing import Dict, List, Any, Optional
import re
from datetime import datetime, date

from app.models import DisasterType


# Map common synonyms to enum *value strings*. Use enum values directly
# so any future rename of the enum doesn't require updating this table.
_DISASTER_KEYWORDS = {
    "flood": DisasterType.flood.value,
    "flooding": DisasterType.flood.value,
    "deluge": DisasterType.flood.value,
    "fire": DisasterType.fire.value,
    "blaze": DisasterType.fire.value,
    "earthquake": DisasterType.earthquake.value,
    "quake": DisasterType.earthquake.value,
    "seismic": DisasterType.earthquake.value,
    "landslide": DisasterType.landslide.value,
    "mudslide": DisasterType.landslide.value,
    "typhoon": DisasterType.typhoon.value,
    "storm": DisasterType.typhoon.value,
    "cyclone": DisasterType.typhoon.value,
}


_FIELD_ALIASES = {
    "barangay":             ["barangay", "brgy", "barangay_name", "location"],
    "disaster_type":        ["disaster_type", "disaster", "hazard", "hazard_type",
                             "incident_type", "type"],
    "date_occurred":        ["date_occurred", "date", "incident_date",
                             "date_of_incident", "occurred_on"],
    "affected_families":    ["affected_families", "families_affected",
                             "no_of_families", "families"],
    "affected_individuals": ["affected_individuals", "individuals_affected",
                             "persons_affected", "individuals", "persons"],
    "casualties":           ["casualties", "deaths", "fatalities", "no_of_casualties"],
    "description":          ["description", "remarks", "notes", "narrative",
                             "details"],
    "resources_used":       ["resources_used", "resources", "equipment_used",
                             "supplies_used"],
}


def _norm_key(k: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (k or "").strip().lower()).strip("_")


def _pick(row: Dict[str, Any], canonical: str) -> Any:
    """Find a value in a row using the alias table, case/punctuation-insensitive."""
    normalized_row = {_norm_key(k): v for k, v in row.items()}
    for alias in _FIELD_ALIASES.get(canonical, [canonical]):
        v = normalized_row.get(_norm_key(alias))
        if v is not None and v != "":
            return v
    return None


def _to_int(v: Any) -> int:
    if v is None or v == "":
        return 0
    try:
        return int(float(v))
    except (TypeError, ValueError):
        m = re.search(r"-?\d+", str(v))
        return int(m.group(0)) if m else 0


def _to_date_string(v: Any) -> str:
    """Return 'YYYY-MM-DD' or '' if unparseable."""
    if v is None or v == "":
        return ""
    if isinstance(v, date) and not isinstance(v, datetime):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d")
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%m-%d-%Y",
                "%B %d, %Y", "%b %d, %Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    # last resort: ISO-ish prefix
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return m.group(0)
    return s  # leave as-is so the admin can fix it on the review screen


def _to_disaster_value(v: Any) -> str:
    """Map free-form text to a DisasterType *value*. Falls back to 'other'."""
    if v is None:
        return DisasterType.other.value
    s = str(v).strip().lower()
    if not s:
        return DisasterType.other.value
    # exact-value match first (covers stored enum values verbatim)
    for dt in DisasterType:
        if s == dt.value.lower():
            return dt.value
    for keyword, value in _DISASTER_KEYWORDS.items():
        if keyword in s:
            return value
    return DisasterType.other.value


def structure_row(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "barangay":             (str(_pick(row, "barangay") or "")).strip(),
        "disaster_type":        _to_disaster_value(_pick(row, "disaster_type")),
        "date_occurred":        _to_date_string(_pick(row, "date_occurred")),
        "affected_families":    _to_int(_pick(row, "affected_families")),
        "affected_individuals": _to_int(_pick(row, "affected_individuals")),
        "casualties":           _to_int(_pick(row, "casualties")),
        "description":          (str(_pick(row, "description") or "")).strip(),
        "resources_used":       (str(_pick(row, "resources_used") or "")).strip(),
    }


def structure_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [structure_row(r) for r in (rows or [])]


def empty_field_row() -> Dict[str, Any]:
    """Blank row for manual-entry fallback (used when extraction failed
    or PDF text has no recognizable tabular data)."""
    return {
        "barangay": "",
        "disaster_type": DisasterType.other.value,
        "date_occurred": "",
        "affected_families": 0,
        "affected_individuals": 0,
        "casualties": 0,
        "description": "",
        "resources_used": "",
    }
