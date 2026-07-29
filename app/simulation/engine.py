"""
Resource Simulator — calculation engine.

PURE functions only. Every function here takes plain arguments (ints, strings,
small dicts) and returns plain dicts/lists. There is deliberately NO database
access, NO FastAPI, NO AI in this module so that:

  * every number can be explained line-by-line in the capstone defense, and
  * the whole engine is unit-testable without a running app or DB.

All planning numbers come from app/simulation/config.py. The Stage-3 route does
the DB queries and hands the already-counted availability figures to
run_simulation(); the engine never touches persistence itself.
"""

from math import ceil

# The engine no longer imports the admin-editable planning ratios directly:
# the ROUTE resolves them (override-or-default) via app/simulation/thresholds.py
# and passes plain numbers in, so this module stays pure and DB-free. Only the
# readiness classification bands (not part of the editable set) come from config.
from app.simulation.config import READINESS_THRESHOLDS

# The resource classes the engine reasons about, in display order. Availability
# passed in by the route must be keyed by these names.
NEED_CLASSES = [
    "water",
    "food",
    "blankets",
    "evac_capacity",
    "medicine",
    "vehicles",
]

# Tier-B planning threshold (CDRRMO assumption, adjustable): a barangay with
# this many or more recorded incidents of the selected disaster type is treated
# as a recurrent-hazard area — the exposure fraction is nudged up and an
# operational-risk note is raised.
HAZARD_HISTORY_THRESHOLD = 5

# Exposure bump applied once when the recurrent-hazard threshold is met, and the
# hard ceiling the resulting fraction is never allowed to exceed. Both are
# CDRRMO planning assumptions, adjustable — not predictions.
HAZARD_HISTORY_BUMP = 0.05
EXPOSURE_FRACTION_CAP = 0.60


# ─────────────────────────────────────────────────────────────────────────
# 1. Affected population
# ─────────────────────────────────────────────────────────────────────────
def estimate_affected(total_population, hazard_history_count, base_exposure_fraction):
    """Estimate how many people are affected.

    `base_exposure_fraction` is resolved by the CALLER from the (possibly
    admin-overridden) exposure fraction for the barangay's risk level, so the
    engine never looks it up itself. Bump rule: if the barangay has 5+ recorded
    incidents of this disaster type (HAZARD_HISTORY_THRESHOLD), add a modest
    +0.05 (HAZARD_HISTORY_BUMP) to reflect recurrent exposure. The result is
    capped at 0.60 (EXPOSURE_FRACTION_CAP) so it can never claim more than ~60%
    of the population. The bump and cap remain engine policy, not admin-editable.
    """
    fraction = base_exposure_fraction
    if hazard_history_count >= HAZARD_HISTORY_THRESHOLD:
        fraction += HAZARD_HISTORY_BUMP
    fraction = min(fraction, EXPOSURE_FRACTION_CAP)

    if total_population <= 0:
        return 0
    return int(round(total_population * fraction))


# ─────────────────────────────────────────────────────────────────────────
# 2. Vulnerable subset of the affected
# ─────────────────────────────────────────────────────────────────────────
def estimate_vulnerable(estimated_affected, vulnerable_pop, total_population):
    """Estimate how many of the affected are vulnerable.

    vulnerable_pop is supplied by the route as pwd_count + elderly_count +
    children_count (it reuses admin.py's _vulnerable_percent source data). We
    apply the barangay-wide vulnerable share to the affected count so the two
    estimates stay proportional. The share is clamped to [0, 1] to stay sane if
    the source counts are noisy.
    """
    if total_population <= 0 or estimated_affected <= 0:
        return 0
    share = vulnerable_pop / total_population
    share = max(0.0, min(share, 1.0))
    return int(round(estimated_affected * share))


