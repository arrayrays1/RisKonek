"""
Resource Simulator — Pydantic schemas.

ScenarioInput validates what the admin submits from the setup form (never the
affected count — that is computed server-side per TR-ADM-08). SimulationResult
is a typed mirror of the pure engine's output dict so /docs shows a clear shape.
"""

from datetime import date
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel

from app.models import DisasterType


class ScenarioInput(BaseModel):
    """One simulation request. No severity field (TR-ADM-07); no affected count
    (TR-ADM-08 — the server derives it).

    duration is expressed as an explicit calendar date range (date_from/date_to,
    inclusive on both ends) rather than a fixed preset — the route turns that
    into `horizon_days` for the engine and a human label for display. Ordering
    (date_to >= date_from) is checked in the route, not here, since it's a
    cross-field rule and the two dates already parse independently.
    """
    barangay_id: int
    disaster_type: DisasterType
    date_from: date
    date_to: date


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
