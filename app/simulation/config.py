"""
Disaster Simulator — planning configuration.

Central home for every planning assumption the simulator engine uses, so that
no magic numbers live in the engine itself. Everything here is a plain constant
or dict and is meant to be read and edited by hand.

The config is deliberately split into TWO tiers that must NOT be blurred:

  * TIER A — CITED STANDARDS: values traceable to a published standard
    (Sphere Handbook, DSWD). Defensible in the capstone defense with a footnote.

  * TIER B — LOCAL PLANNING ASSUMPTIONS: CDRRMO planning figures with no
    international per-capita standard behind them. These are adjustable
    estimates and must NEVER be presented as Sphere or DSWD figures.

No functions and no database access live in this module.
"""

# ══════════════════════════════════════════════════════════════════════════
# TIER A — CITED STANDARDS  (defensible with a footnote)
# ══════════════════════════════════════════════════════════════════════════

# Persons per family/household unit.
# Basis: DSWD Family Food Pack is designed for a family of five members.
PERSONS_PER_FAMILY = 5

# Minimum drinking/domestic water per person per day, in litres.
# Basis: Sphere Handbook, Water Supply Standard 2.1 — a minimum of
# 15 L/person/day is established practice.
# NOTE: This is a MINIMUM, never a maximum. Acute-phase (immediate onset)
# responses may fall back to ~7.5 L/person/day for a short term only; the
# engine should treat 15 as the planning target, not a ceiling.
WATER_LITERS_PER_PERSON_PER_DAY = 15

# Acute-phase short-term floor, litres/person/day (context only — do not use
# as the default planning figure). Basis: Sphere acute-response guidance.
WATER_LITERS_PER_PERSON_PER_DAY_ACUTE_MIN = 7.5

# Number of days one DSWD Family Food Pack sustains one family.
# Basis: one DSWD Family Food Pack feeds a family of five for 2 days.
FFP_DAYS_PER_PACK = 2


# ══════════════════════════════════════════════════════════════════════════
# TIER B — LOCAL PLANNING ASSUMPTIONS  (NOT international standards)
# ══════════════════════════════════════════════════════════════════════════
# Every value below is a "CDRRMO planning assumption, adjustable". The client
# has no formal risk formula, and simulator outputs are estimates. Do NOT
# present any of these as Sphere or DSWD figures.

# Fraction of a barangay's population assumed to be affected, keyed to
# Barangay.risk_level. Keys MUST match the RiskLevel enum values
# (low / moderate / high / critical).
# CDRRMO planning assumption, adjustable — this is a planning input, not a
# prediction. The engine may adjust this upward based on incident history.
EXPOSURE_FRACTION = {
    "low": 0.10,       # CDRRMO planning assumption, adjustable
    "moderate": 0.25,  # CDRRMO planning assumption, adjustable
    "high": 0.40,      # CDRRMO planning assumption, adjustable
    "critical": 0.55,  # CDRRMO planning assumption, adjustable
}

# Target-only ratios for resource classes that have NO citable per-capita
# standard. CDRRMO planning assumptions, adjustable. These are NOT
# requirements — the UI should present these classes as
# "Available vs. manual target", never as a standard-based requirement.
# (See the engine note in Stage 2.)

# Responders per affected person — REMOVED. There is no personnel roster in the
# schema, so a personnel class would sit permanently at zero availability and
# distort overall_readiness with a metric we cannot populate. Kept here (commented)
# only to document the decision; re-enable if a roster data source is ever added.
# PERSONNEL_PER_AFFECTED = 0.01   # ~1 responder per 100 affected persons

# Response/transport vehicles per affected person. CDRRMO planning assumption, adjustable.
VEHICLES_PER_AFFECTED = 0.002   # ~1 vehicle per 500 affected persons

# Medicine/first-aid kits per affected person. CDRRMO planning assumption, adjustable.
MEDICINE_KITS_PER_AFFECTED = 0.02  # ~1 kit per 50 affected persons


# ══════════════════════════════════════════════════════════════════════════
# READINESS THRESHOLDS  (coverage ratio = available / required)
# ══════════════════════════════════════════════════════════════════════════
# CDRRMO planning assumption, adjustable. Classifies a coverage ratio into a
# readiness band for display.
#   Adequate : ratio >= 1.0
#   Partial  : 0.5 <= ratio < 1.0
#   Critical : ratio < 0.5
READINESS_THRESHOLDS = {
    "adequate": 1.0,   # ratio >= 1.0  -> Adequate
    "partial": 0.5,    # 0.5 <= ratio < 1.0 -> Partial; below 0.5 -> Critical
}
