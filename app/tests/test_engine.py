"""
Unit tests for the Disaster Simulator engine (app/simulation/engine.py).

The engine is pure, so these tests need no DB, app, or fixtures — just made-up
numbers and hand-computed expectations. Run with:  pytest app/tests/test_engine.py
"""

from app.simulation.engine import (
    estimate_affected,
    estimate_vulnerable,
    compute_gaps,
    readiness_status,
    overall_readiness,
    run_simulation,
)
from app.simulation.thresholds import default_engine_thresholds, validate_threshold

# The engine no longer imports the editable planning ratios; callers pass them
# in. These tests use the config defaults so the expected numbers are unchanged.
THRESHOLDS = default_engine_thresholds()


# ─────────────────────────────────────────────────────────────────────────
# Scenario 1 — High-risk flood, recurrent hazard (exposure bump fires)
# ─────────────────────────────────────────────────────────────────────────
def test_high_risk_flood_scenario():
    # high risk = 0.40 exposure; 6 incidents (>=5) adds the +0.05 bump -> 0.45.
    # 10,000 people * 0.45 = 4,500 affected.
    facts = {
        "disaster_type": "flood",
        "barangay_name": "Barangay Test",
        "total_population": 10_000,
        "risk_level": "high",
        "hazard_history_count": 6,
        "vulnerable_pop": 2_000,   # 20% share -> 900 of the affected
        "horizon_days": 3,
        "thresholds": THRESHOLDS,
        "available_by_class": {
            "water": 202_500,      # exactly covers -> Adequate
            "food": 1_800,
            "blankets": 900,
            "evac_capacity": 4_500,
            "medicine": 45,        # half of 90 need -> Partial
            "vehicles": 9,
        },
    }
    result = run_simulation(facts)

    assert result["estimated_affected"] == 4_500
    assert result["estimated_vulnerable"] == 900

    needs = result["needs"]
    assert needs["water"]["quantity"] == 4_500 * 15 * 3        # 202,500
    assert needs["food"]["quantity"] == 900 * 2               # 1,800 (900 families, 2 packs)
    assert needs["blankets"]["quantity"] == 900               # per family, not per person
    assert needs["evac_capacity"]["quantity"] == 4_500

    # Basis tagging: standards vs adjustable assumptions.
    assert needs["water"]["basis"] == "standard"
    assert needs["food"]["basis"] == "standard"
    assert needs["medicine"]["basis"] == "assumption"
    assert needs["vehicles"]["basis"] == "assumption"
    # personnel was removed entirely (no roster data source).
    assert "personnel" not in needs

    # Medicine is half-covered -> Partial, which drags overall to Partial.
    assert result["status_by_class"]["medicine"] == "Partial"
    assert result["overall_readiness"] == "Partial"

    # Recurrent-hazard note is raised because history (6) >= threshold (5).
    assert any("Recurrent hazard" in r for r in result["operational_risks"])


# ─────────────────────────────────────────────────────────────────────────
# Scenario 2 — Zero-need edge case (empty barangay / no affected)
# ─────────────────────────────────────────────────────────────────────────
def test_zero_need_edge_case():
    facts = {
        "disaster_type": "fire",
        "total_population": 0,      # nobody -> nobody affected
        "risk_level": "low",
        "hazard_history_count": 0,
        "vulnerable_pop": 0,
        "horizon_days": 3,
        "thresholds": THRESHOLDS,
        "available_by_class": {},   # nothing on hand either
    }
    result = run_simulation(facts)

    assert result["estimated_affected"] == 0
    assert result["estimated_vulnerable"] == 0
    assert result["needs"]["water"]["quantity"] == 0
    assert result["needs"]["food"]["quantity"] == 0

    # Zero need is fully covered by definition -> every class Adequate.
    assert all(s == "Adequate" for s in result["status_by_class"].values())
    assert result["overall_readiness"] == "Adequate"

    # No shortfalls, no fleet, no history -> no operational risks.
    assert result["operational_risks"] == []


