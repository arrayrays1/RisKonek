import uuid
from collections import OrderedDict
from urllib.parse import urlencode

from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session
from pydantic import ValidationError

from app.database import get_db
from app.models import (
    Barangay, DisasterType, Population, Resource, ResourceCategory,
    Equipment, EquipmentType, EquipmentStatus, Facility, FacilityStatus,
    Incident, PlanningThreshold, log_action,
)
from app.auth import require_role
from app.simulation import engine
from app.simulation import ai_layer
from app.simulation import thresholds as thresholds_svc
from app.simulation.schemas import ScenarioInput
from typing import Optional

router = APIRouter(prefix="/admin/simulator")
templates = Jinja2Templates(directory="app/templates")

# ── Short-lived server-side store for the Post/Redirect/Get flow ──────────
# The result dict + sanitized AI briefing are too large to safely round-trip in
# the signed session cookie (~4 KB limit), so POST stashes them here keyed by an
# unguessable run id and redirects to GET, which renders from the store. A
# refresh of the GET page re-reads the store — it never recomputes or re-calls
# Groq. Capped with FIFO eviction so it can't grow unbounded. (Single-process
# store: for a multi-worker deployment this would move to a shared cache/DB.)
_RUN_STORE = OrderedDict()
_RUN_STORE_MAX = 50


def _store_run(payload: dict) -> str:
    run_id = uuid.uuid4().hex
    _RUN_STORE[run_id] = payload
    while len(_RUN_STORE) > _RUN_STORE_MAX:
        _RUN_STORE.popitem(last=False)   # drop oldest
    return run_id

# Estimated-duration presets. The value is what the engine reads (via `days`)
# to size water/food needs; the label is what the planner sees. "3 days" is the
# default per the task spec.
DURATION_OPTIONS = [
    {"value": "1_day", "label": "1 day", "days": 1},
    {"value": "3_days", "label": "3 days", "days": 3},
    {"value": "1_week", "label": "1 week", "days": 7},
    {"value": "2_weeks", "label": "2 weeks", "days": 14},
]
# Fast key -> horizon days lookup reused by the /run handler.
DURATION_DAYS = {opt["value"]: opt["days"] for opt in DURATION_OPTIONS}

# Which Facility.supports_* boolean gates evacuation capacity for each disaster
# type. earthquake / other have no dedicated support flag, so they fall back to
# counting all operational, non-archived facilities (evacuees still need shelter).
DISASTER_SUPPORT_ATTR = {
    "flood": "supports_flooding",
    "fire": "supports_fire",
    "landslide": "supports_landslide",
    "typhoon": "supports_tropical_cyclone",
}

# Equipment statuses treated as READY (assignable) vs DOWN (out of action) for
# the fleet dict. `deployed` is deliberately in NEITHER set — it is operational
# but already committed elsewhere. (Confirmed mapping, Stage 3.)
READY_STATUSES = (EquipmentStatus.serviceable, EquipmentStatus.available)
DOWN_STATUSES = (
    EquipmentStatus.under_repair,
    EquipmentStatus.not_serviceable,
    EquipmentStatus.unserviceable,
)
# The "fleet"/vehicles class counts only actual response vehicles, not gear.
VEHICLE_TYPES = (
    EquipmentType.fire_truck,
    EquipmentType.ambulance,
    EquipmentType.rescue_vehicle,
    EquipmentType.rescue_boat,
)


def _capacity_lower_bound(raw):
    """Facility capacity fields are strings that may hold ranges like "40-80".
    Parse the CONSERVATIVE (lower) bound; unparseable/blank -> 0."""
    if not raw:
        return 0
    first = str(raw).split("-")[0].strip()
    try:
        return int(float(first))
    except ValueError:
        return 0


@router.get("/setup", response_class=HTMLResponse)
def simulator_setup(request: Request, db: Session = Depends(get_db)):
    user = require_role(request, ["admin"])
    if isinstance(user, RedirectResponse):
        return user

    barangays = db.query(Barangay).order_by(Barangay.name).all()

    return templates.TemplateResponse(
        request=request,
        name="admin/simulator_setup.html",
        context={
            "title": "Disaster Simulator — RisKonek",
            "user": user,
            "barangays": barangays,
            "disaster_types": list(DisasterType),
            "duration_options": DURATION_OPTIONS,
            "default_duration": "3_days",
        },
    )