# ─────────────────────────────────────────────────────────────────────────
# 3. Needs per resource class
# ─────────────────────────────────────────────────────────────────────────
def compute_needs(estimated_affected, horizon_days, thresholds):
    """Compute the required quantity for each resource class.

    `thresholds` is a plain dict of resolved (override-or-default) numbers passed
    in by the caller — the engine holds no config constants of its own for these.
    Required keys: persons_per_family, ffp_days_per_pack,
    water_liters_per_person_per_day, medicine_kits_per_affected,
    vehicles_per_affected.

    Each entry is tagged with a "basis":
      * "standard"   — traceable to Sphere / DSWD (defensible with a footnote)
      * "assumption" — Tier-B CDRRMO planning ratio, no citable per-capita
                       standard; the UI should present these as adjustable
                       "Available vs. manual target" figures, not requirements.
    """
    persons_per_family = thresholds["persons_per_family"]
    ffp_days_per_pack = thresholds["ffp_days_per_pack"]
    water_per_person_day = thresholds["water_liters_per_person_per_day"]
    medicine_per_affected = thresholds["medicine_kits_per_affected"]
    vehicles_per_affected = thresholds["vehicles_per_affected"]

    affected = max(0, estimated_affected)
    days = max(0, horizon_days)

    families = ceil(affected / persons_per_family) if affected else 0

    # Water: Sphere minimum, per person, per day. int() keeps whole litres even
    # when the resolved per-person figure is a float.
    water_liters = int(round(affected * water_per_person_day * days))

    # Food: DSWD Family Food Packs, per family, replenished every
    # ffp_days_per_pack days across the horizon.
    packs_per_family = ceil(days / ffp_days_per_pack) if days else 0
    food_packs = families * packs_per_family

    # Blankets / NFI kits: DSWD kits are issued per family, not per person.
    blankets = families

    # Evacuation capacity: one shelter slot per affected person.
    evac_capacity = affected

    return {
        "water": {"quantity": water_liters, "unit": "liters", "basis": "standard"},
        "food": {"quantity": food_packs, "unit": "family packs", "basis": "standard"},
        "blankets": {"quantity": blankets, "unit": "kits", "basis": "standard"},
        "evac_capacity": {"quantity": evac_capacity, "unit": "slots", "basis": "standard"},
        # Tier-B assumption classes — adjustable planning targets, not standards.
        "medicine": {
            "quantity": ceil(affected * medicine_per_affected),
            "unit": "kits",
            "basis": "assumption",
        },
        "vehicles": {
            "quantity": ceil(affected * vehicles_per_affected),
            "unit": "vehicles",
            "basis": "assumption",
        },
    }


# ─────────────────────────────────────────────────────────────────────────
# 4. Availability normaliser
# ─────────────────────────────────────────────────────────────────────────
def effective_availability(available_by_class):
    """Normalise the availability figures the route hands in.

    The Stage-3 route does ALL the DB counting (consumables = sum of
    Resource.quantity excluding archived; equipment = count of serviceable/
    available rows; evac_capacity = sum of supporting operational facility
    capacity, lower bound of any range). This function only coerces those
    numbers into a complete, integer-keyed dict so the engine can rely on every
    class being present. It performs no calculation of its own.
    """
    return {cls: int(available_by_class.get(cls, 0)) for cls in NEED_CLASSES}


# ─────────────────────────────────────────────────────────────────────────
# 5. Gaps
# ─────────────────────────────────────────────────────────────────────────
def compute_gaps(needs, available):
    """For each class return need / available / gap / coverage.

    gap = max(0, need - available). coverage = available / need, and is defined
    as 1.0 when nothing is needed (a zero need is fully covered by definition).
    """
    gaps = {}
    for cls, spec in needs.items():
        need = spec["quantity"]
        avail = available.get(cls, 0)
        gap = max(0, need - avail)
        coverage = (avail / need) if need > 0 else 1.0
        gaps[cls] = {
            "need": need,
            "available": avail,
            "gap": gap,
            "coverage": coverage,
            "basis": spec.get("basis", "assumption"),
            "unit": spec.get("unit", ""),
        }
    return gaps


# ─────────────────────────────────────────────────────────────────────────
# 6 & 7. Readiness classification
# ─────────────────────────────────────────────────────────────────────────
def readiness_status(coverage):
    """Map a coverage ratio to a readiness band using READINESS_THRESHOLDS."""
    if coverage >= READINESS_THRESHOLDS["adequate"]:
        return "Adequate"
    if coverage >= READINESS_THRESHOLDS["partial"]:
        return "Partial"
    return "Critical"


# Worst-first ordering so overall readiness is conservative.
_STATUS_RANK = {"Critical": 0, "Partial": 1, "Adequate": 2}


def overall_readiness(list_of_statuses):
    """Return the worst (most conservative) status in the list.

    Empty input means nothing was at risk, so it reports 'Adequate'.
    """
    if not list_of_statuses:
        return "Adequate"
    return min(list_of_statuses, key=lambda s: _STATUS_RANK.get(s, 0))


