"""
Disaster Simulator — Pydantic schemas.

ScenarioInput validates what the admin submits from the setup form (never the
affected count — that is computed server-side per TR-ADM-08). SimulationResult
is a typed mirror of the pure engine's output dict so /docs shows a clear shape.
"""

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel

from app.models import DisasterType


# The four duration keys mirror DURATION_OPTIONS in app/simulation/routes.py
# (the Stage-0 GET /setup mapping). The route reuses that same mapping to turn
# the key into horizon days; here we only constrain the accepted values.
DurationKey = Literal["1_day", "3_days", "1_week", "2_weeks"]


class ScenarioInput(BaseModel):
    """One simulation request. No severity field (TR-ADM-07); no affected count
    (TR-ADM-08 — the server derives it)."""
    barangay_id: int
    disaster_type: DisasterType
    duration: DurationKey


# ── Typed mirror of the engine output ────────────────────────────────────

class NeedItem(BaseModel):
    quantity: int
    unit: str
    basis: str          # "standard" | "assumption"


class GapItem(BaseModel):
    need: int
    available: int
    gap: int
    coverage: float
    basis: str
    unit: str


class ResultInputs(BaseModel):
    disaster_type: Optional[str] = None
    barangay_name: Optional[str] = None
    total_population: int
    risk_level: str
    horizon_days: int


class SimulationResult(BaseModel):
    inputs: ResultInputs
    hazard_history_count: int
    estimated_affected: int
    estimated_vulnerable: int
    needs: Dict[str, NeedItem]
    available: Dict[str, int]
    gaps: Dict[str, GapItem]
    status_by_class: Dict[str, str]
    overall_readiness: str
    fleet: Dict[str, int]
    operational_risks: List[str]
    # Set by the route: False when the selected disaster type is not listed in
    # Barangay.hazard_types, with an accompanying advisory note.
    hazard_recorded: bool
    hazard_note: Optional[str] = None