@router.post("/run")
def simulator_run(
    request: Request,
    db: Session = Depends(get_db),
    barangay_id: int = Form(...),
    disaster_type: str = Form(...),
    duration: str = Form(...),
):
    """Assemble scenario facts from the DB (read-only) and run the pure engine.

    The ROUTE does every query; the engine receives only plain ints/strings/
    dicts — no ORM objects. Stage 4 will render this; for now it returns JSON.
    """
    user = require_role(request, ["admin"])
    if isinstance(user, RedirectResponse):
        return user

    # Validate the submitted scenario (disaster type + duration + barangay id).
    try:
        scenario = ScenarioInput(
            barangay_id=barangay_id,
            disaster_type=disaster_type,
            duration=duration,
        )
    except ValidationError as exc:
        return JSONResponse(status_code=422, content={"errors": exc.errors()})

    barangay = db.query(Barangay).filter(Barangay.id == scenario.barangay_id).first()
    if barangay is None:
        return JSONResponse(status_code=404, content={"error": "Barangay not found."})

    dtype = scenario.disaster_type            # DisasterType enum
    horizon_days = DURATION_DAYS[scenario.duration]

    # ── Population — latest record for this barangay ──────────────────────
    pop = (
        db.query(Population)
        .filter(Population.barangay_id == barangay.id)
        .order_by(Population.recorded_at.desc())
        .first()
    )
    total_population = (pop.total_population or 0) if pop else 0
    vulnerable_pop = (
        (pop.pwd_count or 0) + (pop.elderly_count or 0) + (pop.children_count or 0)
        if pop else 0
    )

    # ── Resources — sum quantity per category (exclude archived) ──────────
    def _resource_sum(category):
        return int(
            db.query(func.coalesce(func.sum(Resource.quantity), 0))
            .filter(Resource.category == category, Resource.is_archived == False)
            .scalar()
            or 0
        )

    water_avail = _resource_sum(ResourceCategory.water)
    food_avail = _resource_sum(ResourceCategory.food)
    medicine_avail = _resource_sum(ResourceCategory.medicine)
    blankets_avail = _resource_sum(ResourceCategory.shelter)   # shelter = NFI/blankets

    # ── Equipment — vehicle fleet only, by ready/down mapping ─────────────
    def _vehicle_count(statuses):
        return int(
            db.query(func.count(Equipment.id))
            .filter(
                Equipment.equipment_type.in_(VEHICLE_TYPES),
                Equipment.status.in_(statuses),
                Equipment.is_archived == False,
            )
            .scalar()
            or 0
        )

    vehicles_ready = _vehicle_count(READY_STATUSES)
    vehicles_down = _vehicle_count(DOWN_STATUSES)

    # ── Facilities — evacuation capacity supporting this disaster type ────
    fac_q = db.query(Facility).filter(
        Facility.is_archived == False,
        Facility.operational_status == FacilityStatus.available,   # "operational"
    )
    support_attr = DISASTER_SUPPORT_ATTR.get(dtype.value)
    if support_attr:
        fac_q = fac_q.filter(getattr(Facility, support_attr) == True)
    evac_capacity = sum(_capacity_lower_bound(f.capacity_individuals) for f in fac_q.all())

    # ── Incidents — history of this disaster type here (feeds the bump) ───
    hazard_history_count = int(
        db.query(func.count(Incident.id))
        .filter(Incident.barangay_id == barangay.id, Incident.disaster_type == dtype)
        .scalar()
        or 0
    )

    # ── Assemble PLAIN facts for the pure engine (no ORM objects) ─────────
    scenario_facts = {
        "disaster_type": dtype.value,
        "barangay_name": barangay.name,
        "total_population": total_population,
        "risk_level": barangay.risk_level.value if barangay.risk_level else "low",
        "hazard_history_count": hazard_history_count,
        "vulnerable_pop": vulnerable_pop,
        "horizon_days": horizon_days,
        "available_by_class": {
            "water": water_avail,
            "food": food_avail,
            "blankets": blankets_avail,
            "medicine": medicine_avail,
            "evac_capacity": evac_capacity,
            "vehicles": vehicles_ready,
        },
        "fleet": {"serviceable": vehicles_ready, "under_repair": vehicles_down},
        # Resolved (override-or-default) planning numbers — the ROUTE reads them
        # from the DB so the engine stays pure. build_engine_thresholds returns
        # plain floats/dicts only, never ORM objects or a session.
        "thresholds": thresholds_svc.build_engine_thresholds(db),
    }

    result = engine.run_simulation(scenario_facts)
    result["duration_label"] = next(
        (o["label"] for o in DURATION_OPTIONS if o["value"] == scenario.duration),
        scenario.duration,
    )

    # ── Hazard-recorded advisory ──────────────────────────────────────────
    recorded_hazards = {
        h.strip().lower() for h in (barangay.hazard_types or "").split(",") if h.strip()
    }
    hazard_recorded = dtype.value in recorded_hazards
    result["hazard_recorded"] = hazard_recorded
    result["hazard_note"] = (
        None if hazard_recorded else
        f"'{dtype.value}' is not in {barangay.name}'s recorded hazard types; "
        "this scenario is hypothetical for that barangay."
    )

    # ── Audit — the ONLY write in this handler ────────────────────────────
    log_action(
        db, user["id"], "simulated", "simulator", barangay.id,
        f"Ran disaster simulation: {dtype.value} in {barangay.name} "
        f"(horizon {horizon_days}d) — affected≈{result['estimated_affected']}, "
        f"readiness={result['overall_readiness']}",
    )

    # ── Optional AI briefing ──────────────────────────────────────────────
    # The numbers are the product; the AI is an optional explanation layer. If
    # the key is missing, the API errors, or it times out, we render the full
    # numeric report anyway with a visible "unavailable" note. The exception is
    # NEVER surfaced to the browser (it could, in theory, echo config details)
    # and the API key is never logged.
    ai_briefing = None   # sanitized HTML fragment, safe to render
    ai_note = None
    try:
        raw_briefing = ai_layer.explain_simulation(result)
        ai_briefing = ai_layer.to_safe_html(raw_briefing)
    except Exception as exc:
        # Distinguish a Groq 429 / rate-limit from every other failure, without
        # importing the SDK's exception classes here. Check the HTTP status code
        # (Groq's APIStatusError carries .status_code) and the type name.
        status = getattr(exc, "status_code", None)
        if status == 429 or type(exc).__name__ == "RateLimitError":
            ai_note = (
                "The AI summary is temporarily unavailable (usage limit reached). "
                "The numeric report below is complete and unaffected."
            )
        else:
            ai_note = (
                "The AI summary is temporarily unavailable. The numeric report "
                "below is complete and unaffected."
            )
        # Log the failure TYPE only — never the message (avoids leaking the key
        # or config) and never the key itself.
        print(f"[simulator] AI briefing unavailable: {type(exc).__name__}")

    # ── Post/Redirect/Get ─────────────────────────────────────────────────
    # Stash the computed result + briefing and redirect (303) to the GET view.
    # This makes refresh re-fetch the stored run instead of re-POSTing, so no
    # second simulation, no second Groq call, and no duplicate audit row.
    run_id = _store_run({"result": result, "ai_briefing": ai_briefing,
                         "ai_note": ai_note, "user_id": user["id"]})
    return RedirectResponse(url=f"/admin/simulator/results/{run_id}", status_code=303)