# ─────────────────────────────────────────────────────────────────────────
# 8. Operational risks (simple, transparent boolean rules)
# ─────────────────────────────────────────────────────────────────────────
def operational_risks(result):
    """Derive human-readable operational-risk notes from a result dict.

    Rules (all explainable):
      * Shelter shortfall — evacuation capacity does not cover the affected.
      * Fleet degraded — vehicles under repair >= serviceable vehicles.
      * Recurrent hazard — historical incidents >= HAZARD_HISTORY_THRESHOLD.
      * Any resource class classified Critical.
    """
    risks = []
    gaps = result.get("gaps", {})

    evac = gaps.get("evac_capacity")
    if evac and evac["gap"] > 0:
        risks.append(
            "Evacuation shelter capacity shortfall — "
            f"{evac['gap']} slots short of the affected population."
        )

    fleet = result.get("fleet", {})
    serviceable = fleet.get("serviceable", 0)
    under_repair = fleet.get("under_repair", 0)
    if (serviceable + under_repair) > 0 and under_repair >= serviceable:
        risks.append(
            "Response fleet degraded — vehicles under repair "
            f"({under_repair}) equal or exceed serviceable vehicles ({serviceable})."
        )

    if result.get("hazard_history_count", 0) >= HAZARD_HISTORY_THRESHOLD:
        risks.append(
            "Recurrent hazard area — "
            f"{result['hazard_history_count']} recorded incidents of this "
            "disaster type; exposure estimate was adjusted upward."
        )

    for cls, g in gaps.items():
        if readiness_status(g["coverage"]) == "Critical":
            risks.append(f"Critical shortfall in {cls.replace('_', ' ')}.")

    return risks


# ─────────────────────────────────────────────────────────────────────────
# 9. Top-level orchestration
# ─────────────────────────────────────────────────────────────────────────
def run_simulation(scenario_facts):
    """Tie every step together into one structured result dict.

    Expected scenario_facts keys (the Stage-3 route assembles these from the DB):
        total_population       int
        risk_level             str  (low/moderate/high/critical)
        hazard_history_count   int  (incidents of the selected disaster type)
        vulnerable_pop         int  (pwd + elderly + children)
        horizon_days           int
        disaster_type          str  (echoed through for display)
        barangay_name          str  (optional, echoed through)
        available_by_class     dict (per-class availability, already counted)
        fleet                  dict (optional: {serviceable, under_repair})
        thresholds             dict (REQUIRED: resolved planning numbers — the
                               route builds this from thresholds.build_engine_
                               thresholds(db); see that module for the shape)
    """
    total_population = scenario_facts.get("total_population", 0)
    risk_level = scenario_facts.get("risk_level", "low")
    hazard_history_count = scenario_facts.get("hazard_history_count", 0)
    vulnerable_pop = scenario_facts.get("vulnerable_pop", 0)
    horizon_days = scenario_facts.get("horizon_days", 0)
    thresholds = scenario_facts["thresholds"]

    exposure = thresholds["exposure_fraction"]
    base_exposure = exposure.get(risk_level, exposure["low"])
    affected = estimate_affected(total_population, hazard_history_count, base_exposure)
    vulnerable = estimate_vulnerable(affected, vulnerable_pop, total_population)

    needs = compute_needs(affected, horizon_days, thresholds)
    available = effective_availability(scenario_facts.get("available_by_class", {}))
    gaps = compute_gaps(needs, available)

    per_class_status = {
        cls: readiness_status(g["coverage"]) for cls, g in gaps.items()
    }
    overall = overall_readiness(list(per_class_status.values()))

    result = {
        "inputs": {
            "disaster_type": scenario_facts.get("disaster_type"),
            "barangay_name": scenario_facts.get("barangay_name"),
            "total_population": total_population,
            "risk_level": risk_level,
            "horizon_days": horizon_days,
        },
        "hazard_history_count": hazard_history_count,
        "estimated_affected": affected,
        "estimated_vulnerable": vulnerable,
        "needs": needs,
        "available": available,
        "gaps": gaps,
        "status_by_class": per_class_status,
        "overall_readiness": overall,
        "fleet": scenario_facts.get("fleet", {}),
    }
    result["operational_risks"] = operational_risks(result)
    return result
