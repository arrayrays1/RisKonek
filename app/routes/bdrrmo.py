from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from datetime import datetime, date, timezone, timedelta
from urllib.parse import quote_plus
from app.database import get_db
from app.models import (
    Barangay, Incident, Facility, Population, log_action,
    DisasterType, Severity, FacilityType, FacilityStatus,
)
from app.auth import require_role, require_barangay_access
# Reuse Week 4 barangay-profile helpers so the BDRRMO profile renders the
# exact same population / incident / facility / planning-priority data.
from app.routes.admin import barangay_profile_context, _vulnerable_percent

router = APIRouter(prefix="/bdrrmo")
templates = Jinja2Templates(directory="app/templates")

# Display UTC timestamps in Philippine Standard Time (UTC+8), matching the
# admin and CFAU portals.
_PHT = timezone(timedelta(hours=8))


def _to_pht(dt):
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_PHT).strftime('%B %d, %Y at %I:%M %p')


templates.env.filters['pht'] = _to_pht


def _resolve_scope(request: Request, db: Session):
    """Resolve the BDRRMO user and their assigned barangay, enforcing
    barangay scoping (TR-BDR-10) via the shared require_barangay_access.

    Returns (user, barangay):
      - (RedirectResponse, None) when not authorised — caller returns it.
      - (user, None) when the account has no barangay assigned yet.
      - (user, Barangay) on success.
    """
    user = require_role(request, ["bdrrmo"])
    if isinstance(user, RedirectResponse):
        return user, None

    barangay_id = user.get("barangay_id")
    if not barangay_id:
        return user, None

    # A BDRRMO user may only ever touch their own barangay's data.
    access = require_barangay_access(request, barangay_id)
    if isinstance(access, RedirectResponse):
        return access, None

    barangay = db.query(Barangay).filter(Barangay.id == barangay_id).first()
    return user, barangay


# ══════════════════════════════════════════════════════════════════════
# LANDING — the BDRRMO portal opens on the Barangay Profile (which doubles
# as the dashboard). The old /dashboard URL redirects here for any stale
# links/bookmarks.
# ══════════════════════════════════════════════════════════════════════

@router.get("/dashboard")
def dashboard(request: Request):
    return RedirectResponse(url="/bdrrmo/profile", status_code=302)


# ══════════════════════════════════════════════════════════════════════
# BARANGAY PROFILE (reuses the Week 4 profile context) — portal landing
# ══════════════════════════════════════════════════════════════════════

@router.get("/profile", response_class=HTMLResponse)
def profile(request: Request, db: Session = Depends(get_db)):
    user, barangay = _resolve_scope(request, db)
    if isinstance(user, RedirectResponse):
        return user

    if not barangay:
        return templates.TemplateResponse(
            request=request,
            name="bdrrmo/profile.html",
            context={"user": user, "active_nav": "bdrrmo_profile", "barangay": None},
        )

    context = barangay_profile_context(db, barangay)
    context["user"] = user
    context["active_nav"] = "bdrrmo_profile"
    return templates.TemplateResponse(
        request=request, name="bdrrmo/profile.html", context=context
    )


# ══════════════════════════════════════════════════════════════════════
# INCIDENT HISTORY (barangay-scoped, read-only)
# ══════════════════════════════════════════════════════════════════════

@router.get("/incidents", response_class=HTMLResponse)
def incidents(request: Request, db: Session = Depends(get_db)):
    user, barangay = _resolve_scope(request, db)
    if isinstance(user, RedirectResponse):
        return user

    rows = []
    if barangay:
        rows = db.query(Incident).filter(
            Incident.barangay_id == barangay.id
        ).order_by(Incident.date_occurred.desc()).all()

    return templates.TemplateResponse(
        request=request,
        name="bdrrmo/incidents.html",
        context={
            "user": user,
            "active_nav": "bdrrmo_incidents",
            "barangay": barangay,
            "incidents": rows,
            "disaster_types": [d.value for d in DisasterType],
            "severities": [s.value for s in Severity],
            "success": request.query_params.get("success"),
            "error": request.query_params.get("error"),
        },
    )


