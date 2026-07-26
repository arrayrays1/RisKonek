"""Shared validation/normalization for the Add/Edit Critical Facility form.

Used by both app/routes/bdrrmo.py (barangay-scoped) and app/routes/admin.py
(any barangay, via a barangay picker) so the two CRUD paths stay in lock
step instead of drifting. Coordinate parsing and duplicate detection are
also shared here since both routers need identical semantics; barangay
scoping itself (which barangay a submission is allowed to target) stays in
each router, since that's the one thing that legitimately differs.
"""
import re
from typing import Optional

from app.models import Facility

# Facility.status — the structural classification imported from the
# CDRRMO master list (distinct from Facility.operational_status).
FACILITY_CLASSIFICATIONS = ["Permanent", "Temporary", "Under Construction"]

EO_MOA_MOU_STATUSES = ["Available", "Pending", "Not Available", "Not Applicable"]

MAX_NAME_LEN = 100
MAX_ADDRESS_LEN = 255
MAX_HAZARD_REF_LEN = 255
MAX_EO_MOA_MOU_REF_LEN = 150
MAX_NOTES_LEN = 1000

_LEGACY_RANGE_RE = re.compile(r"^\d+\s*-\s*\d+$")
_WHOLE_NUMBER_RE = re.compile(r"^\d+$")

# Coordinate proximity (in degrees) under which two facilities are treated
# as the same point. ~0.0002° ≈ 22 m — tight enough to catch a re-pin of
# the same building, loose enough not to flag genuinely separate ones.
DUP_COORD_TOLERANCE = 0.0002


def parse_coord(value, lo: float, hi: float) -> Optional[float]:
    """Coerce a coordinate string to float within [lo, hi]; None if invalid."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if lo <= f <= hi else None


def find_duplicate_facility(db, barangay_id, name, lat, lon, exclude_id=None):
    """Return an existing facility in the same barangay that looks like a
    duplicate of the given one, or None.

    A duplicate is either the same (case-insensitive, trimmed) name, or a
    point within DUP_COORD_TOLERANCE degrees of the same coordinates.
    Archived facilities are ignored so a restore isn't blocked. The
    facility being edited (exclude_id) is excluded from the comparison.
    """
    q = db.query(Facility).filter(
        Facility.barangay_id == barangay_id,
        Facility.is_archived == False,
    )
    if exclude_id is not None:
        q = q.filter(Facility.id != exclude_id)

    norm = (name or "").strip().lower()
    for f in q.all():
        if norm and (f.name or "").strip().lower() == norm:
            return f
        if (abs((f.latitude or 0) - lat) <= DUP_COORD_TOLERANCE
                and abs((f.longitude or 0) - lon) <= DUP_COORD_TOLERANCE):
            return f
    return None


def _clean_optional_text(value, max_len, label):
    """Trim; None if empty. Error if it exceeds max_len (reject, don't
    silently truncate, so nothing entered is ever quietly lost)."""
    v = (value or "").strip()
    if not v:
        return None, None
    if len(v) > max_len:
        return None, f"{label} must be {max_len} characters or fewer"
    return v, None


def validate_name(value) -> (Optional[str], Optional[str]):
    v = (value or "").strip()
    if len(v) < 3:
        return None, "Facility name must be at least 3 characters"
    if len(v) > MAX_NAME_LEN:
        return None, f"Facility name must be {MAX_NAME_LEN} characters or fewer"
    return v, None


def validate_address(value) -> (Optional[str], Optional[str]):
    return _clean_optional_text(value, MAX_ADDRESS_LEN, "Address")


def validate_classification(value) -> (Optional[str], Optional[str]):
    v = (value or "").strip()
    if not v or v == "Unspecified":
        return None, None
    if v not in FACILITY_CLASSIFICATIONS:
        return None, "Invalid facility classification"
    return v, None


def validate_floor_area(value) -> (Optional[float], Optional[str]):
    v = (value or "").strip()
    if not v:
        return None, None
    try:
        f = float(v)
    except ValueError:
        return None, "Floor area must be a number"
    if f < 0:
        return None, "Floor area must not be negative"
    if round(f, 2) != round(f, 6):
        return None, "Floor area may have at most 2 decimal places"
    return round(f, 2), None


def validate_capacity_field(value, label) -> (Optional[str], Optional[str]):
    """Capacity fields are String columns (imported ranges like "40-80"
    can't be coerced to Integer). New/edited entries must be a clean
    non-negative whole number; an already-imported range value passed
    through unchanged is grandfathered in rather than rejected."""
    v = (value or "").strip()
    if not v:
        return None, None
    if _LEGACY_RANGE_RE.match(v):
        return v, None
    if _WHOLE_NUMBER_RE.match(v):
        return v, None
    return None, f"{label} must be a whole number (0 or greater)"


def validate_eo_moa_mou_status(value) -> (Optional[str], Optional[str]):
    v = (value or "").strip()
    if not v:
        return None, None
    if v not in EO_MOA_MOU_STATUSES:
        return None, "Invalid EO/MOA/MOU status"
    return v, None


def validate_eo_moa_mou_reference(value) -> (Optional[str], Optional[str]):
    return _clean_optional_text(value, MAX_EO_MOA_MOU_REF_LEN, "EO/MOA/MOU reference")


def validate_hazard_reference(value) -> (Optional[str], Optional[str]):
    return _clean_optional_text(value, MAX_HAZARD_REF_LEN, "Hazard reference")


def validate_notes(value) -> (Optional[str], Optional[str]):
    return _clean_optional_text(value, MAX_NOTES_LEN, "Notes")


def validate_facility_details(form: dict) -> (dict, Optional[str]):
    """Validate the "richer" facility fields (everything beyond name/type/
    status/address/coordinates, which each router already validates on its
    own). Returns (normalized_values, error_message). On error, the dict
    is empty and error_message is set.

    Expected keys in `form`: classification, floor_area_sqm,
    capacity_families, capacity_individuals, ereid_capacity_families,
    ereid_capacity_individuals, supports_tropical_cyclone, supports_flooding,
    supports_landslide, supports_fire, hazard_reference_master_list,
    eo_moa_mou_status, eo_moa_mou_reference, notes.
    """
    out = {}

    out["status"], err = validate_classification(form.get("classification"))
    if err:
        return {}, err

    out["floor_area_sqm"], err = validate_floor_area(form.get("floor_area_sqm"))
    if err:
        return {}, err

    for key, label in [
        ("capacity_families", "Capacity (families)"),
        ("capacity_individuals", "Capacity (individuals)"),
        ("ereid_capacity_families", "EREID capacity (families)"),
        ("ereid_capacity_individuals", "EREID capacity (individuals)"),
    ]:
        out[key], err = validate_capacity_field(form.get(key), label)
        if err:
            return {}, err

    out["supports_tropical_cyclone"] = bool(form.get("supports_tropical_cyclone"))
    out["supports_flooding"] = bool(form.get("supports_flooding"))
    out["supports_landslide"] = bool(form.get("supports_landslide"))
    out["supports_fire"] = bool(form.get("supports_fire"))

    out["vulnerability_risk"], err = validate_hazard_reference(
        form.get("hazard_reference_master_list")
    )
    if err:
        return {}, err

    out["eo_moa_mou_status"], err = validate_eo_moa_mou_status(form.get("eo_moa_mou_status"))
    if err:
        return {}, err

    out["eo_moa_mou_reference"], err = validate_eo_moa_mou_reference(form.get("eo_moa_mou_reference"))
    if err:
        return {}, err

    out["notes"], err = validate_notes(form.get("notes"))
    if err:
        return {}, err

    return out, None
