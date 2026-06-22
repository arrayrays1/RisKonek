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
    date_raw              str   (original date text, for the review screen)
    date_ambiguous        bool  (True when DD/MM vs MM/DD cannot be decided)
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


def _is_ambiguous_dmy(s: str) -> bool:
    """True when a numeric date like '03/04/2025' could be read as either
    DD/MM/YYYY or MM/DD/YYYY — both leading parts are valid months and differ,
    so day-vs-month order cannot be decided. Year-first ISO strings
    (YYYY-MM-DD) and dates with a part > 12 are unambiguous and return False."""
    m = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-]\d{4}$", (s or "").strip())
    if not m:
        return False
    a, b = int(m.group(1)), int(m.group(2))
    return a <= 12 and b <= 12 and a != b


def _to_date_string(v: Any) -> str:
    """Return 'YYYY-MM-DD' or '' if unparseable.

    Ambiguous DD/MM-vs-MM/DD numeric dates return '' on purpose — we refuse
    to guess so the reviewer must disambiguate on the review screen."""
    if v is None or v == "":
        return ""
    if isinstance(v, date) and not isinstance(v, datetime):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d")
    s = str(v).strip()
    if _is_ambiguous_dmy(s):
        return ""
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
    raw_date = _pick(row, "date_occurred")
    date_str = ("" if raw_date is None else str(raw_date)).strip()
    return {
        "barangay":             (str(_pick(row, "barangay") or "")).strip(),
        "disaster_type":        _to_disaster_value(_pick(row, "disaster_type")),
        "date_occurred":        _to_date_string(raw_date),
        "date_raw":             date_str,
        "date_ambiguous":       _is_ambiguous_dmy(date_str),
        "affected_families":    _to_int(_pick(row, "affected_families")),
        "affected_individuals": _to_int(_pick(row, "affected_individuals")),
        "casualties":           _to_int(_pick(row, "casualties")),
        "description":          (str(_pick(row, "description") or "")).strip(),
        "resources_used":       (str(_pick(row, "resources_used") or "")).strip(),
    }


def structure_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [structure_row(r) for r in (rows or [])]


# ─────────────────────────────────────────────────────────────────────
# CFAU post-incident operational fields (Week 8.1 ETL pre-fill)
#
# These map to IncidentReport (the operational after-action report), NOT
# to Incident. They are separate from the core incident fields above so
# the same extraction module can feed both the Admin incident review and
# the CFAU report review without overlap. Tabular (Excel/CSV) only — free
# PDF text is handled separately by the route (AI summary / raw text).
# ─────────────────────────────────────────────────────────────────────

_INCIDENT_REPORT_ALIASES = {
    "operations_summary":     ["operations_summary", "operation_summary",
                               "operations", "operation", "summary"],
    "actions_taken":          ["actions_taken", "action_taken", "actions",
                               "response_actions"],
    "equipment_used":         ["equipment_used", "equipment", "assets_used"],
    "personnel_count":        ["personnel_count", "personnel_deployed",
                               "personnel", "no_of_personnel", "responders"],
    "personnel_notes":        ["personnel_notes", "personnel_remarks",
                               "team_notes"],
    "challenges_encountered": ["challenges_encountered", "challenges",
                               "difficulties", "issues", "field_risks"],
    "recommendations":        ["recommendations", "recommendation",
                               "suggestions"],
}


def _pick_aliased(row: Dict[str, Any], aliases: List[str]) -> Any:
    """Like _pick, but with an explicit alias list (case/punct-insensitive)."""
    normalized_row = {_norm_key(k): v for k, v in row.items()}
    for alias in aliases:
        v = normalized_row.get(_norm_key(alias))
        if v is not None and v != "":
            return v
    return None


def empty_incident_report_fields() -> Dict[str, Any]:
    """Blank operational-field set for the CFAU report form."""
    return {
        "operations_summary": "",
        "actions_taken": "",
        "equipment_used": "",
        "personnel_count": 0,
        "personnel_notes": "",
        "challenges_encountered": "",
        "recommendations": "",
    }


