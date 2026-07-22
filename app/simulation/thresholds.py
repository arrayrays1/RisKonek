"""Simulator planning-threshold resolver + override layer.

config.py stays the source of DEFAULTS and provenance comments. This module is
the thin layer that (a) reads an admin override from the planning_thresholds
table if one exists, else falls back to the config default, and (b) validates
edits according to each value's tier. A fresh DB (no rows) behaves identically
to the config defaults.

Tiers (do not collapse):
  * locked  — describes DSWD pack composition; NOT editable.
  * floored — a published-standard minimum; editable upward, never below floor.
  * local   — no per-capita standard; CDRRMO owns it; no floor.
"""

from app.models import PlanningThreshold
from app.simulation import config as C
from app.simulation.engine import EXPOSURE_FRACTION_CAP

# Absurd-magnitude guard shared by all tiers (a positive value this large is a
# data-entry error, not a plan).
_ABSURD_MAX = 1_000_000

# Catalog: key -> metadata. `value` is the config default (config remains the
# single source of these numbers and their provenance). Order here drives the
# order fields appear on the settings page. source_label is None where no
# standard exists — those must never be presented as Sphere/DSWD figures.
DEFAULTS = {
    # ── locked (DSWD pack composition) ──
    "PERSONS_PER_FAMILY": {
        "value": C.PERSONS_PER_FAMILY, "tier": "locked", "floor_value": None,
        "unit": "persons/family", "source_label": "DSWD Family Food Pack",
        "label": "Persons per family",
    },
    "FFP_DAYS_PER_PACK": {
        "value": C.FFP_DAYS_PER_PACK, "tier": "locked", "floor_value": None,
        "unit": "days/pack", "source_label": "DSWD Family Food Pack",
        "label": "Days per DSWD Family Food Pack",
    },
    # ── floored (published-standard minimum) ──
    "WATER_LITERS_PER_PERSON_PER_DAY": {
        "value": C.WATER_LITERS_PER_PERSON_PER_DAY, "tier": "floored", "floor_value": 15.0,
        "unit": "L/person/day", "source_label": "Sphere WS 2.1",
        "label": "Water per person per day",
    },
    # ── local (CDRRMO-owned, no standard) ──
    "EXPOSURE_FRACTION_LOW": {
        "value": C.EXPOSURE_FRACTION["low"], "tier": "local", "floor_value": None,
        "unit": "fraction", "source_label": None,
        "label": "Low-risk barangay: share of population affected",
    },
    "EXPOSURE_FRACTION_MODERATE": {
        "value": C.EXPOSURE_FRACTION["moderate"], "tier": "local", "floor_value": None,
        "unit": "fraction", "source_label": None,
        "label": "Moderate-risk barangay: share of population affected",
    },
    "EXPOSURE_FRACTION_HIGH": {
        "value": C.EXPOSURE_FRACTION["high"], "tier": "local", "floor_value": None,
        "unit": "fraction", "source_label": None,
        "label": "High-risk barangay: share of population affected",
    },
    "EXPOSURE_FRACTION_CRITICAL": {
        "value": C.EXPOSURE_FRACTION["critical"], "tier": "local", "floor_value": None,
        "unit": "fraction", "source_label": None,
        "label": "Critical-risk barangay: share of population affected",
    },
    "MEDICINE_KITS_PER_AFFECTED": {
        "value": C.MEDICINE_KITS_PER_AFFECTED, "tier": "local", "floor_value": None,
        "unit": "kits/person", "source_label": None, "label": "Medicine kit coverage",
    },
    "VEHICLES_PER_AFFECTED": {
        "value": C.VEHICLES_PER_AFFECTED, "tier": "local", "floor_value": None,
        "unit": "vehicles/person", "source_label": None, "label": "Response vehicle coverage",
    },
}


def get_threshold(db, key: str) -> float:
    """Return the admin override for `key` if a row exists, else the config
    default. Fresh DB (no rows) => identical to today."""
    row = db.query(PlanningThreshold).filter(PlanningThreshold.key == key).first()
    if row is not None:
        return float(row.value)
    return float(DEFAULTS[key]["value"])


def ensure_seeded(db) -> None:
    """Insert a row per catalog key on first use (idempotent). Values seed to the
    config defaults, so seeding changes no behaviour."""
    existing = {k for (k,) in db.query(PlanningThreshold.key).all()}
    added = False
    for key, meta in DEFAULTS.items():
        if key not in existing:
            db.add(PlanningThreshold(
                key=key, value=float(meta["value"]), tier=meta["tier"],
                floor_value=meta["floor_value"], unit=meta["unit"],
                source_label=meta["source_label"],
            ))
            added = True
    if added:
        db.commit()


def _assemble(getter):
    """Build the plain-number dict the engine consumes, using `getter(key)`."""
    return {
        "persons_per_family": getter("PERSONS_PER_FAMILY"),
        "ffp_days_per_pack": getter("FFP_DAYS_PER_PACK"),
        "water_liters_per_person_per_day": getter("WATER_LITERS_PER_PERSON_PER_DAY"),
        "medicine_kits_per_affected": getter("MEDICINE_KITS_PER_AFFECTED"),
        "vehicles_per_affected": getter("VEHICLES_PER_AFFECTED"),
        "exposure_fraction": {
            "low": getter("EXPOSURE_FRACTION_LOW"),
            "moderate": getter("EXPOSURE_FRACTION_MODERATE"),
            "high": getter("EXPOSURE_FRACTION_HIGH"),
            "critical": getter("EXPOSURE_FRACTION_CRITICAL"),
        },
    }


def build_engine_thresholds(db) -> dict:
    """DB-aware: resolve every threshold (override-or-default) into plain floats
    for the engine. No ORM objects leave this function."""
    return _assemble(lambda k: get_threshold(db, k))


def default_engine_thresholds() -> dict:
    """DB-free: the config defaults as the engine dict (used by tests and as a
    reference)."""
    return _assemble(lambda k: float(DEFAULTS[k]["value"]))


def validate_threshold(key: str, new_value) -> str | None:
    """Return None if the edit is allowed, else a human-readable error string.

    Enforced server-side (an HTML min= is only a convenience). Rules:
      locked  -> reject any change (describes pack composition).
      floored -> reject below floor_value, naming the standard.
      local   -> any positive value.
      all     -> reject non-positive and absurd magnitudes.
      exposure fractions -> within (0, 1] and <= EXPOSURE_FRACTION_CAP (0.60).
    """
    meta = DEFAULTS.get(key)
    if meta is None:
        return "Unknown threshold."

    try:
        v = float(new_value)
    except (TypeError, ValueError):
        return "Value must be a number."

    if v <= 0:
        return "Value must be greater than zero."
    if v > _ABSURD_MAX:
        return "Value is implausibly large."

    if meta["tier"] == "locked":
        return (
            f"'{meta['label']}' describes DSWD Family Food Pack composition, not a "
            "planning minimum — it cannot be changed here."
        )

    if meta["tier"] == "floored":
        floor = meta["floor_value"]
        if v < floor:
            std = meta["source_label"] or "the standard"
            return (
                f"{meta['label']} cannot be set below {floor:g} {meta['unit']} ({std})."
            )

    if key.startswith("EXPOSURE_FRACTION_"):
        if v > 1.0:
            return "Exposure fraction must be between 0 and 1."
        if v > EXPOSURE_FRACTION_CAP:
            return f"Exposure fraction cannot exceed the engine cap of {EXPOSURE_FRACTION_CAP:g}."

    return None
