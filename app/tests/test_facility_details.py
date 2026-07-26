"""Unit tests for app/services/facility_details.py — the shared Add/Edit
Critical Facility validation/normalization helpers used by both
app/routes/bdrrmo.py and app/routes/admin.py.

Most of these are pure functions, no DB/app needed (mirrors the style of
test_engine.py). find_duplicate_facility does touch the DB, so those tests
use a real (temporary, in-memory) SQLite session rather than a mock — a
fake query object can't faithfully reproduce SQLAlchemy's .filter() chain.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Barangay, Facility, FacilityType
from app.services.facility_details import (
    validate_name,
    validate_address,
    validate_classification,
    validate_floor_area,
    validate_capacity_field,
    validate_eo_moa_mou_status,
    validate_eo_moa_mou_reference,
    validate_hazard_reference,
    validate_notes,
    validate_facility_details,
    find_duplicate_facility,
    parse_coord,
    FACILITY_CLASSIFICATIONS,
    EO_MOA_MOU_STATUSES,
)


# ─────────────────────────────────────────────────────────────────────────
# Name / address
# ─────────────────────────────────────────────────────────────────────────
def test_name_too_short_rejected():
    value, err = validate_name("ab")
    assert value is None
    assert "at least 3" in err


def test_name_missing_rejected():
    value, err = validate_name("")
    assert value is None and err is not None


def test_name_too_long_rejected():
    value, err = validate_name("x" * 101)
    assert value is None
    assert "100 characters" in err


def test_name_trims_whitespace():
    value, err = validate_name("  Barangay Hall  ")
    assert err is None
    assert value == "Barangay Hall"


def test_address_optional():
    value, err = validate_address("")
    assert value is None and err is None


def test_address_oversized_rejected():
    value, err = validate_address("x" * 256)
    assert value is None and err is not None


# ─────────────────────────────────────────────────────────────────────────
# Classification (Facility.status — Permanent/Temporary/Under Construction)
# ─────────────────────────────────────────────────────────────────────────
def test_classification_blank_is_unspecified():
    value, err = validate_classification("")
    assert value is None and err is None


def test_classification_unspecified_literal_is_none():
    value, err = validate_classification("Unspecified")
    assert value is None and err is None


@pytest.mark.parametrize("c", FACILITY_CLASSIFICATIONS)
def test_classification_valid_values(c):
    value, err = validate_classification(c)
    assert value == c and err is None


def test_classification_invalid_rejected():
    value, err = validate_classification("Ruined")
    assert value is None and err is not None


# ─────────────────────────────────────────────────────────────────────────
# Floor area
# ─────────────────────────────────────────────────────────────────────────
def test_floor_area_optional_blank():
    value, err = validate_floor_area("")
    assert value is None and err is None


def test_floor_area_negative_rejected():
    value, err = validate_floor_area("-5")
    assert value is None
    assert "negative" in err


def test_floor_area_too_many_decimals_rejected():
    value, err = validate_floor_area("120.5001")
    assert value is None
    assert "decimal" in err


def test_floor_area_non_numeric_rejected():
    value, err = validate_floor_area("abc")
    assert value is None and err is not None


def test_floor_area_valid_two_decimals():
    value, err = validate_floor_area("120.50")
    assert err is None
    assert value == 120.5


def test_floor_area_zero_is_valid():
    value, err = validate_floor_area("0")
    assert err is None and value == 0


# ─────────────────────────────────────────────────────────────────────────
# Capacity fields — String columns; whole numbers OR legacy imported ranges
# ─────────────────────────────────────────────────────────────────────────
def test_capacity_blank_optional():
    value, err = validate_capacity_field("", "Capacity")
    assert value is None and err is None


def test_capacity_whole_number_valid():
    value, err = validate_capacity_field("50", "Capacity")
    assert value == "50" and err is None


def test_capacity_legacy_range_grandfathered():
    """A pre-existing imported range like "40-80" must still save
    unchanged when the user doesn't touch the field."""
    value, err = validate_capacity_field("40-80", "Capacity")
    assert value == "40-80" and err is None