def structure_incident_report_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Map one loose tabular row onto the CFAU operational fields."""
    out = empty_incident_report_fields()
    for key, aliases in _INCIDENT_REPORT_ALIASES.items():
        v = _pick_aliased(row, aliases)
        if v is None:
            continue
        if key == "personnel_count":
            out[key] = _to_int(v)
        else:
            out[key] = str(v).strip()
    return out


def structure_incident_report_rows(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Return operational fields from the first row that yields any value,
    else a blank set. Post-incident uploads describe a single response, so
    one row is the expected shape; extra rows are ignored."""
    blank = empty_incident_report_fields()
    for r in (rows or []):
        mapped = structure_incident_report_row(r)
        if mapped != blank:
            return mapped
    return blank


# ─────────────────────────────────────────────────────────────────────
# Narrative PDF → CFAU operational fields (Week 8.1)
#
# Free-text post-incident PDFs aren't tabular, so structure_incident_report_
# rows() can't help. This label-anchored pass mirrors structure_text(): it
# only treats a known label as a section header when it is immediately
# followed by a colon, and captures each section's value up to the NEXT
# recognized label (or end of document). Conservative by design — it assists
# the review screen, it does not replace it.
# ─────────────────────────────────────────────────────────────────────

# Human-readable label variants (display style, not column keys). Longest
# variants are matched first so compound labels win over their substrings
# (e.g. "Operations Summary" over "Summary", "Personnel Deployed"/"Personnel
# Notes" over "Personnel").
_REPORT_TEXT_LABELS = {
    "operations_summary":     ["operations summary", "operation summary",
                               "summary of operations", "summary"],
    "actions_taken":          ["actions taken", "action taken",
                               "response actions", "actions"],
    "equipment_used":         ["equipment used", "assets used", "equipment"],
    "personnel_count":        ["personnel deployed", "number of personnel",
                               "no. of personnel", "no of personnel",
                               "personnel count", "personnel", "responders"],
    "personnel_notes":        ["personnel notes", "personnel remarks",
                               "team notes"],
    "challenges_encountered": ["challenges encountered", "challenges",
                               "difficulties", "issues encountered", "issues"],
    "recommendations":        ["recommendations", "recommendation",
                               "suggestions"],
}


def structure_incident_report_text(text: str) -> Dict[str, Any]:
    """Best-effort operational fields from free PDF text, anchored on
    'Label:' headers. Starts from the blank set and fills only sections that
    are present; the first occurrence of each field wins."""
    out = empty_incident_report_fields()
    if not text or not text.strip():
        return out

    # Flatten (alias, field) and sort longest-first so compound labels are
    # preferred by the alternation and never partially matched.
    pairs = [(a, f) for f, aliases in _REPORT_TEXT_LABELS.items() for a in aliases]
    pairs.sort(key=lambda p: len(p[0]), reverse=True)
    alias_to_field = {_norm_key(a): f for a, f in pairs}

    alias_pattern = "|".join(re.escape(a) for a, _ in pairs)
    # A label only counts as a section header when followed by a colon.
    label_re = re.compile(rf"(?i)\b({alias_pattern})\s*:")

    matches = list(label_re.finditer(text))
    if not matches:
        return out

    for i, m in enumerate(matches):
        field = alias_to_field.get(_norm_key(m.group(1)))
        if not field:
            continue
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        value = text[start:end].strip(" \t\r\n:-")
        if not value:
            continue
        if field == "personnel_count":
            if out[field] == 0:        # first occurrence wins
                out[field] = _to_int(value)
        elif not out[field]:           # first occurrence wins
            out[field] = value
    return out


def empty_field_row() -> Dict[str, Any]:
    """Blank row for manual-entry fallback (used when extraction failed
    or PDF text has no recognizable tabular data)."""
    return {
        "barangay": "",
        "disaster_type": DisasterType.other.value,
        "date_occurred": "",
        "date_raw": "",
        "date_ambiguous": False,
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
