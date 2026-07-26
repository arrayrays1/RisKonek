import json
import re
import uuid
from collections import OrderedDict
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session
from pydantic import ValidationError

from app.database import get_db
from app.models import (
    Barangay, DisasterType, Population, Resource, ResourceCategory,
    Equipment, EquipmentType, EquipmentStatus, Facility, FacilityStatus,
    Incident, PlanningThreshold, SavedScenario, log_action,
)
from app.auth import require_role
from app.simulation import engine
from app.simulation import ai_layer
from app.simulation import thresholds as thresholds_svc
from app.simulation import weather as weather_svc
from app.simulation import config as C
from app.simulation.schemas import ScenarioInput
from app.simulation import pdf_export
from app.analytics.simulator import compute_risk_score
from typing import Optional

router = APIRouter(prefix="/admin/simulator")
templates = Jinja2Templates(directory="app/templates")

# Timestamps are stored UTC; display in Philippine Standard Time (UTC+8),
# mirroring the `pht` filter registered in app/routes/admin.py.
_PHT = timezone(timedelta(hours=8))


def _to_pht(dt):
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_PHT).strftime('%B %d, %Y at %I:%M %p')


templates.env.filters['pht'] = _to_pht

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
# Fast key -> human label lookup reused by the saved-scenario views.
DURATION_LABELS = {opt["value"]: opt["label"] for opt in DURATION_OPTIONS}

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
def simulator_setup(
    request: Request,
    db: Session = Depends(get_db),
    barangay_id: Optional[str] = None,
):
    user = require_role(request, ["admin"])
    if isinstance(user, RedirectResponse):
        return user

    barangays = db.query(Barangay).order_by(Barangay.name).all()

    # Optional pre-selection when arriving from a barangay profile
    # ("Use for Planning"). Coerce safely — blank/garbage never selects.
    preselect_id = int(barangay_id) if (barangay_id or "").strip().isdigit() else None
    preselect_barangay = next(
        (b for b in barangays if b.id == preselect_id), None
    ) if preselect_id else None

    # ── Saved-scenario history (newest first) shown below the setup form ───
    # Compact by default: 10 most recent, with a "View all" toggle so the form
    # stays visible at the top.
    show_all = request.query_params.get("all") == "1"
    saved_q = db.query(SavedScenario).order_by(SavedScenario.created_at.desc())
    total_saved = saved_q.count()
    rows = saved_q.all() if show_all else saved_q.limit(10).all()
    saved_rows = [
        {
            "id": s.id,
            "name": s.name,
            "barangay_name": s.barangay_name,
            "disaster_type": s.disaster_type,
            "duration_label": DURATION_LABELS.get(s.duration, s.duration),
            "readiness": json.loads(s.result_json).get("overall_readiness"),
            "saved_by": s.created_by_user.username if s.created_by_user else "—",
            "created_at": s.created_at,
        }
        for s in rows
    ]

    # ── Right column: weather outlook (server-side, fail-safe) + priority list ─
    weather = weather_svc.get_outlook()   # dict or None (never raises)

    # Priority barangays: compute the study risk score for each configured name,
    # then rank highest-first. Score/level reuse the existing analytics formula
    # (app/analytics/simulator.py) — the same numbers shown on the profile page.
    risk_bar_class = {
        "critical": "bg-danger", "high": "bg-warning",
        "moderate": "bg-info", "low": "bg-success",
    }
    priority_rows = []
    for brgy in (
        db.query(Barangay).filter(Barangay.name.in_(C.PRIORITY_BARANGAYS)).all()
    ):
        pop = (
            db.query(Population)
            .filter(Population.barangay_id == brgy.id)
            .order_by(Population.recorded_at.desc())
            .first()
        )
        rr = compute_risk_score(brgy, brgy.incidents, pop)
        level = rr["level"].value
        hazards = [
            h.strip().title()
            for h in (brgy.hazard_types or "").split(",") if h.strip()
        ]
        priority_rows.append({
            "id": brgy.id,
            "name": brgy.name,
            "hazards": ", ".join(hazards) if hazards else "—",
            "population": pop.total_population if pop else 0,
            "score": rr["score"],
            "level": level,
            "bar_class": risk_bar_class.get(level, "bg-secondary"),
        })
    priority_rows.sort(key=lambda r: r["score"], reverse=True)

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
            "preselect_barangay_id": preselect_barangay.id if preselect_barangay else None,
            "preselect_barangay_name": preselect_barangay.name if preselect_barangay else None,
            "saved_rows": saved_rows,
            "total_saved": total_saved,
            "show_all": show_all,
            "weather": weather,
            "priority_rows": priority_rows,
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
    run_id = _store_run({
        "result": result, "ai_briefing": ai_briefing, "ai_note": ai_note,
        "user_id": user["id"],
        # Extra snapshot facts the /save route freezes into a SavedScenario.
        "barangay_id": barangay.id,
        "duration": scenario.duration,
        "thresholds": scenario_facts["thresholds"],
        # When this run was generated (UTC) — used to autofill the save name.
        "generated_at": datetime.utcnow(),
    })
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

    # Suggested save name: barangay — duration — disaster type — generated date/time.
    # Editable in the modal; the planner can override before saving.
    ir = run["result"]["inputs"]
    default_scenario_name = (
        f"{ir['barangay_name']} — {DURATION_LABELS.get(run.get('duration'), '')} — "
        f"{str(ir['disaster_type']).title()} — {_to_pht(run.get('generated_at'))}"
    )

    return templates.TemplateResponse(
        request=request,
        name="admin/simulator_results.html",
        context={
            "title": "Simulation Results — RisKonek",
            "user": user,
            "result": run["result"],
            "ai_briefing": run["ai_briefing"],
            "ai_note": run["ai_note"],
            "run_id": run_id,
            "default_scenario_name": default_scenario_name,
            # Set once this run has been saved, so the button flips to a link
            # and a second POST can't create a duplicate SavedScenario.
            "saved_scenario_id": run.get("saved_scenario_id"),
        },
    )


