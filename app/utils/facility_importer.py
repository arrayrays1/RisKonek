"""
Importer for data/san_pedro_critical_facilities.xlsx.

The source file is hand-maintained by CDRRMO and is NOT a clean
one-row-header CSV:
  rows 0-1   blank
  row   2-3  multi-row header (section + sub-column titles)
  rows 4-9   "City of San Pedro"-level facilities (no specific barangay)
  rows 10..  barangay facilities; the barangay column is merged so
             subsequent rows for the same barangay have a blank col 0
  row  ~107  "TOTAL" footer
  rows 108+  trailing blanks

This module:
  * reads the file with pandas/openpyxl,
  * forward-fills the barangay column,
  * normalizes "Brgy. GSIS" → "G.S.I.S." style variants,
  * parses coordinates (decimal / DMS / fallback),
  * routes "City of San Pedro" rows to the nearest known barangay,
  * upserts Facility rows keyed on (barangay_id, name).
"""
from __future__ import annotations

import math
import os
import re
from typing import Optional

import pandas as pd

from app.models import Barangay, Facility, FacilityType
from app.utils.coords import parse_coordinates
from app.utils.geo import BARANGAY_COORDS


DEFAULT_EXCEL_PATH = os.path.join("data", "san_pedro_critical_facilities.xlsx")

# Column positions in the source spreadsheet (0-indexed).
_COLS = {
    0:  "barangay_raw",
    1:  "name",
    2:  "coordinates",
    3:  "status_permanent",
    4:  "status_temporary",
    5:  "status_under_construction",
    6:  "floor_area_sqm",
    7:  "tropical_cyclone",
    8:  "flooding",
    9:  "landslide",
    10: "fire",
    11: "capacity_families",
    12: "capacity_individuals",
    13: "ereid_capacity_families",
    14: "ereid_capacity_individuals",
    15: "vulnerability_risk",
    16: "eo_moa_mou",
}

# First data row, last data row (exclusive) — verified by inspecting
# the workbook (114 × 26, "TOTAL" footer at row 107).
_DATA_START = 4
_DATA_END = 107

# Maps the spreadsheet's barangay column to the canonical Barangay.name
# already seeded in the database. Any "Barangay X" prefix is stripped
# generically; only the exceptions below need explicit mappings.
_BARANGAY_ALIASES = {
    "GSIS": "G.S.I.S.",
    "Sto Nino": "Santo Niño",
    "Sto. Nino": "Santo Niño",
    "Sto Niño": "Santo Niño",
    "Sampaguita": "Sampaguita Village",
    "United Bayanihan (UB)": "United Bayanihan",
    "United Better Living (UBL)": "United Better Living",
}

CITY_LEVEL_MARKER = "City of San Pedro"


def _normalize_barangay(raw: str) -> Optional[str]:
    """Return the canonical Barangay.name, or None for city-level rows."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s.lower() == "nan":
        return None
    if s.lower().startswith("city of san pedro"):
        return CITY_LEVEL_MARKER

    # Strip "Barangay " / "Brgy. " / "Brgy " prefixes uniformly.
    s = re.sub(r"^(barangay|brgy\.?)\s+", "", s, flags=re.IGNORECASE).strip()
    return _BARANGAY_ALIASES.get(s, s)


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _nearest_barangay_name(lat: float, lng: float) -> str:
    return min(
        BARANGAY_COORDS.items(),
        key=lambda kv: _haversine_km(lat, lng, kv[1]["lat"], kv[1]["lng"]),
    )[0]


def _infer_facility_type(name: str) -> FacilityType:
    """Map free-text facility names to the existing FacilityType enum.

    The Excel covers covered courts, multi-purpose halls, evacuation
    centers, schools, daycare, health centers, chapels, etc. We don't
    invent new enum values — anything that functions as a shelter maps
    to evacuation_center.
    """
    n = (name or "").lower()
    if "hospital" in n:
        return FacilityType.hospital
    if "health center" in n or "lying-in" in n or "clinic" in n:
        return FacilityType.health_clinic
    if "staging" in n:
        return FacilityType.staging_area
    if "school" in n or "daycare" in n or "day care" in n:
        return FacilityType.school
    return FacilityType.evacuation_center


def _pick_status(row) -> Optional[str]:
    """The three status columns are check-boxes. Pick the first one set."""
    if _truthy(row.get("status_permanent")):
        return "Permanent"
    if _truthy(row.get("status_temporary")):
        return "Temporary"
    if _truthy(row.get("status_under_construction")):
        return "Under Construction"
    return None


def _truthy(value) -> bool:
    """Treat True / 'True' / 'x' / non-empty as set; NaN/blank as unset."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, float) and math.isnan(value):
        return False
    s = str(value).strip().lower()
    if not s or s == "nan":
        return False
    if s in {"false", "0", "no", "unidentified"}:
        return False
    return True