# ─────────────────────────────────────────────────────────────────────────
# Scenario 3 — Fleet-degraded (vehicles under repair >= serviceable)
# ─────────────────────────────────────────────────────────────────────────
def test_fleet_degraded_scenario():
    # moderate risk = 0.25; 4,000 * 0.25 = 1,000 affected.
    facts = {
        "disaster_type": "typhoon",
        "total_population": 4_000,
        "risk_level": "moderate",
        "hazard_history_count": 2,   # below threshold -> no bump, no recurrent note
        "vulnerable_pop": 400,
        "horizon_days": 7,
        "thresholds": THRESHOLDS,
        "available_by_class": {
            "water": 105_000,
            "food": 800,
            "blankets": 200,
            "evac_capacity": 1_000,
            "medicine": 20,
            "vehicles": 2,
        },
        "fleet": {"serviceable": 2, "under_repair": 3},  # 3 >= 2 -> degraded
    }
    result = run_simulation(facts)

    assert result["estimated_affected"] == 1_000
    # water = 1000 * 15 * 7 ; food = 200 families * ceil(7/2)=4 packs = 800
    assert result["needs"]["water"]["quantity"] == 105_000
    assert result["needs"]["food"]["quantity"] == 800

    # The fleet-degraded operational risk must be present.
    assert any("fleet degraded" in r.lower() for r in result["operational_risks"])

    # No recurrent-hazard note (history below threshold).
    assert not any("Recurrent hazard" in r for r in result["operational_risks"])


# ─────────────────────────────────────────────────────────────────────────
# A few direct unit checks on the smaller pure helpers
# ─────────────────────────────────────────────────────────────────────────
def test_exposure_bump_is_capped():
    # base 0.55 + bump (0.05) = 0.60 exactly, never above the cap.
    # signature: estimate_affected(total_population, hazard_history_count, base_fraction)
    assert estimate_affected(1_000, 5, 0.55) == 600


def test_estimate_vulnerable_guards_zero_population():
    assert estimate_vulnerable(100, 50, 0) == 0


def test_readiness_thresholds():
    assert readiness_status(1.0) == "Adequate"
    assert readiness_status(0.75) == "Partial"
    assert readiness_status(0.5) == "Partial"
    assert readiness_status(0.49) == "Critical"


def test_overall_readiness_is_conservative():
    assert overall_readiness(["Adequate", "Partial", "Critical"]) == "Critical"
    assert overall_readiness(["Adequate", "Adequate"]) == "Adequate"
    assert overall_readiness([]) == "Adequate"


def test_compute_gaps_math():
    needs = {"water": {"quantity": 100, "unit": "liters", "basis": "standard"}}
    gaps = compute_gaps(needs, {"water": 40})
    assert gaps["water"]["gap"] == 60
    assert gaps["water"]["coverage"] == 0.4


# ─────────────────────────────────────────────────────────────────────────
# Threshold validation (three-tier rules)
# ─────────────────────────────────────────────────────────────────────────
def test_floored_value_below_floor_is_rejected():
    # Water's floor is Sphere's 15 L/person/day — below it must be rejected,
    # and the error must name the standard.
    err = validate_threshold("WATER_LITERS_PER_PERSON_PER_DAY", 10)
    assert err is not None
    assert "15" in err and "Sphere" in err

    # At/above the floor is accepted.
    assert validate_threshold("WATER_LITERS_PER_PERSON_PER_DAY", 15) is None
    assert validate_threshold("WATER_LITERS_PER_PERSON_PER_DAY", 20) is None


def test_locked_value_cannot_be_changed():
    err = validate_threshold("PERSONS_PER_FAMILY", 6)
    assert err is not None and "cannot be changed" in err


def test_local_and_bounds_rules():
    # local accepts any positive value...
    assert validate_threshold("MEDICINE_KITS_PER_AFFECTED", 0.05) is None
    # ...but not non-positive.
    assert validate_threshold("MEDICINE_KITS_PER_AFFECTED", 0) is not None
    # exposure fractions are capped at the engine's 0.60.
    assert validate_threshold("EXPOSURE_FRACTION_HIGH", 0.60) is None
    assert validate_threshold("EXPOSURE_FRACTION_HIGH", 0.70) is not None