@router.post("/incidents")
def incident_create(
    request: Request,
    db: Session = Depends(get_db),
    disaster_type: str = Form(...),
    date_occurred: str = Form(...),
    severity: str = Form("moderate"),
    affected_families: int = Form(0),
    casualties: int = Form(0),
    description: str = Form(""),
):
    """TR-BDR-01/02 — the BDRRMO Chairperson submits a disaster/risk report,
    stored as an Incident scoped to (associated with) their own barangay."""
    user, barangay = _resolve_scope(request, db)
    if isinstance(user, RedirectResponse):
        return user
    if not barangay:
        return RedirectResponse(
            url="/bdrrmo/incidents?error=No+barangay+is+assigned+to+your+account",
            status_code=302,
        )

    # Validate the enum / date inputs; bounce back with a message on bad data.
    if disaster_type not in {d.value for d in DisasterType}:
        return RedirectResponse(
            url="/bdrrmo/incidents?error=Invalid+disaster+type", status_code=302
        )
    if severity not in {s.value for s in Severity}:
        severity = Severity.moderate.value
    try:
        occurred = date.fromisoformat(date_occurred)
    except (ValueError, TypeError):
        return RedirectResponse(
            url="/bdrrmo/incidents?error=Invalid+date", status_code=302
        )

    incident = Incident(
        barangay_id=barangay.id,                 # TR-BDR-02 association
        reported_by=user["id"],
        disaster_type=DisasterType(disaster_type),
        date_occurred=occurred,
        severity=Severity(severity),
        affected_families=max(0, affected_families or 0),
        casualties=max(0, casualties or 0),
        description=(description or "").strip() or None,
        source="BDRRMO submission",
    )
    db.add(incident)
    db.commit()
    db.refresh(incident)

    log_action(
        db, user["id"], "created", "incidents", incident.id,
        f"BDRRMO submitted a {incident.disaster_type.value} report for "
        f"{barangay.name} (occurred {occurred.isoformat()}, "
        f"severity: {incident.severity.value})",
    )

    return RedirectResponse(
        url="/bdrrmo/incidents?success=Incident+report+submitted", status_code=302
    )


# ══════════════════════════════════════════════════════════════════════
# CRITICAL FACILITIES (barangay-scoped, add / update / manage)
# ══════════════════════════════════════════════════════════════════════

# Coordinate proximity (in degrees) under which two facilities are treated
# as the same point. ~0.0002° ≈ 22 m — tight enough to catch a re-pin of
# the same building, loose enough not to flag genuinely separate ones.
_DUP_COORD_TOLERANCE = 0.0002


@router.get("/facilities", response_class=HTMLResponse)
def facilities(request: Request, db: Session = Depends(get_db)):
    user, barangay = _resolve_scope(request, db)
    if isinstance(user, RedirectResponse):
        return user

    show_archived = request.query_params.get("archived") == "1"
    rows = []
    archived_count = 0
    if barangay:
        archived_count = db.query(Facility).filter(
            Facility.barangay_id == barangay.id,
            Facility.is_archived == True,
        ).count()
        rows = db.query(Facility).filter(
            Facility.barangay_id == barangay.id,
            Facility.is_archived == show_archived,
        ).order_by(Facility.facility_type, Facility.name).all()

    return templates.TemplateResponse(
        request=request,
        name="bdrrmo/facilities.html",
        context={
            "user": user,
            "active_nav": "bdrrmo_facilities",
            "barangay": barangay,
            "facilities": rows,
            "facility_types": [t.value for t in FacilityType],
            "facility_statuses": [s.value for s in FacilityStatus],
            "show_archived": show_archived,
            "archived_count": archived_count,
            "edit_id": request.query_params.get("edit"),
            "success": request.query_params.get("success"),
            "error": request.query_params.get("error"),
        },
    )