def _clean_text(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    s = str(value).strip()
    if not s or s.lower() == "nan":
        return None
    return s


def _clean_number(value) -> Optional[float]:
    """Floor-area column may contain a single number or be blank."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and math.isnan(value):
            return None
        return float(value)
    s = str(value).strip()
    if not s or s.lower() == "nan":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def import_facilities(db, excel_path: str = DEFAULT_EXCEL_PATH) -> dict:
    """Read the Excel file and upsert Facility rows.

    Returns a summary dict the caller (seed.py) can print:
        {
            "inserted": int,
            "skipped_existing": int,
            "skipped_invalid": int,
            "problematic_rows": [ (excel_row, reason), ... ],
        }
    """
    summary = {
        "inserted": 0,
        "skipped_existing": 0,
        "skipped_invalid": 0,
        "problematic_rows": [],
    }

    if not os.path.exists(excel_path):
        print(f"⚠ Facility importer: {excel_path} not found.")
        return summary

    raw = pd.read_excel(excel_path, header=None, engine="openpyxl")
    df = raw.iloc[_DATA_START:_DATA_END, list(_COLS.keys())].copy()
    df.columns = list(_COLS.values())
    df["barangay_raw"] = df["barangay_raw"].ffill()

    # Cache barangays once — they were created earlier in seed.py.
    brgy_by_name = {b.name: b for b in db.query(Barangay).all()}

    for excel_row, row in df.iterrows():
        name = _clean_text(row.get("name"))
        if not name:
            continue
        # Footer row "TOTAL" has a digit in the name column.
        if name.upper() == "TOTAL":
            continue

        canonical = _normalize_barangay(row.get("barangay_raw"))
        if canonical is None:
            summary["problematic_rows"].append(
                (int(excel_row), f"no barangay column value for '{name}'")
            )
            summary["skipped_invalid"] += 1
            continue

        lat, lng, coord_reason = parse_coordinates(row.get("coordinates"))
        is_approx = False

        # City-level rows: prefer real coords, otherwise fall back to
        # the centroid of the nearest barangay. Either way we route them
        # to that nearest barangay so the FK stays satisfied.
        if canonical == CITY_LEVEL_MARKER:
            if lat is not None and lng is not None:
                target_barangay_name = _nearest_barangay_name(lat, lng)
            else:
                # No usable coordinate — fall back to Poblacion as a safe
                # city-hall anchor. Should not happen with current data.
                target_barangay_name = "Poblacion"
                centroid = BARANGAY_COORDS[target_barangay_name]
                lat, lng = centroid["lat"], centroid["lng"]
                is_approx = True
                summary["problematic_rows"].append(
                    (int(excel_row), f"city-level '{name}': {coord_reason}; "
                                     f"fell back to {target_barangay_name}")
                )
            is_city_level = True
        else:
            target_barangay_name = canonical
            is_city_level = False
            if lat is None or lng is None:
                centroid = BARANGAY_COORDS.get(target_barangay_name)
                if centroid is None:
                    summary["problematic_rows"].append(
                        (int(excel_row), f"'{name}': no centroid for "
                                         f"'{target_barangay_name}'")
                    )
                    summary["skipped_invalid"] += 1
                    continue
                lat, lng = centroid["lat"], centroid["lng"]
                is_approx = True
                summary["problematic_rows"].append(
                    (int(excel_row), f"'{name}' in {target_barangay_name}: "
                                     f"{coord_reason}; using centroid")
                )

        brgy = brgy_by_name.get(target_barangay_name)
        if brgy is None:
            summary["problematic_rows"].append(
                (int(excel_row), f"'{name}': barangay "
                                 f"'{target_barangay_name}' not in DB")
            )
            summary["skipped_invalid"] += 1
            continue

        existing = (
            db.query(Facility)
            .filter(Facility.barangay_id == brgy.id, Facility.name == name)
            .first()
        )
        if existing:
            summary["skipped_existing"] += 1
            continue

        facility = Facility(
            barangay_id=brgy.id,
            name=name,
            facility_type=_infer_facility_type(name),
            latitude=lat,
            longitude=lng,
            address=f"{brgy.name}, San Pedro, Laguna",
            is_active=True,
            status=_pick_status(row),
            floor_area_sqm=_clean_number(row.get("floor_area_sqm")),
            capacity_families=_clean_text(row.get("capacity_families")),
            capacity_individuals=_clean_text(row.get("capacity_individuals")),
            ereid_capacity_families=_clean_text(row.get("ereid_capacity_families")),
            ereid_capacity_individuals=_clean_text(row.get("ereid_capacity_individuals")),
            supports_tropical_cyclone=_truthy(row.get("tropical_cyclone")),
            supports_flooding=_truthy(row.get("flooding")),
            supports_landslide=_truthy(row.get("landslide")),
            supports_fire=_truthy(row.get("fire")),
            vulnerability_risk=_clean_text(row.get("vulnerability_risk")),
            eo_moa_mou=_clean_text(row.get("eo_moa_mou")),
            is_approximate_location=is_approx,
            is_city_level=is_city_level,
        )
        db.add(facility)
        summary["inserted"] += 1

    db.commit()
    return summary
