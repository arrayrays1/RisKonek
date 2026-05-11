from sqlalchemy.orm import Session
from app.models import Barangay, Incident, Population, RiskLevel, Severity
from datetime import date

# Severity weights for incident scoring
SEVERITY_WEIGHTS = {
    "minor": 1,
    "moderate": 2,
    "major": 3,
    "catastrophic": 4
}

# Hazard type weights — more dangerous = higher weight
HAZARD_WEIGHTS = {
    "flood": 3,
    "earthquake": 3,
    "landslide": 4,
    "typhoon": 2,
    "fire": 1,
}

def compute_risk_score(barangay: Barangay, incidents: list, population: Population) -> dict:
    """
    Computes a 0-100 risk score for a barangay based on:
    - Hazard exposure (40%)
    - Incident history (40%)
    - Population size (20%)

    Returns a dict with score, level, and breakdown.
    """

    # ── FACTOR 1: Hazard Exposure Score (0–100) ──────────────────
    hazard_score = 0
    if barangay.hazard_types:
        hazards = [h.strip() for h in barangay.hazard_types.split(",")]
        raw = sum(HAZARD_WEIGHTS.get(h, 1) for h in hazards)
        # Max possible: flood+earthquake+landslide+typhoon = 12
        hazard_score = min((raw / 12) * 100, 100)

    # ── FACTOR 2: Incident History Score (0–100) ─────────────────
    incident_score = 0
    if incidents:
        today = date.today()
        weighted_total = 0
        for inc in incidents:
            severity_w = SEVERITY_WEIGHTS.get(inc.severity.value, 1)
            # Recent incidents (last 3 years) count more
            years_ago = (today - inc.date_occurred).days / 365
            recency_w = 1.5 if years_ago <= 3 else 1.0
            weighted_total += severity_w * recency_w

        # Normalize: 10+ weighted incidents = max score
        incident_score = min((weighted_total / 10) * 100, 100)

    # ── FACTOR 3: Population Vulnerability Score (0–100) ─────────
    pop_score = 0
    if population:
        # San Pedro's largest barangay is San Antonio with ~59,000
        # Use that as the max reference
        pop_score = min((population.total_population / 60000) * 100, 100)

    # ── WEIGHTED FINAL SCORE ──────────────────────────────────────
    final_score = (
        (hazard_score * 0.40) +
        (incident_score * 0.40) +
        (pop_score * 0.20)
    )
    final_score = round(final_score, 1)

    # ── MAP SCORE TO RISK LEVEL ───────────────────────────────────
    if final_score >= 70:
        level = RiskLevel.critical
    elif final_score >= 45:
        level = RiskLevel.high
    elif final_score >= 20:
        level = RiskLevel.moderate
    else:
        level = RiskLevel.low

    return {
        "score": final_score,
        "level": level,
        "breakdown": {
            "hazard_score": round(hazard_score, 1),
            "incident_score": round(incident_score, 1),
            "population_score": round(pop_score, 1),
        }
    }


def update_all_risk_levels(db: Session):
    """
    Recomputes and updates risk levels for all 27 barangays.
    Call this after new incidents are added or hazard data changes.
    """
    barangays = db.query(Barangay).all()
    for brgy in barangays:
        incidents = brgy.incidents
        population = db.query(Population).filter(
            Population.barangay_id == brgy.id
        ).order_by(Population.recorded_at.desc()).first()

        result = compute_risk_score(brgy, incidents, population)
        brgy.risk_level = result["level"]

    db.commit()
    print("✓ Risk levels updated for all barangays")