def _parse_coord(value, lo, hi):
    """Coerce a coordinate string to float within [lo, hi]; None if invalid."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if lo <= f <= hi else None


def _find_duplicate(db, barangay_id, name, lat, lon, exclude_id=None):
    """Return an existing facility in the same barangay that looks like a
    duplicate of the given one, or None.

    A duplicate is either the same (case-insensitive, trimmed) name, or a
    point within _DUP_COORD_TOLERANCE degrees of the same coordinates.
    Archived facilities are ignored so a restore isn't blocked. The
    facility being edited (exclude_id) is excluded from the comparison.
    """
    q = db.query(Facility).filter(
        Facility.barangay_id == barangay_id,
        Facility.is_archived == False,
    )
    if exclude_id is not None:
        q = q.filter(Facility.id != exclude_id)

    norm = (name or "").strip().lower()
    for f in q.all():
        if norm and (f.name or "").strip().lower() == norm:
            return f
        if (abs((f.latitude or 0) - lat) <= _DUP_COORD_TOLERANCE
                and abs((f.longitude or 0) - lon) <= _DUP_COORD_TOLERANCE):
            return f
    return None


def _sync_active(facility):
    """Keep the legacy is_active flag in step with the operational tag so
    the admin map/profile (which still read is_active) stay correct."""
    facility.is_active = facility.operational_status == FacilityStatus.available


@router.post("/facilities")
def facility_create(
    request: Request,
    db: Session = Depends(get_db),
    name: str = Form(...),
    facility_type: str = Form(...),
    latitude: str = Form(...),
    longitude: str = Form(...),
    address: str = Form(""),
    operational_status: str = Form("available"),
):
    """TR-BDR-03/04 — add a critical facility point within own barangay,
    rejecting duplicates of an existing facility."""
    user, barangay = _resolve_scope(request, db)
    if isinstance(user, RedirectResponse):
        return user
    if not barangay:
        return RedirectResponse(
            url="/bdrrmo/facilities?error=No+barangay+is+assigned+to+your+account",
            status_code=302,
        )
    if facility_type not in {t.value for t in FacilityType}:
        return RedirectResponse(
            url="/bdrrmo/facilities?error=Invalid+facility+type", status_code=302
        )
    if operational_status not in {s.value for s in FacilityStatus}:
        operational_status = FacilityStatus.available.value
    lat = _parse_coord(latitude, -90, 90)
    lon = _parse_coord(longitude, -180, 180)
    if lat is None or lon is None:
        return RedirectResponse(
            url="/bdrrmo/facilities?error=Invalid+coordinates", status_code=302
        )

    dup = _find_duplicate(db, barangay.id, name, lat, lon)
    if dup:
        return RedirectResponse(
            url="/bdrrmo/facilities?error=" + quote_plus(
                f"A similar facility already exists: '{dup.name}'. "
                "Edit that record instead of adding a duplicate."
            ),
            status_code=302,
        )

    facility = Facility(
        barangay_id=barangay.id,                 # scoped to own barangay
        name=name.strip(),
        facility_type=FacilityType(facility_type),
        latitude=lat,
        longitude=lon,
        address=(address or "").strip() or None,
        operational_status=FacilityStatus(operational_status),
        is_archived=False,
    )
    _sync_active(facility)
    db.add(facility)
    db.commit()
    db.refresh(facility)

    log_action(
        db, user["id"], "created", "facilities", facility.id,
        f"BDRRMO added critical facility '{facility.name}' "
        f"({facility.facility_type.value}, "
        f"status={facility.operational_status.value}) in {barangay.name}",
    )
    return RedirectResponse(
        url="/bdrrmo/facilities?success=Facility+added", status_code=302
    )


def _own_facility(db, barangay, facility_id):
    """Fetch a facility only if it belongs to the user's barangay (TR-BDR-10)."""
    return db.query(Facility).filter(
        Facility.id == facility_id,
        Facility.barangay_id == barangay.id,
    ).first()