def test_capacity_range_with_spaces_grandfathered():
    value, err = validate_capacity_field("40 - 80", "Capacity")
    assert value == "40 - 80" and err is None


def test_capacity_decimal_rejected():
    value, err = validate_capacity_field("50.5", "Capacity (families)")
    assert value is None
    assert "Capacity (families)" in err


def test_capacity_negative_rejected():
    value, err = validate_capacity_field("-10", "Capacity")
    assert value is None and err is not None


def test_capacity_letters_rejected():
    value, err = validate_capacity_field("fifty", "Capacity")
    assert value is None and err is not None


# ─────────────────────────────────────────────────────────────────────────
# EO / MOA / MOU
# ─────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("s", EO_MOA_MOU_STATUSES)
def test_eo_moa_mou_status_valid_values(s):
    value, err = validate_eo_moa_mou_status(s)
    assert value == s and err is None


def test_eo_moa_mou_status_invalid_rejected():
    value, err = validate_eo_moa_mou_status("Maybe")
    assert value is None and err is not None


def test_eo_moa_mou_reference_oversized_rejected():
    value, err = validate_eo_moa_mou_reference("x" * 151)
    assert value is None and err is not None


def test_eo_moa_mou_reference_valid():
    value, err = validate_eo_moa_mou_reference("MOA No. 2026-014")
    assert value == "MOA No. 2026-014" and err is None


# ─────────────────────────────────────────────────────────────────────────
# Hazard reference / notes
# ─────────────────────────────────────────────────────────────────────────
def test_hazard_reference_oversized_rejected():
    value, err = validate_hazard_reference("x" * 256)
    assert value is None and err is not None


def test_notes_oversized_rejected():
    value, err = validate_notes("x" * 1001)
    assert value is None and err is not None


def test_notes_valid():
    value, err = validate_notes("Generator on standby.")
    assert value == "Generator on standby." and err is None


# ─────────────────────────────────────────────────────────────────────────
# Full-form validation
# ─────────────────────────────────────────────────────────────────────────
def _base_form(**overrides):
    form = {
        "classification": "Permanent",
        "floor_area_sqm": "120.5",
        "capacity_families": "50",
        "capacity_individuals": "250",
        "ereid_capacity_families": "30",
        "ereid_capacity_individuals": "150",
        "supports_tropical_cyclone": "on",
        "supports_flooding": None,
        "supports_landslide": None,
        "supports_fire": None,
        "hazard_reference_master_list": "Ground Shaking — Moderate Risk",
        "eo_moa_mou_status": "Available",
        "eo_moa_mou_reference": "MOA No. 2026-014",
        "notes": "Backup generator on site.",
    }
    form.update(overrides)
    return form


def test_validate_facility_details_all_fields_valid():
    details, err = validate_facility_details(_base_form())
    assert err is None
    assert details["status"] == "Permanent"
    assert details["floor_area_sqm"] == 120.5
    assert details["supports_tropical_cyclone"] is True
    assert details["supports_flooding"] is False
    assert details["eo_moa_mou_status"] == "Available"


def test_validate_facility_details_only_required_equivalent_to_all_blank():
    """Everything in this form is optional — an all-blank submission (only
    name/type/status/coordinates, validated separately by each router) must
    still validate cleanly."""
    blank = {k: "" for k in _base_form()}
    for k in ("supports_tropical_cyclone", "supports_flooding", "supports_landslide", "supports_fire"):
        blank[k] = None
    details, err = validate_facility_details(blank)
    assert err is None
    assert all(v is None for k, v in details.items() if not k.startswith("supports_"))
    assert all(details[k] is False for k in details if k.startswith("supports_"))


def test_validate_facility_details_zero_capacity_preserved():
    details, err = validate_facility_details(_base_form(capacity_families="0", capacity_individuals="0"))
    assert err is None
    assert details["capacity_families"] == "0"
    assert details["capacity_individuals"] == "0"