@router.get("/results/{run_id}", response_class=HTMLResponse)
def simulator_results(request: Request, run_id: str, db: Session = Depends(get_db)):
    """Render a stored simulation run. Refreshing this page never triggers a new
    simulation or Groq call — it only reads the server-side store."""
    user = require_role(request, ["admin"])
    if isinstance(user, RedirectResponse):
        return user

    run = _RUN_STORE.get(run_id)
    # Missing (evicted/invalid) or owned by another user -> back to setup.
    if run is None or run.get("user_id") != user["id"]:
        return RedirectResponse(url="/admin/simulator/setup", status_code=303)

    return templates.TemplateResponse(
        request=request,
        name="admin/simulator_results.html",
        context={
            "title": "Simulation Results — RisKonek",
            "user": user,
            "result": run["result"],
            "ai_briefing": run["ai_briefing"],
            "ai_note": run["ai_note"],
        },
    )


# ═══════════════════════════════════════════════════════════════════════════
# PLANNING THRESHOLD SETTINGS (admin-editable, three-tier)
# ═══════════════════════════════════════════════════════════════════════════

# The keys the settings form is allowed to edit (locked keys are display-only
# and never accepted as changes here).
_EDITABLE_KEYS = [k for k, m in thresholds_svc.DEFAULTS.items() if m["tier"] != "locked"]


def _settings_sections(db):
    """Build the per-tier row lists the template renders, each item carrying the
    live row plus its catalog metadata (label, default, floor, source)."""
    rows = {r.key: r for r in db.query(PlanningThreshold).all()}
    sections = {"floored": [], "local": [], "locked": []}
    for key, meta in thresholds_svc.DEFAULTS.items():
        row = rows.get(key)
        sections[meta["tier"]].append({
            "key": key,
            "value": row.value if row else meta["value"],
            "default": meta["value"],
            "tier": meta["tier"],
            "label": meta["label"],
            "unit": meta["unit"],
            "floor": meta["floor_value"],
            "source_label": meta["source_label"],
        })
    return sections