@router.post("/facilities/{facility_id}/edit")
def facility_edit(
    facility_id: int,
    request: Request,
    db: Session = Depends(get_db),
    name: str = Form(...),
    facility_type: str = Form(...),
    latitude: str = Form(...),
    longitude: str = Form(...),
    address: str = Form(""),
    operational_status: str = Form("available"),
):
    """TR-BDR-03/04 — update an existing facility within own barangay."""
    user, barangay = _resolve_scope(request, db)
    if isinstance(user, RedirectResponse):
        return user
    if not barangay:
        return RedirectResponse(
            url="/bdrrmo/facilities?error=No+barangay+is+assigned+to+your+account",
            status_code=302,
        )
    facility = _own_facility(db, barangay, facility_id)
    if not facility:
        return RedirectResponse(
            url="/bdrrmo/facilities?error=Facility+not+found", status_code=302
        )
    if facility_type not in {t.value for t in FacilityType}:
        return RedirectResponse(
            url="/bdrrmo/facilities?error=Invalid+facility+type", status_code=302
        )
    if operational_status not in {s.value for s in FacilityStatus}:
        operational_status = FacilityStatus.available.value
    lat = _parse_coord(latitude, -90, 90)
    lon = _parse_coord(longitude, -180, 180)
    if lat is None or lon is None:
        return RedirectResponse(
            url="/bdrrmo/facilities?error=Invalid+coordinates", status_code=302
        )

    dup = _find_duplicate(db, barangay.id, name, lat, lon, exclude_id=facility.id)
    if dup:
        return RedirectResponse(
            url=f"/bdrrmo/facilities?edit={facility.id}&error=" + quote_plus(
                f"Another facility already matches this name/location: '{dup.name}'."
            ),
            status_code=302,
        )

    facility.name = name.strip()
    facility.facility_type = FacilityType(facility_type)
    facility.latitude = lat
    facility.longitude = lon
    facility.address = (address or "").strip() or None
    facility.operational_status = FacilityStatus(operational_status)
    _sync_active(facility)
    db.commit()

    log_action(
        db, user["id"], "updated", "facilities", facility.id,
        f"BDRRMO updated critical facility '{facility.name}' "
        f"(status={facility.operational_status.value}) in {barangay.name}",
    )
    return RedirectResponse(
        url="/bdrrmo/facilities?success=Facility+updated", status_code=302
    )


@router.post("/facilities/{facility_id}/status")
def facility_set_status(
    facility_id: int,
    request: Request,
    db: Session = Depends(get_db),
    operational_status: str = Form(...),
):
    """TR-BDR-03 — quick-set a facility's operational status tag
    (available / under_maintenance / unavailable)."""
    user, barangay = _resolve_scope(request, db)
    if isinstance(user, RedirectResponse):
        return user
    if not barangay:
        return RedirectResponse(
            url="/bdrrmo/facilities?error=No+barangay+is+assigned+to+your+account",
            status_code=302,
        )
    facility = _own_facility(db, barangay, facility_id)
    if not facility:
        return RedirectResponse(
            url="/bdrrmo/facilities?error=Facility+not+found", status_code=302
        )
    if operational_status not in {s.value for s in FacilityStatus}:
        return RedirectResponse(
            url="/bdrrmo/facilities?error=Invalid+status", status_code=302
        )

    facility.operational_status = FacilityStatus(operational_status)
    _sync_active(facility)
    db.commit()
    log_action(
        db, user["id"], "updated", "facilities", facility.id,
        f"BDRRMO set facility '{facility.name}' status to "
        f"{facility.operational_status.value}",
    )
    return RedirectResponse(
        url="/bdrrmo/facilities?success=Facility+status+updated", status_code=302
    )


@router.post("/facilities/{facility_id}/archive")
def facility_archive(
    facility_id: int, request: Request, db: Session = Depends(get_db)
):
    """TR-BDR-03 — archive (soft delete) or restore a facility. Archiving
    keeps the record and its history rather than destroying it."""
    user, barangay = _resolve_scope(request, db)
    if isinstance(user, RedirectResponse):
        return user
    if not barangay:
        return RedirectResponse(
            url="/bdrrmo/facilities?error=No+barangay+is+assigned+to+your+account",
            status_code=302,
        )
    facility = _own_facility(db, barangay, facility_id)
    if not facility:
        return RedirectResponse(
            url="/bdrrmo/facilities?error=Facility+not+found", status_code=302
        )

    facility.is_archived = not bool(facility.is_archived)
    db.commit()
    verb = "archived" if facility.is_archived else "restored"
    log_action(
        db, user["id"], verb, "facilities", facility.id,
        f"BDRRMO {verb} critical facility '{facility.name}' in {barangay.name}",
    )
    dest = "/bdrrmo/facilities?archived=1" if facility.is_archived else "/bdrrmo/facilities"
    sep = "&" if "?" in dest else "?"
    return RedirectResponse(
        url=f"{dest}{sep}success=Facility+{verb}", status_code=302
    )


# ══════════════════════════════════════════════════════════════════════
# POPULATION RECORDS (barangay-scoped, append-only logging)
# Each submission creates a NEW Population row — no edit/delete. History
# is kept intact. recorded_by = current user, barangay_id = own barangay.
# ══════════════════════════════════════════════════════════════════════

