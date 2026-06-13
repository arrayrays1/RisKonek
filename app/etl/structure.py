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


# ─────────────────────────────────────────────────────────────────────
# Best-effort PDF text → single field row (Week 8.1)
#
# Free-text PDFs aren't tabular, so structure_rows() can't help. This is a
# deliberately conservative, rule-based pass over the raw text: it only
# fills a field when a value is *confidently* detected, and otherwise
# leaves the empty-row default untouched. It assists manual review — it
# does not replace it. Scope is limited to: barangay, disaster_type,
# date_occurred, affected_families, casualties.
# ─────────────────────────────────────────────────────────────────────

# Label aliases used to anchor label-based detection in free text.
_TEXT_LABELS = {
    "date_occurred":     ["date occurred", "date of incident", "date of occurrence",
                          "incident date", "date"],
    "affected_families": ["affected families", "families affected", "no. of families",
                          "number of families", "families"],
    "casualties":        ["casualties", "no. of casualties", "number of casualties",
                          "deaths", "fatalities"],
    "barangay":          ["barangay", "brgy.", "brgy", "location"],
}

# A date token in common PH report formats (numeric or month-name).
_DATE_TOKEN = (
    r"(\d{4}-\d{1,2}-\d{1,2}"
    r"|\d{1,2}[/-]\d{1,2}[/-]\d{2,4}"
    r"|(?:January|February|March|April|May|June|July|August|September|October|"
    r"November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.?\s+\d{1,2},?\s+\d{4})"
)


def _label_value(text: str, labels: List[str], value_pattern: str) -> Optional[str]:
    """Return the first 'label : value' match for any alias, else None.

    Anchors on the label so we never grab stray values from prose. The
    separator may be ':' or '-' (or whitespace), value taken from the
    same line.
    """
    for label in labels:
        pat = re.compile(
            rf"{re.escape(label)}\s*[:\-]?\s*({value_pattern})",
            re.IGNORECASE,
        )
        m = pat.search(text)
        if m:
            return m.group(1).strip()
    return None


def _detect_barangay(text: str, known_barangays: List[str]) -> str:
    """Prefer matching a known barangay name (finite, reliable); fall back
    to a 'Barangay: <name>' label. Returns '' if not confident."""
    low = text.lower()
    # Longest names first so 'San Antonio' wins over a substring 'San'.
    for name in sorted(known_barangays or [], key=len, reverse=True):
        if name and re.search(rf"\b{re.escape(name.lower())}\b", low):
            return name
    labelled = _label_value(text, _TEXT_LABELS["barangay"], r"[A-Za-z .'\-]{3,40}")
    if labelled:
        # Trim trailing noise; keep it as a suggestion for the reviewer.
        return labelled.strip(" .-")
    return ""


def _detect_disaster_type(text: str) -> str:
    """Confident only when exactly ONE distinct disaster type is found
    (word-boundary keyword match). Ambiguous/none → 'other' default."""
    low = text.lower()
    found = set()
    for keyword, value in _DISASTER_KEYWORDS.items():
        if re.search(rf"\b{re.escape(keyword)}\b", low):
            found.add(value)
    if len(found) == 1:
        return next(iter(found))
    return DisasterType.other.value


def structure_text(text: str, known_barangays: Optional[List[str]] = None) -> Dict[str, Any]:
    """Best-effort single field row from free PDF text. Starts from the
    blank row and overrides only confidently detected fields."""
    row = empty_field_row()
    if not text or not text.strip():
        return row

    # Barangay — known-list match or label.
    brgy = _detect_barangay(text, known_barangays or [])
    if brgy:
        row["barangay"] = brgy

    # Disaster type — only if exactly one is detected.
    row["disaster_type"] = _detect_disaster_type(text)

    # Date — label-anchored first, else the first clear date token.
    date_raw = _label_value(text, _TEXT_LABELS["date_occurred"], _DATE_TOKEN)
    if not date_raw:
        m = re.search(_DATE_TOKEN, text)
        date_raw = m.group(1) if m else None
    if date_raw:
        parsed = _to_date_string(date_raw)
        # Only accept a normalized YYYY-MM-DD; leave blank if unparseable.
        if re.match(r"^\d{4}-\d{2}-\d{2}$", parsed):
            row["date_occurred"] = parsed

    # Numeric fields — label-anchored only (never bare numbers from prose).
    # Strip thousands separators so "1,250" parses as 1250, not 1.
    fam = _label_value(text, _TEXT_LABELS["affected_families"], r"\d[\d,]*")
    if fam is not None:
        row["affected_families"] = _to_int(fam.replace(",", ""))
    cas = _label_value(text, _TEXT_LABELS["casualties"], r"\d[\d,]*")
    if cas is not None:
        row["casualties"] = _to_int(cas.replace(",", ""))

    return row