# ═══════════════════════════════════════════════════════════════════════════
# SAVED SCENARIOS (frozen snapshots — save / view / delete)
# ═══════════════════════════════════════════════════════════════════════════


@router.post("/save")
def simulator_save(
    request: Request,
    db: Session = Depends(get_db),
    run_id: str = Form(...),
    name: str = Form(...),
):
    """Freeze a stored run into a SavedScenario. Reads the run store (never
    recomputes), persists the computed result + the thresholds used, audits it,
    and redirects to the saved-scenario view."""
    user = require_role(request, ["admin"])
    if isinstance(user, RedirectResponse):
        return user

    run = _RUN_STORE.get(run_id)
    if run is None or run.get("user_id") != user["id"]:
        return RedirectResponse(url="/admin/simulator/setup", status_code=303)

    # Already saved (double-submit / refresh) → go to the existing record.
    existing_id = run.get("saved_scenario_id")
    if existing_id:
        return RedirectResponse(
            url=f"/admin/simulator/scenarios/{existing_id}?"
                + urlencode({"success": "This run was already saved."}),
            status_code=303,
        )

    result = run["result"]
    inputs = result["inputs"]
    label = (name or "").strip() or (
        f"{str(inputs['disaster_type']).title()} — {inputs['barangay_name']}"
    )

    scenario = SavedScenario(
        name=label[:150],
        barangay_id=run.get("barangay_id"),
        barangay_name=inputs["barangay_name"],
        disaster_type=inputs["disaster_type"],
        duration=run.get("duration"),
        horizon_days=inputs["horizon_days"],
        result_json=json.dumps(result),
        ai_briefing=run.get("ai_briefing"),
        thresholds_json=json.dumps(run.get("thresholds") or {}),
        created_by=user["id"],
    )
    db.add(scenario)
    db.commit()
    db.refresh(scenario)

    # Mark the run so a repeat POST for the same run_id can't duplicate it.
    run["saved_scenario_id"] = scenario.id

    log_action(
        db, user["id"], "saved", "saved_scenarios", scenario.id,
        f"Saved scenario '{label}': {scenario.disaster_type} in "
        f"{scenario.barangay_name} (readiness={result['overall_readiness']})",
    )

    return RedirectResponse(
        url=f"/admin/simulator/scenarios/{scenario.id}?"
            + urlencode({"success": "Scenario saved."}),
        status_code=303,
    )