def test_validate_facility_details_rejects_negative_floor_area():
    details, err = validate_facility_details(_base_form(floor_area_sqm="-1"))
    assert details == {}
    assert err is not None


def test_validate_facility_details_rejects_decimal_capacity():
    details, err = validate_facility_details(_base_form(capacity_families="12.5"))
    assert details == {}
    assert err is not None


def test_validate_facility_details_rejects_invalid_eo_moa_mou_status():
    details, err = validate_facility_details(_base_form(eo_moa_mou_status="Kind Of"))
    assert details == {}
    assert err is not None


def test_validate_facility_details_rejects_oversized_hazard_reference():
    details, err = validate_facility_details(_base_form(hazard_reference_master_list="x" * 256))
    assert details == {}
    assert err is not None


def test_validate_facility_details_rejects_oversized_notes():
    details, err = validate_facility_details(_base_form(notes="x" * 1001))
    assert details == {}
    assert err is not None


def test_validate_facility_details_all_hazards_selected():
    details, err = validate_facility_details(_base_form(
        supports_tropical_cyclone="on", supports_flooding="on",
        supports_landslide="on", supports_fire="on",
    ))
    assert err is None
    assert all(details[k] for k in (
        "supports_tropical_cyclone", "supports_flooding",
        "supports_landslide", "supports_fire",
    ))


# ─────────────────────────────────────────────────────────────────────────
# Coordinate parsing / duplicate detection
# ─────────────────────────────────────────────────────────────────────────
def test_parse_coord_valid():
    assert parse_coord("14.35", -90, 90) == 14.35


def test_parse_coord_out_of_range():
    assert parse_coord("200", -90, 90) is None


def test_parse_coord_non_numeric():
    assert parse_coord("abc", -90, 90) is None


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    barangay = Barangay(name="Test Barangay")
    session.add(barangay)
    session.commit()
    yield session, barangay.id
    session.close()


def _add_facility(session, barangay_id, name, lat, lng, is_archived=False):
    f = Facility(
        barangay_id=barangay_id, name=name, facility_type=FacilityType.evacuation_center,
        latitude=lat, longitude=lng, is_archived=is_archived,
    )
    session.add(f)
    session.commit()
    return f


def test_find_duplicate_by_name(db_session):
    session, barangay_id = db_session
    existing = _add_facility(session, barangay_id, "Barangay Hall", 14.35, 121.05)
    dup = find_duplicate_facility(session, barangay_id, "barangay hall", 14.40, 121.10)
    assert dup is not None and dup.id == existing.id


def test_find_duplicate_by_coordinates(db_session):
    session, barangay_id = db_session
    existing = _add_facility(session, barangay_id, "Evac Center A", 14.350000, 121.050000)
    dup = find_duplicate_facility(session, barangay_id, "Different Name", 14.350010, 121.050010)
    assert dup is not None and dup.id == existing.id


def test_find_duplicate_none_when_far_apart(db_session):
    session, barangay_id = db_session
    _add_facility(session, barangay_id, "Evac Center A", 14.350000, 121.050000)
    dup = find_duplicate_facility(session, barangay_id, "Different Name", 14.400000, 121.100000)
    assert dup is None


def test_find_duplicate_excludes_self_on_edit(db_session):
    session, barangay_id = db_session
    existing = _add_facility(session, barangay_id, "Barangay Hall", 14.35, 121.05)
    dup = find_duplicate_facility(session, barangay_id, "Barangay Hall", 14.35, 121.05, exclude_id=existing.id)
    assert dup is None


def test_find_duplicate_ignores_archived(db_session):
    session, barangay_id = db_session
    _add_facility(session, barangay_id, "Old Hall", 14.35, 121.05, is_archived=True)
    dup = find_duplicate_facility(session, barangay_id, "Old Hall", 14.35, 121.05)
    assert dup is None