@router.get("/settings", response_class=HTMLResponse)
def simulator_settings(
    request: Request,
    db: Session = Depends(get_db),
    success: Optional[str] = None,
    error: Optional[str] = None,
):
    user = require_role(request, ["admin"])
    if isinstance(user, RedirectResponse):
        return user

    thresholds_svc.ensure_seeded(db)
    return templates.TemplateResponse(
        request=request,
        name="admin/simulator_settings.html",
        context={
            "title": "Planning Thresholds — RisKonek",
            "user": user,
            "sections": _settings_sections(db),
            "success": success,
            "error": error,
        },
    )


@router.post("/settings")
def simulator_settings_save(
    request: Request,
    db: Session = Depends(get_db),
    reset_key: Optional[str] = Form(None),
    WATER_LITERS_PER_PERSON_PER_DAY: Optional[str] = Form(None),
    EXPOSURE_FRACTION_LOW: Optional[str] = Form(None),
    EXPOSURE_FRACTION_MODERATE: Optional[str] = Form(None),
    EXPOSURE_FRACTION_HIGH: Optional[str] = Form(None),
    EXPOSURE_FRACTION_CRITICAL: Optional[str] = Form(None),
    MEDICINE_KITS_PER_AFFECTED: Optional[str] = Form(None),
    VEHICLES_PER_AFFECTED: Optional[str] = Form(None),
):
    user = require_role(request, ["admin"])
    if isinstance(user, RedirectResponse):
        return user

    thresholds_svc.ensure_seeded(db)

    def _redirect(**params):
        return RedirectResponse(
            url="/admin/simulator/settings?" + urlencode(params), status_code=303
        )

    def _apply(key, new_value):
        """Update one row + audit old->new. Returns True if it changed."""
        row = db.query(PlanningThreshold).filter(PlanningThreshold.key == key).first()
        old = row.value
        if old == new_value:
            return False
        row.value = new_value
        row.updated_by = user["id"]
        db.add(row)
        db.flush()
        log_action(
            db, user["id"], "updated", "planning_thresholds", row.id,
            f"{key}: {old:g} -> {new_value:g}",
        )  # log_action commits
        return True

    # ── Per-field reset to the config.py default ──────────────────────────
    if reset_key:
        meta = thresholds_svc.DEFAULTS.get(reset_key)
        if meta is None or meta["tier"] == "locked":
            return _redirect(error="That value cannot be reset here.")
        _apply(reset_key, float(meta["value"]))
        return _redirect(success=f"{meta['label']} reset to default.")

    # ── Validate ALL edits first (all-or-nothing), then apply ─────────────
    submitted = {
        "WATER_LITERS_PER_PERSON_PER_DAY": WATER_LITERS_PER_PERSON_PER_DAY,
        "EXPOSURE_FRACTION_LOW": EXPOSURE_FRACTION_LOW,
        "EXPOSURE_FRACTION_MODERATE": EXPOSURE_FRACTION_MODERATE,
        "EXPOSURE_FRACTION_HIGH": EXPOSURE_FRACTION_HIGH,
        "EXPOSURE_FRACTION_CRITICAL": EXPOSURE_FRACTION_CRITICAL,
        "MEDICINE_KITS_PER_AFFECTED": MEDICINE_KITS_PER_AFFECTED,
        "VEHICLES_PER_AFFECTED": VEHICLES_PER_AFFECTED,
    }
    # Ratio fields are entered in the human unit "1 per N": the form submits N
    # (a positive integer), and we store the decimal 1/N. Conversion happens ONLY
    # here at save time, so units/engine math downstream are unchanged.
    ratio_keys = {"MEDICINE_KITS_PER_AFFECTED", "VEHICLES_PER_AFFECTED"}

    def _ratio_n_to_value(raw):
        """('1 per N') -> (value, None) or (None, error)."""
        s = str(raw).strip()
        try:
            f = float(s)
        except ValueError:
            return None, "Enter a whole number of people."
        if f != int(f):
            return None, "Enter a whole number of people."
        n = int(f)
        if n <= 0:
            return None, "The number of people must be greater than zero."
        return 1.0 / n, None

    validated = {}
    for key, raw in submitted.items():
        if raw is None or str(raw).strip() == "":
            continue
        if key in ratio_keys:
            value, n_err = _ratio_n_to_value(raw)
            if n_err:
                return _redirect(error=n_err)
        else:
            try:
                value = float(raw)
            except ValueError:
                return _redirect(error="Value must be a number.")
        # Same tier validation as before, run on the STORED decimal.
        err = thresholds_svc.validate_threshold(key, value)
        if err:
            return _redirect(error=err)   # reject everything; change nothing
        validated[key] = value

    changed = sum(_apply(key, val) for key, val in validated.items())
    return _redirect(success=f"Saved. {changed} value(s) updated.")