# ── Threshold-diff helpers (compare view) ────────────────────────────────


def _flatten_thresholds(d, prefix=""):
    """Flatten the nested thresholds dict to dotted keys for a flat diff."""
    out = {}
    for k, v in (d or {}).items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten_thresholds(v, prefix=key + "."))
        else:
            out[key] = v
    return out


def _humanize_threshold(k: str) -> str:
    if "." in k:
        base, sub = k.split(".", 1)
        return base.replace("_", " ").title() + f" ({sub})"
    return k.replace("_", " ").title()


def _threshold_diff(a_json, b_json):
    """Return the list of thresholds whose stored values differ between two
    snapshots. Empty list => identical planning assumptions."""
    try:
        a = _flatten_thresholds(json.loads(a_json or "{}"))
        b = _flatten_thresholds(json.loads(b_json or "{}"))
    except (ValueError, TypeError):
        return []
    diffs = []
    for k in sorted(set(a) | set(b)):
        if a.get(k) != b.get(k):
            diffs.append({"label": _humanize_threshold(k), "a": a.get(k), "b": b.get(k)})
    return diffs


@router.get("/scenarios/compare", response_class=HTMLResponse)
def saved_scenario_compare(
    request: Request,
    db: Session = Depends(get_db),
    a: Optional[str] = None,
    b: Optional[str] = None,
):
    """Side-by-side comparison built ONLY from two stored result_json snapshots.
    No recomputation, no Groq call. Declared before /scenarios/{scenario_id} so
    the literal 'compare' path isn't captured by the int id route."""
    user = require_role(request, ["admin"])
    if isinstance(user, RedirectResponse):
        return user

    def _coerce(x):
        try:
            return int(x)
        except (TypeError, ValueError):
            return None

    ida, idb = _coerce(a), _coerce(b)
    if ida is None or idb is None:
        return RedirectResponse(
            url="/admin/simulator/setup?"
                + urlencode({"error": "Select two scenarios to compare."}),
            status_code=303,
        )
    if ida == idb:
        return RedirectResponse(
            url="/admin/simulator/setup?"
                + urlencode({"error": "Select two different scenarios to compare."}),
            status_code=303,
        )

    sa = db.query(SavedScenario).filter(SavedScenario.id == ida).first()
    sb = db.query(SavedScenario).filter(SavedScenario.id == idb).first()
    if sa is None or sb is None:
        return RedirectResponse(
            url="/admin/simulator/setup?"
                + urlencode({"error": "One or both scenarios were not found."}),
            status_code=303,
        )

    ra = json.loads(sa.result_json)
    rb = json.loads(sb.result_json)
    gaps_a, gaps_b = ra.get("gaps", {}), rb.get("gaps", {})
    status_a, status_b = ra.get("status_by_class", {}), rb.get("status_by_class", {})

    # Per-class rows with a delta. A lower gap (shortfall) in B = improvement.
    compare_rows = []
    for c in pdf_export.DISPLAY_ORDER:
        if c in gaps_a and c in gaps_b:
            ga, gb = gaps_a[c], gaps_b[c]
            delta_gap = gb.get("gap", 0) - ga.get("gap", 0)
            direction = ("improved" if delta_gap < 0
                         else "worsened" if delta_gap > 0 else "same")
            compare_rows.append({
                "label": pdf_export.CLASS_LABEL.get(c, c),
                "unit": ga.get("unit", ""),
                "a": ga, "b": gb,
                "status_a": status_a.get(c), "status_b": status_b.get(c),
                "delta_gap": delta_gap, "abs_delta": abs(delta_gap),
                "direction": direction,
            })

    def _meta(s, r):
        return {
            "id": s.id, "name": s.name,
            "barangay": r["inputs"]["barangay_name"],
            "disaster_type": r["inputs"]["disaster_type"],
            "duration_label": r.get("duration_label", s.duration),
            "saved_by": s.created_by_user.username if s.created_by_user else "—",
            "created_at": s.created_at,
            "readiness": r.get("overall_readiness"),
        }

    return templates.TemplateResponse(
        request=request,
        name="admin/scenario_compare.html",
        context={
            "title": "Compare Scenarios — RisKonek",
            "user": user,
            "a": _meta(sa, ra), "b": _meta(sb, rb),
            "compare_rows": compare_rows,
            "threshold_diffs": _threshold_diff(sa.thresholds_json, sb.thresholds_json),
        },
    )