@router.get("/population", response_class=HTMLResponse)
def population(request: Request, db: Session = Depends(get_db)):
    user, barangay = _resolve_scope(request, db)
    if isinstance(user, RedirectResponse):
        return user

    history = []
    latest = None
    if barangay:
        history = db.query(Population).filter(
            Population.barangay_id == barangay.id
        ).order_by(Population.recorded_at.desc()).all()
        latest = history[0] if history else None

    return templates.TemplateResponse(
        request=request,
        name="bdrrmo/population.html",
        context={
            "user": user,
            "active_nav": "bdrrmo_population",
            "barangay": barangay,
            "latest": latest,
            "history": history,
            "vulnerable_pct": _vulnerable_percent(latest),
            "success": request.query_params.get("success"),
            "error": request.query_params.get("error"),
        },
    )


@router.post("/population")
def population_create(
    request: Request,
    db: Session = Depends(get_db),
    total_population: int = Form(0),
    total_households: int = Form(0),
    pwd_count: int = Form(0),
    elderly_count: int = Form(0),
    children_count: int = Form(0),
):
    user, barangay = _resolve_scope(request, db)
    if isinstance(user, RedirectResponse):
        return user
    if not barangay:
        return RedirectResponse(
            url="/bdrrmo/population?error=No+barangay+is+assigned+to+your+account",
            status_code=302,
        )

    # Append-only: a new snapshot row, scoped to the user's own barangay.
    record = Population(
        barangay_id=barangay.id,
        recorded_by=user["id"],
        total_population=max(0, total_population or 0),
        total_households=max(0, total_households or 0),
        pwd_count=max(0, pwd_count or 0),
        elderly_count=max(0, elderly_count or 0),
        children_count=max(0, children_count or 0),
    )
    db.add(record)
    db.commit()
    db.refresh(record)

    log_action(
        db, user["id"], "created", "populations", record.id,
        f"BDRRMO recorded a population snapshot for {barangay.name} "
        f"(total population: {record.total_population:,}, "
        f"households: {record.total_households:,})",
    )

    return RedirectResponse(
        url="/bdrrmo/population?success=Population+record+saved", status_code=302
    )


# ══════════════════════════════════════════════════════════════════════
# BARANGAY CONTACT DETAILS (TR-BDR-07/08)
# The BDRRMO Chairperson edits officials (captain/chairperson) and the
# free-text emergency-responder list on their own Barangay record. The
# same record is read by the admin barangay profile, satisfying TR-BDR-08.
# ══════════════════════════════════════════════════════════════════════

@router.get("/contacts", response_class=HTMLResponse)
def contacts(request: Request, db: Session = Depends(get_db)):
    user, barangay = _resolve_scope(request, db)
    if isinstance(user, RedirectResponse):
        return user

    return templates.TemplateResponse(
        request=request,
        name="bdrrmo/contacts.html",
        context={
            "user": user,
            "active_nav": "bdrrmo_contacts",
            "barangay": barangay,
            "success": request.query_params.get("success"),
            "error": request.query_params.get("error"),
        },
    )


@router.post("/contacts")
def contacts_update(
    request: Request,
    db: Session = Depends(get_db),
    captain_name: str = Form(""),
    captain_contact: str = Form(""),
    chairperson_name: str = Form(""),
    chairperson_contact: str = Form(""),
    emergency_contacts: str = Form(""),
):
    user, barangay = _resolve_scope(request, db)
    if isinstance(user, RedirectResponse):
        return user
    if not barangay:
        return RedirectResponse(
            url="/bdrrmo/contacts?error=No+barangay+is+assigned+to+your+account",
            status_code=302,
        )

    barangay.captain_name = captain_name.strip() or None
    barangay.captain_contact = captain_contact.strip() or None
    barangay.chairperson_name = chairperson_name.strip() or None
    barangay.chairperson_contact = chairperson_contact.strip() or None
    barangay.emergency_contacts = emergency_contacts.strip() or None
    db.commit()

    log_action(
        db, user["id"], "updated", "barangays", barangay.id,
        f"BDRRMO updated contact details for {barangay.name}",
    )
    return RedirectResponse(
        url="/bdrrmo/contacts?success=Contact+details+updated", status_code=302
    )