@router.get("/scenarios/{scenario_id}", response_class=HTMLResponse)
def saved_scenario_detail(
    request: Request, scenario_id: int, db: Session = Depends(get_db)
):
    """Render one saved scenario from its stored snapshot. This path NEVER
    recomputes and NEVER calls Groq — result + AI briefing come straight from
    the row, so it always reflects the data and thresholds at run time."""
    user = require_role(request, ["admin"])
    if isinstance(user, RedirectResponse):
        return user

    row = db.query(SavedScenario).filter(SavedScenario.id == scenario_id).first()
    if row is None:
        return RedirectResponse(
            url="/admin/simulator/setup?"
                + urlencode({"error": "Saved scenario not found."}),
            status_code=303,
        )

    result = json.loads(row.result_json)
    saved_by = row.created_by_user.username if row.created_by_user else "—"

    return templates.TemplateResponse(
        request=request,
        name="admin/scenario_detail.html",
        context={
            "title": f"{row.name} — Saved Scenario — RisKonek",
            "user": user,
            "scenario": row,
            "saved_by": saved_by,
            "result": result,
            "ai_briefing": row.ai_briefing,
            "ai_note": None,
        },
    )


@router.get("/scenarios/{scenario_id}/pdf")
def saved_scenario_pdf(
    request: Request, scenario_id: int, db: Session = Depends(get_db)
):
    """Download the stored snapshot as a PDF. Built purely from result_json +
    the saved AI briefing — no recomputation, no Groq call. Audited."""
    user = require_role(request, ["admin"])
    if isinstance(user, RedirectResponse):
        return user

    row = db.query(SavedScenario).filter(SavedScenario.id == scenario_id).first()
    if row is None:
        return RedirectResponse(
            url="/admin/simulator/setup?"
                + urlencode({"error": "Saved scenario not found."}),
            status_code=303,
        )

    result = json.loads(row.result_json)
    saved_by = row.created_by_user.username if row.created_by_user else "—"
    created_at_str = _to_pht(row.created_at)
    pdf_bytes = pdf_export.build_scenario_pdf(row, result, created_at_str, saved_by)

    slug = re.sub(r"[^a-z0-9]+", "-", (row.barangay_name or "scenario").lower()).strip("-")
    date_slug = row.created_at.strftime("%Y%m%d") if row.created_at else "snapshot"
    filename = f"riskonek-scenario-{slug or 'scenario'}-{date_slug}.pdf"

    log_action(
        db, user["id"], "exported", "saved_scenarios", row.id,
        f"Exported PDF for saved scenario '{row.name}'",
    )

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/scenarios/{scenario_id}/delete")
def saved_scenario_delete(
    request: Request, scenario_id: int, db: Session = Depends(get_db)
):
    """Delete a saved scenario (admin only), audited."""
    user = require_role(request, ["admin"])
    if isinstance(user, RedirectResponse):
        return user

    row = db.query(SavedScenario).filter(SavedScenario.id == scenario_id).first()
    if row is None:
        return RedirectResponse(
            url="/admin/simulator/setup?"
                + urlencode({"error": "Saved scenario not found."}),
            status_code=303,
        )

    name = row.name
    db.delete(row)
    db.commit()

    log_action(
        db, user["id"], "deleted", "saved_scenarios", scenario_id,
        f"Deleted saved scenario '{name}'",
    )

    return RedirectResponse(
        url="/admin/simulator/setup?"
            + urlencode({"success": "Saved scenario deleted."}),
        status_code=303,
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
