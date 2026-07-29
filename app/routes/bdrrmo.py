from fastapi import APIRouter, Request, Depends, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from datetime import datetime, date, timezone, timedelta
from urllib.parse import quote_plus
from typing import Optional
import os
from app.database import get_db
from app.models import (
    Barangay, Incident, Facility, Population, log_action,
    DisasterType, Severity, FacilityType, FacilityStatus,
    UploadedReport, ReportStatus, FileType, LifecycleStatus, UploadEvent,
    add_upload_history, BarangayEquipment, EquipmentType,
    IncidentReport, ServiceabilityStatus,
)
from app.auth import require_role, require_barangay_access
from app.utils.pagination import (
    paginate, parse_per_page, parse_page, build_base_query,
)
# Reuse Week 4 barangay-profile helpers so the BDRRMO profile renders the
# exact same population / incident / facility / planning-priority data.
from app.routes.admin import barangay_profile_context, _vulnerable_percent
# Reuse the Week 6 / 8.1 ETL building blocks rather than rebuilding them —
# the same upload validation, extraction, strict matcher and resolve-or-create
# gateway used by the Admin medallion and CFAU post-incident paths.
from app.routes.uploads import (
    _safe_filename, _ext_of, ALLOWED_EXTS, UPLOAD_SUBDIR,
    save_validated_upload, find_matching_incident, resolve_or_create_incident,
)
from app.etl.extract_pdf import extract_pdf
from app.etl.extract_excel import extract_excel, extract_csv
from app.etl.structure import (
    structure_text, structure_rows,
    structure_incident_report_rows, structure_incident_report_text,
    empty_incident_report_fields,
)
from app.etl.ai_pipeline import summarize as ai_summarize, is_available as ai_available
from app.services.geocoding import (
    search_locations, reverse_geocode_san_pedro, nearby_landmarks,
    is_within_san_pedro, get_san_pedro_bounds, get_san_pedro_boundary,
)
from app.services.contact_directory import build_directory_context
from app.services.facility_details import (
    parse_coord as _parse_coord,
    find_duplicate_facility as _find_duplicate,
    validate_name as _validate_facility_name,
    validate_address as _validate_facility_address,
    validate_facility_details,
    FACILITY_CLASSIFICATIONS,
    EO_MOA_MOU_STATUSES,
)

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
# FACILITY LOCATION PICKER — search / reverse-geocode / nearby landmarks
# (server-side proxy to Nominatim/Overpass; see app/services/geocoding.py)
# ══════════════════════════════════════════════════════════════════════

@router.get("/api/location-search")
def api_location_search(request: Request, db: Session = Depends(get_db), q: Optional[str] = None):
    user, _ = _resolve_scope(request, db)
    if isinstance(user, RedirectResponse):
        return user
    return search_locations(db, q or "")


@router.get("/api/reverse-geocode")
def api_reverse_geocode(
    request: Request, db: Session = Depends(get_db),
    lat: Optional[str] = None, lng: Optional[str] = None,
):
    user, _ = _resolve_scope(request, db)
    if isinstance(user, RedirectResponse):
        return user
    try:
        lat_f, lng_f = float(lat), float(lng)
    except (TypeError, ValueError):
        return {"available": True, "display_name": None}
    return reverse_geocode_san_pedro(lat_f, lng_f)


@router.get("/api/nearby-landmarks")
def api_nearby_landmarks(
    request: Request, db: Session = Depends(get_db),
    lat: Optional[str] = None, lng: Optional[str] = None, radius: Optional[str] = None,
):
    user, _ = _resolve_scope(request, db)
    if isinstance(user, RedirectResponse):
        return user
    try:
        lat_f, lng_f = float(lat), float(lng)
    except (TypeError, ValueError):
        return {"available": True, "results": []}
    try:
        radius_i = int(radius) if radius else 400
    except (TypeError, ValueError):
        radius_i = 400
    return nearby_landmarks(lat_f, lng_f, radius_i)


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
def incidents(
    request: Request,
    db: Session = Depends(get_db),
    q: Optional[str] = None,
    disaster_type: Optional[str] = None,
    severity: Optional[str] = None,
    page: Optional[str] = None,
    per_page: Optional[str] = None,
):
    user, barangay = _resolve_scope(request, db)
    if isinstance(user, RedirectResponse):
        return user

    rows = []
    if barangay:
        query = db.query(Incident).filter(Incident.barangay_id == barangay.id)
        if q:
            query = query.filter(Incident.description.ilike(f"%{q.strip()}%"))
        if disaster_type and disaster_type in {d.value for d in DisasterType}:
            query = query.filter(Incident.disaster_type == DisasterType(disaster_type))
        if severity and severity in {s.value for s in Severity}:
            query = query.filter(Incident.severity == Severity(severity))
        rows = query.order_by(Incident.date_occurred.desc()).all()

    page_obj = paginate(rows, parse_page(page), parse_per_page(per_page))
    base_query = build_base_query({
        "q": q or "", "disaster_type": disaster_type or "", "severity": severity or "",
    })

    return templates.TemplateResponse(
        request=request,
        name="bdrrmo/incidents.html",
        context={
            "user": user,
            "active_nav": "bdrrmo_incidents",
            "barangay": barangay,
            "incidents": page_obj.items,
            "page_obj": page_obj,
            "base_query": base_query,
            "disaster_types": [d.value for d in DisasterType],
            "severities": [s.value for s in Severity],
            "f_q": q or "",
            "f_disaster_type": disaster_type or "",
            "f_severity": severity or "",
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

    # Route through the shared resolve-or-create gateway so manual entries
    # participate in the same strict duplicate detection (barangay +
    # disaster_type + date) as the upload paths. A matching Incident is reused
    # UNCHANGED — never overwritten. Barangay is always the user's own.
    try:
        incident, created = resolve_or_create_incident(
            db, user["id"],
            core={
                "barangay": barangay.name,
                "disaster_type": disaster_type,
                "date_occurred": occurred.isoformat(),
                "severity": severity,
                "affected_families": max(0, affected_families or 0),
                "casualties": max(0, casualties or 0),
                "description": description,
            },
            source="bdrrmo_manual",
        )
    except ValueError as e:
        return RedirectResponse(
            url="/bdrrmo/incidents?error=" + quote_plus(str(e)), status_code=302
        )
    db.commit()

    if created:
        log_action(
            db, user["id"], "created", "incidents", incident.id,
            f"BDRRMO submitted a {incident.disaster_type.value} report for "
            f"{barangay.name} (occurred {occurred.isoformat()}, "
            f"severity: {incident.severity.value})",
        )
        msg = "Incident+report+submitted"
    else:
        # Duplicate found — reused, no new row created.
        log_action(
            db, user["id"], "matched", "incidents", incident.id,
            f"BDRRMO manual entry matched existing incident #{incident.id} for "
            f"{barangay.name} ({incident.disaster_type.value}, "
            f"{occurred.isoformat()}) — reused, no duplicate created.",
        )
        msg = quote_plus(
            f"This incident is already on record (#{incident.id}) — it was "
            "reused and no duplicate was created."
        )

    return RedirectResponse(
        url=f"/bdrrmo/incidents?success={msg}", status_code=302
    )


# ══════════════════════════════════════════════════════════════════════
# INCIDENT UPLOAD (Week 8.1) — reuses the shared ETL pipeline (Bronze
# storage + extraction + AI summary + UploadedReport lifecycle + history +
# the resolve-or-create gateway). Unlike the Admin medallion path, a BDRRMO
# upload produces ONE Incident scoped to the user's OWN barangay: the
# barangay is locked, never taken from the document. These literal "upload"
# routes are declared before the facilities block; there is no /incidents/{id}
# route so segment ordering is unambiguous.
# ══════════════════════════════════════════════════════════════════════

UPLOAD_KIND_BDRRMO = "bdrrmo_incident"


def _empty_core_fields():
    return {
        "barangay": "", "disaster_type": "", "date_occurred": "",
        "severity": "", "affected_families": 0, "casualties": 0,
        "description": "",
    }


def _is_bdrrmo_incident_upload(report) -> bool:
    data = report.extracted_data or {}
    return data.get("report_kind") == UPLOAD_KIND_BDRRMO


def _get_owned_upload(db, report_id, user):
    """A BDRRMO incident upload the current user may act on (own only)."""
    r = db.query(UploadedReport).filter(UploadedReport.id == report_id).first()
    if not r or not _is_bdrrmo_incident_upload(r):
        return None
    if r.uploaded_by != user["id"]:
        return None
    return r


@router.get("/incidents/upload", response_class=HTMLResponse)
def incident_upload_form(request: Request, db: Session = Depends(get_db)):
    user, barangay = _resolve_scope(request, db)
    if isinstance(user, RedirectResponse):
        return user
    if not barangay:
        return RedirectResponse(
            url="/bdrrmo/incidents?error=No+barangay+is+assigned+to+your+account",
            status_code=302,
        )
    return templates.TemplateResponse(
        request=request,
        name="bdrrmo/incident_upload_form.html",
        context={
            "user": user,
            "active_nav": "bdrrmo_incidents",
            "barangay": barangay,
            "error": request.query_params.get("error"),
            "ai_available": ai_available(),
        },
    )


@router.post("/incidents/upload")
async def incident_upload_submit(
    request: Request,
    db: Session = Depends(get_db),
    file: UploadFile = File(...),
):
    user, barangay = _resolve_scope(request, db)
    if isinstance(user, RedirectResponse):
        return user
    if not barangay:
        return RedirectResponse(
            url="/bdrrmo/incidents?error=No+barangay+is+assigned+to+your+account",
            status_code=302,
        )

    original_name = file.filename or "report"
    ext = _ext_of(original_name)
    if ext not in ALLOWED_EXTS:
        return RedirectResponse(
            url="/bdrrmo/incidents/upload?error=Unsupported+file+type.+Allowed:+PDF,+XLSX,+XLS,+CSV.",
            status_code=302,
        )

    os.makedirs(UPLOAD_SUBDIR, exist_ok=True)
    stored_name = _safe_filename(original_name)
    stored_path = os.path.join(UPLOAD_SUBDIR, stored_name)

    ok, err = await save_validated_upload(file, ext, stored_path)
    if not ok:
        return RedirectResponse(
            url="/bdrrmo/incidents/upload?error=" + quote_plus(err),
            status_code=302,
        )

    file_type_enum = ALLOWED_EXTS[ext]
    report = UploadedReport(
        uploaded_by=user["id"],
        file_name=original_name,
        file_path=stored_path.replace("\\", "/"),
        file_type=file_type_enum,
        status=ReportStatus.processing,
        lifecycle_status=LifecycleStatus.draft,
    )
    db.add(report)
    db.commit()
    db.refresh(report)

    add_upload_history(
        db, report_id=report.id, user_id=user["id"],
        event_type=UploadEvent.created,
        new_value=f"BDRRMO uploaded incident document '{original_name}' "
                  f"({file_type_enum.value.upper()}) for {barangay.name}",
    )
    db.commit()

    # ── Silver: extract raw text/rows + pre-fill the core incident fields ──
    # Tagged as a BDRRMO incident upload in JSON (no schema change). The
    # barangay is FORCED to the user's own barangay regardless of what the
    # document says — uploads can never create incidents outside scope.
    extracted = {
        "report_kind": UPLOAD_KIND_BDRRMO,
        "raw_text": "", "rows": [], "columns": [], "error": None,
        "core_fields": _empty_core_fields(),
        "matched_incident_id": None,
    }
    try:
        if file_type_enum == FileType.pdf:
            out = extract_pdf(stored_path)
            extracted["raw_text"] = out.get("text", "")
            known_barangays = [b.name for b in db.query(Barangay).all()]
            core_row = structure_text(extracted["raw_text"], known_barangays)
            extracted["core_fields"] = {
                k: core_row.get(k, "") for k in _empty_core_fields()
            }
        elif file_type_enum in (FileType.excel, FileType.csv):
            out = extract_excel(stored_path) if file_type_enum == FileType.excel \
                else extract_csv(stored_path)
            extracted["columns"] = out["columns"]
            extracted["rows"] = out["rows"]
            core_rows = structure_rows(out["rows"])
            if core_rows:
                extracted["core_fields"] = {
                    k: core_rows[0].get(k, "") for k in _empty_core_fields()
                }
        report.status = ReportStatus.reviewed
    except Exception as e:
        print(f"[bdrrmo] Extraction failed for report '{original_name}': {e}")
        extracted["error"] = "Extraction failed. The file could not be read or was malformed."
        report.status = ReportStatus.failed

    # Lock the barangay to the user's own — the strict match and any created
    # incident must stay within scope. Severity is not auto-detected; the
    # reviewer sets it before saving.
    extracted["core_fields"]["barangay"] = barangay.name
    extracted["core_fields"].setdefault("severity", "")

    # Optional AI summary — never blocks the flow.
    ai_text = None
    if extracted.get("raw_text"):
        ai_text = ai_summarize(extracted["raw_text"])
    elif extracted.get("rows"):
        preview = "\n".join(
            ", ".join(f"{k}={v}" for k, v in row.items())
            for row in extracted["rows"][:25]
        )
        ai_text = ai_summarize(preview) if preview else None
    if ai_text:
        report.ai_summary = ai_text

    # Strict incident match within the locked barangay.
    core = extracted["core_fields"]
    matched = find_matching_incident(
        db, barangay.name, core.get("disaster_type"), core.get("date_occurred")
    )
    extracted["matched_incident_id"] = matched.id if matched else None

    report.extracted_data = extracted
    db.commit()

    note = (f"Extraction failed: {extracted['error']}" if extracted.get("error")
            else "Extraction completed — core incident details ready for review.")
    add_upload_history(
        db, report_id=report.id, user_id=user["id"],
        event_type=UploadEvent.extracted, new_value=note,
    )
    db.commit()

    return RedirectResponse(
        url=f"/bdrrmo/incidents/upload/{report.id}/review", status_code=302
    )


@router.get("/incidents/upload/{report_id}/review", response_class=HTMLResponse)
def incident_upload_review(report_id: int, request: Request, db: Session = Depends(get_db)):
    user, barangay = _resolve_scope(request, db)
    if isinstance(user, RedirectResponse):
        return user
    report = _get_owned_upload(db, report_id, user)
    if not report:
        return RedirectResponse(url="/bdrrmo/incidents", status_code=302)

    data = report.extracted_data or {}
    # Already converted — nothing more to review.
    if data.get("linked_incident_id"):
        return RedirectResponse(
            url="/bdrrmo/incidents?success=Incident+already+saved+from+this+upload",
            status_code=302,
        )

    # Strict-match result: only surface if the matched incident still exists.
    matched_id = data.get("matched_incident_id")
    matched_incident = None
    if matched_id:
        matched_incident = db.query(Incident).filter(Incident.id == matched_id).first()

    core = data.get("core_fields") or _empty_core_fields()
    # Barangay is always the user's own — never editable on this screen.
    core["barangay"] = barangay.name if barangay else core.get("barangay", "")

    # Optional inventory selector: the barangay's own active equipment/vehicles.
    # Free-text entry is always allowed too, so this list is a convenience only.
    equipment_options = []
    if barangay:
        equipment_options = db.query(BarangayEquipment).filter(
            BarangayEquipment.barangay_id == barangay.id,
            BarangayEquipment.is_archived == False,
        ).order_by(BarangayEquipment.equipment_type, BarangayEquipment.name).all()

    return templates.TemplateResponse(
        request=request,
        name="bdrrmo/incident_upload_review.html",
        context={
            "user": user,
            "active_nav": "bdrrmo_incidents",
            "barangay": barangay,
            "report": report,
            "equipment_options": equipment_options,
            "core": core,
            "matched_incident": matched_incident,
            "disaster_types": [dt.value for dt in DisasterType],
            "severities": [s.value for s in Severity],
            "ai_summary": report.ai_summary,
            "ai_available": ai_available(),
            "raw_text_preview": (data.get("raw_text") or "")[:4000],
            "rows_preview": (data.get("rows") or [])[:10],
            "columns_preview": data.get("columns") or [],
            "extraction_error": data.get("error"),
            "error": request.query_params.get("error"),
        },
    )


@router.get("/incidents/upload/{report_id}/file")
def incident_upload_file(report_id: int, request: Request, db: Session = Depends(get_db)):
    user, barangay = _resolve_scope(request, db)
    if isinstance(user, RedirectResponse):
        return user
    report = _get_owned_upload(db, report_id, user)
    if not report or not report.file_path or not os.path.exists(report.file_path):
        return RedirectResponse(url="/bdrrmo/incidents", status_code=302)
    return FileResponse(report.file_path, filename=report.file_name)


def _read_equipment_rows(form, valid_equipment):
    """Build the canonical equipment-item list from the review form's parallel
    arrays (equipment_name / equipment_quantity / equipment_id).

    Each item: {"name": str, "quantity": int>=1, "barangay_equipment_id": int|None}.

    `valid_equipment` is a {id: name} map of the uploader's OWN barangay
    equipment. An inventory id is kept only if it is in that map (scope check);
    otherwise it is discarded and the free-text name is retained. When the
    free-text name is blank but a valid id is given, the inventory item's name
    is used. Rows with no usable name are skipped.
    """
    names = form.getlist("equipment_name")
    quantities = form.getlist("equipment_quantity")
    ids = form.getlist("equipment_id")
    n = max(len(names), len(quantities), len(ids))

    items = []
    for i in range(n):
        name = (names[i].strip() if i < len(names) else "")

        # Optional inventory id — kept only if within the uploader's barangay.
        beid = None
        id_raw = (ids[i].strip() if i < len(ids) else "")
        if id_raw:
            try:
                candidate = int(id_raw)
            except ValueError:
                candidate = None
            if candidate in valid_equipment:
                beid = candidate

        # Free-text blank but a valid inventory item picked → use its name.
        if not name and beid is not None:
            name = valid_equipment[beid]
        if not name:
            continue

        q_raw = (quantities[i].strip() if i < len(quantities) else "")
        try:
            quantity = max(1, int(float(q_raw)))
        except (ValueError, TypeError):
            quantity = 1

        items.append({
            "name": name,
            "quantity": quantity,
            "barangay_equipment_id": beid,
        })
    return items


@router.post("/incidents/upload/{report_id}/submit")
async def incident_upload_convert(
    report_id: int,
    request: Request,
    db: Session = Depends(get_db),
    # Core incident fields — barangay is intentionally NOT accepted from the
    # form; it is always forced to the user's own barangay below.
    disaster_type: str = Form(""),
    date_occurred: str = Form(""),
    severity: str = Form("moderate"),
    affected_families: int = Form(0),
    casualties: int = Form(0),
    description: str = Form(""),
):
    user, barangay = _resolve_scope(request, db)
    if isinstance(user, RedirectResponse):
        return user
    if not barangay:
        return RedirectResponse(
            url="/bdrrmo/incidents?error=No+barangay+is+assigned+to+your+account",
            status_code=302,
        )
    report = _get_owned_upload(db, report_id, user)
    if not report:
        return RedirectResponse(url="/bdrrmo/incidents", status_code=302)

    data = dict(report.extracted_data or {})
    if data.get("linked_incident_id"):
        # Idempotency guard — already converted.
        return RedirectResponse(
            url="/bdrrmo/incidents?success=Incident+already+saved+from+this+upload",
            status_code=302,
        )

    def _review_error(msg: str):
        return RedirectResponse(
            url=f"/bdrrmo/incidents/upload/{report.id}/review?error={quote_plus(msg)}",
            status_code=302,
        )

    if severity not in {s.value for s in Severity}:
        severity = Severity.moderate.value

    # Resolve-or-create within the LOCKED barangay. A strict match is reused
    # unchanged; otherwise a new Incident is created (severity persisted).
    try:
        incident, created = resolve_or_create_incident(
            db, user["id"],
            core={
                "barangay": barangay.name,   # locked — never from the document
                "disaster_type": disaster_type,
                "date_occurred": date_occurred,
                "severity": severity,
                "affected_families": affected_families,
                "casualties": casualties,
                "description": description,
            },
            source=f"bdrrmo_upload:{report.id}",
        )
    except ValueError as e:
        return _review_error(str(e))

    # Contributor-supplied equipment/vehicles used during the response. Stored
    # as the canonical structured list; an inventory id is kept only if it
    # belongs to the uploader's own barangay, else discarded (free-text kept).
    valid_equipment = {
        e.id: e.name
        for e in db.query(BarangayEquipment).filter(
            BarangayEquipment.barangay_id == barangay.id
        ).all()
    }
    form = await request.form()
    equipment_used = _read_equipment_rows(form, valid_equipment)

    # Provenance — the uploader's submitted values + linkage are stored on the
    # upload (not on the canonical Incident). incident_contributions() reads
    # these to aggregate every upload that refers to the same Incident.
    data["linked_incident_id"] = incident.id
    data["incident_created"] = created
    data["source"] = f"bdrrmo_upload:{report.id}"
    data["contributed_core"] = {
        "barangay": barangay.name,
        "disaster_type": disaster_type,
        "date_occurred": date_occurred,
        "severity": severity,
        "affected_families": max(0, affected_families or 0),
        "casualties": max(0, casualties or 0),
        "description": (description or "").strip(),
        # Canonical equipment-item shape: [{name, quantity, barangay_equipment_id}]
        "equipment_used": equipment_used,
    }
    report.extracted_data = data
    # First-class contribution linkage (mirrors linked_incident_id JSON).
    report.incident_id = incident.id
    report.lifecycle_status = LifecycleStatus.confirmed
    report.status = ReportStatus.confirmed
    if report.barangay_id is None:
        report.barangay_id = barangay.id
    db.commit()

    if created:
        log_action(
            db, user["id"], "created", "incidents", incident.id,
            f"Incident auto-created from BDRRMO upload '{report.file_name}' — "
            f"{incident.disaster_type.value} in {barangay.name} "
            f"({date_occurred}) (no existing match).",
        )

    link_note = (f"new incident #{incident.id} auto-created" if created
                 else f"linked to existing incident #{incident.id} "
                      "(reused, not overwritten)")
    add_upload_history(
        db, report_id=report.id, user_id=user["id"],
        event_type=UploadEvent.confirmed,
        new_value=f"BDRRMO upload converted — {link_note}.",
    )
    db.commit()

    log_action(
        db, user["id"], "converted", "uploaded_reports", report.id,
        f"BDRRMO converted upload '{report.file_name}' to incident #{incident.id}",
    )

    msg = ("Incident+created+from+upload" if created
           else quote_plus(
               f"This incident is already on record (#{incident.id}) — it was "
               "reused and no duplicate was created."
           ))
    return RedirectResponse(
        url=f"/bdrrmo/incidents?success={msg}", status_code=302
    )


# ══════════════════════════════════════════════════════════════════════
# POST-INCIDENT REPORTS (barangay-scoped)
# The same workflow CFAU runs in app/routes/cfau.py — manual entry or an
# uploaded document reviewed into the SAME IncidentReport model — with two
# scoping rules layered on: a BDRRMO user only ever sees their OWN reports,
# and a report may only be attached to an incident in their OWN barangay.
# Uploads are tagged with the same `post_incident` kind CFAU uses, so the
# admin review list keeps showing correct provenance for both portals.
# ══════════════════════════════════════════════════════════════════════

# Mirrors cfau.WORKFLOW_LABELS — only draft/submitted are reachable here.
REPORT_WORKFLOW_LABELS = {
    "draft": "Draft",
    "submitted": "Submitted",
    "reviewed": "Reviewed",
    "resolved": "Resolved",
}

UPLOAD_KIND_POST_INCIDENT = "post_incident"


def _own_incident_options(db, barangay):
    """Incidents this user may attach a report to — own barangay only."""
    if not barangay:
        return []
    return (
        db.query(Incident)
        .filter(Incident.barangay_id == barangay.id)
        .order_by(Incident.date_occurred.desc())
        .limit(200)
        .all()
    )


def _incident_label(inc) -> str:
    dtype = inc.disaster_type.value.replace("_", " ").title() if inc.disaster_type else "Incident"
    when = inc.date_occurred.strftime('%Y-%m-%d') if inc.date_occurred else "—"
    return f"{dtype} — {when}"


def _own_incident(db, barangay, incident_id):
    """Fetch an incident only if it belongs to the user's barangay."""
    if not barangay:
        return None
    return db.query(Incident).filter(
        Incident.id == incident_id,
        Incident.barangay_id == barangay.id,
    ).first()


def _own_incident_report(db, report_id, user):
    """A post-incident report the current user filed themselves."""
    r = db.query(IncidentReport).filter(IncidentReport.id == report_id).first()
    if not r or r.submitted_by != user["id"]:
        return None
    return r


@router.get("/incident-reports", response_class=HTMLResponse)
def incident_report_list(
    request: Request,
    db: Session = Depends(get_db),
    status: Optional[str] = None,
    page: Optional[str] = None,
    per_page: Optional[str] = None,
):
    user, barangay = _resolve_scope(request, db)
    if isinstance(user, RedirectResponse):
        return user

    query = db.query(IncidentReport).filter(
        IncidentReport.submitted_by == user["id"]
    )
    if status in {s.value for s in ServiceabilityStatus}:
        query = query.filter(IncidentReport.report_status == ServiceabilityStatus(status))

    rows = []
    for r in query.order_by(IncidentReport.created_at.desc()).all():
        inc = r.incident
        status_value = r.report_status.value if r.report_status else "draft"
        rows.append({
            "id": r.id,
            "disaster_type": (inc.disaster_type.value.replace("_", " ").title()
                              if inc and inc.disaster_type else "—"),
            "date_occurred": inc.date_occurred if inc else None,
            "personnel_count": r.personnel_count or 0,
            "report_status": status_value,
            "report_status_label": REPORT_WORKFLOW_LABELS.get(status_value, "Draft"),
            "created_at": r.created_at,
        })

    page_obj = paginate(rows, parse_page(page), parse_per_page(per_page))
    base_query = build_base_query({"status": status or ""})

    return templates.TemplateResponse(
        request=request,
        name="bdrrmo/incident_report_list.html",
        context={
            "user": user,
            "active_nav": "bdrrmo_incident_reports",
            "barangay": barangay,
            "rows": page_obj.items,
            "page_obj": page_obj,
            "base_query": base_query,
            "statuses": [("draft", "Draft"), ("submitted", "Submitted")],
            "f_status": status or "",
        },
    )


@router.get("/incident-reports/new", response_class=HTMLResponse)
def incident_report_new_form(request: Request, db: Session = Depends(get_db)):
    user, barangay = _resolve_scope(request, db)
    if isinstance(user, RedirectResponse):
        return user
    if not barangay:
        return RedirectResponse(
            url="/bdrrmo/incident-reports?error=No+barangay+is+assigned+to+your+account",
            status_code=302,
        )
    return templates.TemplateResponse(
        request=request,
        name="bdrrmo/incident_report_form.html",
        context={
            "user": user,
            "active_nav": "bdrrmo_incident_reports",
            "barangay": barangay,
            "edit_mode": False,
            "target": None,
            "incidents": [(i.id, _incident_label(i))
                          for i in _own_incident_options(db, barangay)],
            "error": None,
        },
    )


@router.post("/incident-reports/new")
def incident_report_create(
    request: Request,
    db: Session = Depends(get_db),
    incident_id: int = Form(...),
    operations_summary: str = Form(""),
    actions_taken: str = Form(""),
    equipment_used: str = Form(""),
    personnel_count: int = Form(0),
    personnel_notes: str = Form(""),
    challenges_encountered: str = Form(""),
    recommendations: str = Form(""),
    action: str = Form("draft"),
):
    user, barangay = _resolve_scope(request, db)
    if isinstance(user, RedirectResponse):
        return user
    if not barangay:
        return RedirectResponse(
            url="/bdrrmo/incident-reports?error=No+barangay+is+assigned+to+your+account",
            status_code=302,
        )

    # An incident outside the user's barangay is not selectable, and is
    # rejected here too — the dropdown is presentation, this is the guard.
    incident = _own_incident(db, barangay, incident_id)
    if not incident:
        return templates.TemplateResponse(
            request=request,
            name="bdrrmo/incident_report_form.html",
            context={
                "user": user,
                "active_nav": "bdrrmo_incident_reports",
                "barangay": barangay,
                "edit_mode": False,
                "target": None,
                "incidents": [(i.id, _incident_label(i))
                              for i in _own_incident_options(db, barangay)],
                "error": "Please select an incident recorded in your barangay.",
            },
        )

    submitting = (action == "submit")
    r = IncidentReport(
        incident_id=incident.id,
        submitted_by=user["id"],
        operations_summary=operations_summary.strip() or None,
        actions_taken=actions_taken.strip() or None,
        equipment_used=equipment_used.strip() or None,
        personnel_count=max(0, personnel_count or 0),
        personnel_notes=personnel_notes.strip() or None,
        challenges_encountered=challenges_encountered.strip() or None,
        recommendations=recommendations.strip() or None,
        report_status=ServiceabilityStatus.submitted if submitting else ServiceabilityStatus.draft,
        submitted_at=datetime.utcnow() if submitting else None,
    )
    db.add(r)
    db.commit()
    db.refresh(r)

    log_action(
        db, user["id"], "submitted" if submitting else "created",
        "incident_reports", r.id,
        f"BDRRMO post-incident report for {_incident_label(incident)} in "
        f"{barangay.name} {'submitted' if submitting else 'saved as draft'}",
    )

    msg = "Report+submitted" if submitting else "Draft+saved"
    return RedirectResponse(url=f"/bdrrmo/incident-reports?success={msg}", status_code=302)


# ── Upload path ───────────────────────────────────────────────────────
# Same Bronze storage + extraction + AI summary + UploadedReport lifecycle
# as every other upload in the app; converts into the SAME IncidentReport
# model as the manual path above. Declared BEFORE /incident-reports/{id}
# so the literal "upload" segment matches first.

def _get_owned_report_upload(db, report_id, user):
    """A post-incident upload the current user may act on (own only)."""
    r = db.query(UploadedReport).filter(UploadedReport.id == report_id).first()
    if not r or (r.extracted_data or {}).get("report_kind") != UPLOAD_KIND_POST_INCIDENT:
        return None
    if r.uploaded_by != user["id"]:
        return None
    return r


@router.get("/incident-reports/upload", response_class=HTMLResponse)
def incident_report_upload_form(request: Request, db: Session = Depends(get_db)):
    user, barangay = _resolve_scope(request, db)
    if isinstance(user, RedirectResponse):
        return user
    if not barangay:
        return RedirectResponse(
            url="/bdrrmo/incident-reports?error=No+barangay+is+assigned+to+your+account",
            status_code=302,
        )
    return templates.TemplateResponse(
        request=request,
        name="bdrrmo/incident_report_upload_form.html",
        context={
            "user": user,
            "active_nav": "bdrrmo_incident_reports",
            "barangay": barangay,
            "error": request.query_params.get("error"),
            "ai_available": ai_available(),
        },
    )


@router.post("/incident-reports/upload")
async def incident_report_upload_submit(
    request: Request,
    db: Session = Depends(get_db),
    file: UploadFile = File(...),
):
    user, barangay = _resolve_scope(request, db)
    if isinstance(user, RedirectResponse):
        return user
    if not barangay:
        return RedirectResponse(
            url="/bdrrmo/incident-reports?error=No+barangay+is+assigned+to+your+account",
            status_code=302,
        )

    original_name = file.filename or "report"
    ext = _ext_of(original_name)
    if ext not in ALLOWED_EXTS:
        return RedirectResponse(
            url="/bdrrmo/incident-reports/upload?error=Unsupported+file+type.+Allowed:+PDF,+XLSX,+XLS,+CSV.",
            status_code=302,
        )

    os.makedirs(UPLOAD_SUBDIR, exist_ok=True)
    stored_name = _safe_filename(original_name)
    stored_path = os.path.join(UPLOAD_SUBDIR, stored_name)

    ok, err = await save_validated_upload(file, ext, stored_path)
    if not ok:
        return RedirectResponse(
            url="/bdrrmo/incident-reports/upload?error=" + quote_plus(err),
            status_code=302,
        )

    file_type_enum = ALLOWED_EXTS[ext]
    report = UploadedReport(
        uploaded_by=user["id"],
        file_name=original_name,
        file_path=stored_path.replace("\\", "/"),
        file_type=file_type_enum,
        status=ReportStatus.processing,
        lifecycle_status=LifecycleStatus.draft,
        barangay_id=barangay.id,
    )
    db.add(report)
    db.commit()
    db.refresh(report)

    add_upload_history(
        db, report_id=report.id, user_id=user["id"],
        event_type=UploadEvent.created,
        new_value=f"BDRRMO uploaded post-incident document '{original_name}' "
                  f"({file_type_enum.value.upper()}) for {barangay.name}",
    )
    db.commit()

    # ── Silver: extract raw text/rows + pre-fill the report form ──────
    # `report_fields` are the operational fields of IncidentReport; the
    # incident triple is only used to pre-select an existing incident in
    # the user's own barangay (uploads here never create incidents — that
    # is what /bdrrmo/incidents/upload is for).
    extracted = {
        "report_kind": UPLOAD_KIND_POST_INCIDENT,
        "raw_text": "", "rows": [], "columns": [], "error": None,
        "report_fields": empty_incident_report_fields(),
        "matched_incident_id": None,
    }
    core = {"disaster_type": "", "date_occurred": ""}
    try:
        if file_type_enum == FileType.pdf:
            out = extract_pdf(stored_path)
            extracted["raw_text"] = out.get("text", "")
            known_barangays = [b.name for b in db.query(Barangay).all()]
            core_row = structure_text(extracted["raw_text"], known_barangays)
            core = {k: core_row.get(k, "") for k in core}
            extracted["report_fields"] = structure_incident_report_text(
                extracted["raw_text"]
            )
        elif file_type_enum in (FileType.excel, FileType.csv):
            out = extract_excel(stored_path) if file_type_enum == FileType.excel \
                else extract_csv(stored_path)
            extracted["columns"] = out["columns"]
            extracted["rows"] = out["rows"]
            extracted["report_fields"] = structure_incident_report_rows(out["rows"])
            core_rows = structure_rows(out["rows"])
            if core_rows:
                core = {k: core_rows[0].get(k, "") for k in core}
        report.status = ReportStatus.reviewed
    except Exception as e:
        print(f"[bdrrmo] Extraction failed for report '{original_name}': {e}")
        extracted["error"] = "Extraction failed. The file could not be read or was malformed."
        report.status = ReportStatus.failed

    # Optional AI summary — never blocks the flow.
    ai_text = None
    if extracted.get("raw_text"):
        ai_text = ai_summarize(extracted["raw_text"])
    elif extracted.get("rows"):
        preview = "\n".join(
            ", ".join(f"{k}={v}" for k, v in row.items())
            for row in extracted["rows"][:25]
        )
        ai_text = ai_summarize(preview) if preview else None
    if ai_text:
        report.ai_summary = ai_text

    # Keep a narrative upload from arriving completely blank.
    if not extracted["report_fields"].get("operations_summary"):
        if ai_text:
            extracted["report_fields"]["operations_summary"] = ai_text
        elif extracted.get("raw_text"):
            extracted["report_fields"]["operations_summary"] = \
                extracted["raw_text"].strip()[:2000]

    # Strict incident match inside the LOCKED barangay — the document's own
    # barangay is ignored, exactly as on the incident-upload path.
    matched = find_matching_incident(
        db, barangay.name, core.get("disaster_type"), core.get("date_occurred")
    )
    extracted["matched_incident_id"] = matched.id if matched else None

    report.extracted_data = extracted
    db.commit()

    note = (f"Extraction failed: {extracted['error']}" if extracted.get("error")
            else "Extraction completed — file ready as reference for the report form.")
    add_upload_history(
        db, report_id=report.id, user_id=user["id"],
        event_type=UploadEvent.extracted, new_value=note,
    )
    db.commit()

    return RedirectResponse(
        url=f"/bdrrmo/incident-reports/upload/{report.id}/review", status_code=302
    )


@router.get("/incident-reports/upload/{report_id}/review", response_class=HTMLResponse)
def incident_report_upload_review(
    report_id: int, request: Request, db: Session = Depends(get_db)
):
    user, barangay = _resolve_scope(request, db)
    if isinstance(user, RedirectResponse):
        return user
    report = _get_owned_report_upload(db, report_id, user)
    if not report:
        return RedirectResponse(url="/bdrrmo/incident-reports", status_code=302)

    data = report.extracted_data or {}
    produced_id = data.get("produced_incident_report_id")
    # Already converted — send the user to the produced report.
    if produced_id:
        return RedirectResponse(
            url=f"/bdrrmo/incident-reports/{produced_id}", status_code=302
        )

    # Only pre-select the matched incident if it still exists and is still
    # in this user's barangay.
    matched_id = data.get("matched_incident_id")
    matched_incident = _own_incident(db, barangay, matched_id) if matched_id else None

    return templates.TemplateResponse(
        request=request,
        name="bdrrmo/incident_report_upload_review.html",
        context={
            "user": user,
            "active_nav": "bdrrmo_incident_reports",
            "barangay": barangay,
            "report": report,
            "incidents": [(i.id, _incident_label(i))
                          for i in _own_incident_options(db, barangay)],
            "ai_summary": report.ai_summary,
            "ai_available": ai_available(),
            "raw_text_preview": (data.get("raw_text") or "")[:4000],
            "rows_preview": (data.get("rows") or [])[:10],
            "columns_preview": data.get("columns") or [],
            "extraction_error": data.get("error"),
            "error": request.query_params.get("error"),
            "prefill": data.get("report_fields") or empty_incident_report_fields(),
            "selected_incident_id": matched_incident.id if matched_incident else None,
        },
    )


@router.get("/incident-reports/upload/{report_id}/file")
def incident_report_upload_file(
    report_id: int, request: Request, db: Session = Depends(get_db)
):
    user, _ = _resolve_scope(request, db)
    if isinstance(user, RedirectResponse):
        return user
    report = _get_owned_report_upload(db, report_id, user)
    if not report or not report.file_path or not os.path.exists(report.file_path):
        return RedirectResponse(url="/bdrrmo/incident-reports", status_code=302)
    return FileResponse(report.file_path, filename=report.file_name)


@router.post("/incident-reports/upload/{report_id}/submit")
def incident_report_upload_convert(
    report_id: int,
    request: Request,
    db: Session = Depends(get_db),
    incident_id: int = Form(...),
    operations_summary: str = Form(""),
    actions_taken: str = Form(""),
    equipment_used: str = Form(""),
    # Blank means "not reported" — kept NULL rather than a misleading 0.
    personnel_count: Optional[str] = Form(None),
    personnel_notes: str = Form(""),
    challenges_encountered: str = Form(""),
    recommendations: str = Form(""),
    action: str = Form("draft"),
):
    user, barangay = _resolve_scope(request, db)
    if isinstance(user, RedirectResponse):
        return user
    if not barangay:
        return RedirectResponse(
            url="/bdrrmo/incident-reports?error=No+barangay+is+assigned+to+your+account",
            status_code=302,
        )
    report = _get_owned_report_upload(db, report_id, user)
    if not report:
        return RedirectResponse(url="/bdrrmo/incident-reports", status_code=302)

    data = dict(report.extracted_data or {})
    if data.get("produced_incident_report_id"):
        # Idempotency guard — already converted.
        return RedirectResponse(
            url=f"/bdrrmo/incident-reports/{data['produced_incident_report_id']}",
            status_code=302,
        )

    incident = _own_incident(db, barangay, incident_id)
    if not incident:
        return RedirectResponse(
            url=f"/bdrrmo/incident-reports/upload/{report.id}/review?error="
                + quote_plus("Please select an incident recorded in your barangay."),
            status_code=302,
        )

    _pc = (personnel_count or "").strip()
    personnel_value = max(0, int(_pc)) if _pc.isdigit() else None

    submitting = (action == "submit")
    # Same model + fields as the manual path — both converge here.
    r = IncidentReport(
        incident_id=incident.id,
        submitted_by=user["id"],
        operations_summary=operations_summary.strip() or None,
        actions_taken=actions_taken.strip() or None,
        equipment_used=equipment_used.strip() or None,
        personnel_count=personnel_value,
        personnel_notes=personnel_notes.strip() or None,
        challenges_encountered=challenges_encountered.strip() or None,
        recommendations=recommendations.strip() or None,
        report_status=ServiceabilityStatus.submitted if submitting else ServiceabilityStatus.draft,
        submitted_at=datetime.utcnow() if submitting else None,
    )
    db.add(r)
    db.commit()
    db.refresh(r)

    # Link the produced report back to the upload (JSON — same linkage the
    # admin review list reads) and close the upload lifecycle as converted.
    data["produced_incident_report_id"] = r.id
    data["linked_incident_id"] = incident.id
    report.extracted_data = data
    report.incident_id = incident.id
    report.lifecycle_status = LifecycleStatus.confirmed
    report.status = ReportStatus.confirmed
    db.commit()

    add_upload_history(
        db, report_id=report.id, user_id=user["id"],
        event_type=UploadEvent.confirmed,
        new_value=f"Converted to post-incident report #{r.id} "
                  f"({'submitted' if submitting else 'draft'}) — "
                  f"linked to incident #{incident.id}.",
    )
    db.commit()

    log_action(
        db, user["id"], "converted", "uploaded_reports", report.id,
        f"BDRRMO converted upload '{report.file_name}' to post-incident report #{r.id}",
    )
    log_action(
        db, user["id"], "submitted" if submitting else "created",
        "incident_reports", r.id,
        f"BDRRMO post-incident report for {_incident_label(incident)} in "
        f"{barangay.name} {'submitted' if submitting else 'saved as draft'} "
        f"(from upload #{report.id})",
    )

    msg = "Report+submitted+from+upload" if submitting else "Draft+saved+from+upload"
    return RedirectResponse(url=f"/bdrrmo/incident-reports?success={msg}", status_code=302)


def _find_source_upload(db, incident_report_id):
    """Reverse-lookup the upload that produced this report via the existing
    JSON linkage (no schema change). Scans only post-incident uploads."""
    for up in db.query(UploadedReport).filter(
        UploadedReport.extracted_data.isnot(None)
    ).all():
        data = up.extracted_data or {}
        if (data.get("report_kind") == UPLOAD_KIND_POST_INCIDENT
                and data.get("produced_incident_report_id") == incident_report_id):
            return up
    return None


@router.get("/incident-reports/{report_id}", response_class=HTMLResponse)
def incident_report_detail(report_id: int, request: Request, db: Session = Depends(get_db)):
    user, barangay = _resolve_scope(request, db)
    if isinstance(user, RedirectResponse):
        return user
    r = _own_incident_report(db, report_id, user)
    if not r:
        return RedirectResponse(url="/bdrrmo/incident-reports", status_code=302)

    inc = r.incident
    return templates.TemplateResponse(
        request=request,
        name="bdrrmo/incident_report_detail.html",
        context={
            "user": user,
            "active_nav": "bdrrmo_incident_reports",
            "barangay": barangay,
            "r": r,
            "incident": inc,
            "disaster_type": (inc.disaster_type.value.replace("_", " ").title()
                              if inc and inc.disaster_type else "—"),
            "workflow_label": REPORT_WORKFLOW_LABELS.get(
                r.report_status.value if r.report_status else "draft", "Draft"
            ),
            "can_edit": r.report_status == ServiceabilityStatus.draft,
            "source_upload": _find_source_upload(db, r.id),
        },
    )


@router.get("/incident-reports/{report_id}/edit", response_class=HTMLResponse)
def incident_report_edit_form(report_id: int, request: Request, db: Session = Depends(get_db)):
    user, barangay = _resolve_scope(request, db)
    if isinstance(user, RedirectResponse):
        return user
    r = _own_incident_report(db, report_id, user)
    if not r:
        return RedirectResponse(url="/bdrrmo/incident-reports", status_code=302)
    if r.report_status != ServiceabilityStatus.draft:
        return RedirectResponse(
            url=f"/bdrrmo/incident-reports/{report_id}?error=Only+drafts+can+be+edited",
            status_code=302,
        )
    return templates.TemplateResponse(
        request=request,
        name="bdrrmo/incident_report_form.html",
        context={
            "user": user,
            "active_nav": "bdrrmo_incident_reports",
            "barangay": barangay,
            "edit_mode": True,
            "target": r,
            "incidents": [(i.id, _incident_label(i))
                          for i in _own_incident_options(db, barangay)],
            "error": None,
        },
    )


@router.post("/incident-reports/{report_id}/edit")
def incident_report_edit(
    report_id: int,
    request: Request,
    db: Session = Depends(get_db),
    incident_id: int = Form(...),
    operations_summary: str = Form(""),
    actions_taken: str = Form(""),
    equipment_used: str = Form(""),
    personnel_count: int = Form(0),
    personnel_notes: str = Form(""),
    challenges_encountered: str = Form(""),
    recommendations: str = Form(""),
    action: str = Form("draft"),
):
    user, barangay = _resolve_scope(request, db)
    if isinstance(user, RedirectResponse):
        return user
    r = _own_incident_report(db, report_id, user)
    if not r:
        return RedirectResponse(url="/bdrrmo/incident-reports", status_code=302)
    if r.report_status != ServiceabilityStatus.draft:
        return RedirectResponse(
            url=f"/bdrrmo/incident-reports/{report_id}?error=Only+drafts+can+be+edited",
            status_code=302,
        )

    # Re-pointing the report is only allowed within the user's own barangay.
    incident = _own_incident(db, barangay, incident_id)
    if incident:
        r.incident_id = incident.id
    r.operations_summary = operations_summary.strip() or None
    r.actions_taken = actions_taken.strip() or None
    r.equipment_used = equipment_used.strip() or None
    r.personnel_count = max(0, personnel_count or 0)
    r.personnel_notes = personnel_notes.strip() or None
    r.challenges_encountered = challenges_encountered.strip() or None
    r.recommendations = recommendations.strip() or None

    submitting = (action == "submit")
    if submitting:
        r.report_status = ServiceabilityStatus.submitted
        r.submitted_at = datetime.utcnow()

    db.commit()

    log_action(
        db, user["id"], "submitted" if submitting else "edited",
        "incident_reports", r.id,
        f"BDRRMO post-incident report #{r.id} "
        f"{'submitted' if submitting else 'draft edited'}",
    )

    msg = "Report+submitted" if submitting else "Draft+updated"
    return RedirectResponse(url=f"/bdrrmo/incident-reports?success={msg}", status_code=302)


@router.post("/incident-reports/{report_id}/submit")
def incident_report_submit(report_id: int, request: Request, db: Session = Depends(get_db)):
    user, _ = _resolve_scope(request, db)
    if isinstance(user, RedirectResponse):
        return user
    r = _own_incident_report(db, report_id, user)
    if not r:
        return RedirectResponse(url="/bdrrmo/incident-reports", status_code=302)
    if r.report_status != ServiceabilityStatus.draft:
        return RedirectResponse(
            url="/bdrrmo/incident-reports?error=Only+drafts+can+be+submitted",
            status_code=302,
        )

    r.report_status = ServiceabilityStatus.submitted
    r.submitted_at = datetime.utcnow()
    db.commit()

    log_action(
        db, user["id"], "submitted", "incident_reports", r.id,
        f"BDRRMO post-incident report #{r.id} submitted",
    )
    return RedirectResponse(
        url="/bdrrmo/incident-reports?success=Report+submitted", status_code=302
    )


# ══════════════════════════════════════════════════════════════════════
# CRITICAL FACILITIES (barangay-scoped, add / update / manage)
# ══════════════════════════════════════════════════════════════════════


def _facility_map_json(f: Facility, own_barangay_id: int) -> dict:
    """Shape one Facility for the Leaflet popup on the BDRRMO map — the
    same field set admin/map.html's /admin/api/facilities-map-data exposes,
    minus the admin-only city-level/approximate-location badges.

    `is_own` drives the read-only presentation of other barangays'
    facilities in the citywide view (muted marker, no Edit action). It is
    presentation only — the write routes enforce scoping via
    _own_facility()."""
    return {
        "id": f.id,
        "name": f.name,
        "barangay": f.barangay.name if f.barangay else None,
        "is_own": f.barangay_id == own_barangay_id,
        "facility_type": f.facility_type.value if f.facility_type else None,
        "facility_type_label": (
            f.facility_type.value.replace("_", " ").title() if f.facility_type else None
        ),
        "lat": f.latitude,
        "lng": f.longitude,
        "address": f.address,
        "operational_status": f.operational_status.value if f.operational_status else None,
        "status": f.status,
        "floor_area_sqm": f.floor_area_sqm,
        "capacity_families": f.capacity_families,
        "capacity_individuals": f.capacity_individuals,
        "ereid_capacity_families": f.ereid_capacity_families,
        "ereid_capacity_individuals": f.ereid_capacity_individuals,
        "supports_tropical_cyclone": bool(f.supports_tropical_cyclone),
        "supports_flooding": bool(f.supports_flooding),
        "supports_landslide": bool(f.supports_landslide),
        "supports_fire": bool(f.supports_fire),
        "vulnerability_risk": f.vulnerability_risk,
        "eo_moa_mou": f.eo_moa_mou,
        "eo_moa_mou_status": f.eo_moa_mou_status,
        "eo_moa_mou_reference": f.eo_moa_mou_reference,
    }


@router.get("/facilities", response_class=HTMLResponse)
def facilities(request: Request, db: Session = Depends(get_db)):
    user, barangay = _resolve_scope(request, db)
    if isinstance(user, RedirectResponse):
        return user

    # Default view is the user's own barangay (TR-BDR-10 scoping for every
    # write); ?scope=city widens the *read* to all of San Pedro, matching
    # what the admin map shows. Archiving stays an own-barangay concern, so
    # the archived tab is not available in the citywide view.
    citywide = request.query_params.get("scope") == "city"
    show_archived = not citywide and request.query_params.get("archived") == "1"
    rows = []
    archived_count = 0
    active_facilities = []
    barangays = []
    if barangay:
        archived_count = db.query(Facility).filter(
            Facility.barangay_id == barangay.id,
            Facility.is_archived == True,
        ).count()
        if citywide:
            rows = (
                db.query(Facility)
                .join(Barangay, Facility.barangay_id == Barangay.id)
                .filter(Facility.is_archived == False)
                .order_by(Barangay.name, Facility.name)
                .all()
            )
            active_facilities = rows
            barangays = db.query(Barangay).order_by(Barangay.name).all()
        else:
            rows = db.query(Facility).filter(
                Facility.barangay_id == barangay.id,
                Facility.is_archived == show_archived,
            ).order_by(Facility.facility_type, Facility.name).all()
            # The map always shows active facilities, regardless of which tab
            # (Active/Archived) the table below is on.
            active_facilities = rows if not show_archived else db.query(Facility).filter(
                Facility.barangay_id == barangay.id,
                Facility.is_archived == False,
            ).order_by(Facility.facility_type, Facility.name).all()

    summary = {
        "total": len(active_facilities),
        "evacuation_centers": sum(
            1 for f in active_facilities
            if f.facility_type == FacilityType.evacuation_center
        ),
        "available": sum(
            1 for f in active_facilities
            if f.operational_status == FacilityStatus.available
        ),
        "archived": archived_count,
    }

    return templates.TemplateResponse(
        request=request,
        name="bdrrmo/facilities.html",
        context={
            "user": user,
            "active_nav": "bdrrmo_facilities",
            "barangay": barangay,
            "facilities": rows,
            "map_facilities": [
                _facility_map_json(f, barangay.id if barangay else None)
                for f in active_facilities
            ],
            "citywide": citywide,
            "barangays": barangays,
            "sp_bounds": get_san_pedro_bounds(),
            "sp_boundary": get_san_pedro_boundary(),
            "summary": summary,
            "facility_types": [t.value for t in FacilityType],
            "facility_statuses": [s.value for s in FacilityStatus],
            "facility_classifications": FACILITY_CLASSIFICATIONS,
            "eo_moa_mou_statuses": EO_MOA_MOU_STATUSES,
            "show_archived": show_archived,
            "archived_count": archived_count,
            "edit_id": request.query_params.get("edit"),
            "success": request.query_params.get("success"),
            "error": request.query_params.get("error"),
        },
    )


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
    classification: str = Form(""),
    floor_area_sqm: str = Form(""),
    capacity_families: str = Form(""),
    capacity_individuals: str = Form(""),
    ereid_capacity_families: str = Form(""),
    ereid_capacity_individuals: str = Form(""),
    supports_tropical_cyclone: Optional[str] = Form(None),
    supports_flooding: Optional[str] = Form(None),
    supports_landslide: Optional[str] = Form(None),
    supports_fire: Optional[str] = Form(None),
    hazard_reference_master_list: str = Form(""),
    eo_moa_mou_status: str = Form(""),
    eo_moa_mou_reference: str = Form(""),
    notes: str = Form(""),
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
    name_clean, name_err = _validate_facility_name(name)
    if name_err:
        return RedirectResponse(
            url="/bdrrmo/facilities?error=" + quote_plus(name_err), status_code=302
        )
    if facility_type not in {t.value for t in FacilityType}:
        return RedirectResponse(
            url="/bdrrmo/facilities?error=Invalid+facility+type", status_code=302
        )
    if operational_status not in {s.value for s in FacilityStatus}:
        operational_status = FacilityStatus.available.value
    address_clean, address_err = _validate_facility_address(address)
    if address_err:
        return RedirectResponse(
            url="/bdrrmo/facilities?error=" + quote_plus(address_err), status_code=302
        )
    lat = _parse_coord(latitude, -90, 90)
    lon = _parse_coord(longitude, -180, 180)
    if lat is None or lon is None:
        return RedirectResponse(
            url="/bdrrmo/facilities?error=Invalid+coordinates", status_code=302
        )
    if not is_within_san_pedro(lat, lon):
        return RedirectResponse(
            url="/bdrrmo/facilities?error=" + quote_plus(
                "Location must be within San Pedro City, Laguna."
            ),
            status_code=302,
        )

    details, details_err = validate_facility_details({
        "classification": classification,
        "floor_area_sqm": floor_area_sqm,
        "capacity_families": capacity_families,
        "capacity_individuals": capacity_individuals,
        "ereid_capacity_families": ereid_capacity_families,
        "ereid_capacity_individuals": ereid_capacity_individuals,
        "supports_tropical_cyclone": supports_tropical_cyclone,
        "supports_flooding": supports_flooding,
        "supports_landslide": supports_landslide,
        "supports_fire": supports_fire,
        "hazard_reference_master_list": hazard_reference_master_list,
        "eo_moa_mou_status": eo_moa_mou_status,
        "eo_moa_mou_reference": eo_moa_mou_reference,
        "notes": notes,
    })
    if details_err:
        return RedirectResponse(
            url="/bdrrmo/facilities?error=" + quote_plus(details_err), status_code=302
        )

    dup = _find_duplicate(db, barangay.id, name_clean, lat, lon)
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
        name=name_clean,
        facility_type=FacilityType(facility_type),
        latitude=lat,
        longitude=lon,
        address=address_clean,
        operational_status=FacilityStatus(operational_status),
        is_archived=False,
        **details,
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
    classification: str = Form(""),
    floor_area_sqm: str = Form(""),
    capacity_families: str = Form(""),
    capacity_individuals: str = Form(""),
    ereid_capacity_families: str = Form(""),
    ereid_capacity_individuals: str = Form(""),
    supports_tropical_cyclone: Optional[str] = Form(None),
    supports_flooding: Optional[str] = Form(None),
    supports_landslide: Optional[str] = Form(None),
    supports_fire: Optional[str] = Form(None),
    hazard_reference_master_list: str = Form(""),
    eo_moa_mou_status: str = Form(""),
    eo_moa_mou_reference: str = Form(""),
    notes: str = Form(""),
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
    name_clean, name_err = _validate_facility_name(name)
    if name_err:
        return RedirectResponse(
            url=f"/bdrrmo/facilities?edit={facility.id}&error=" + quote_plus(name_err),
            status_code=302,
        )
    if facility_type not in {t.value for t in FacilityType}:
        return RedirectResponse(
            url="/bdrrmo/facilities?error=Invalid+facility+type", status_code=302
        )
    if operational_status not in {s.value for s in FacilityStatus}:
        operational_status = FacilityStatus.available.value
    address_clean, address_err = _validate_facility_address(address)
    if address_err:
        return RedirectResponse(
            url=f"/bdrrmo/facilities?edit={facility.id}&error=" + quote_plus(address_err),
            status_code=302,
        )
    lat = _parse_coord(latitude, -90, 90)
    lon = _parse_coord(longitude, -180, 180)
    if lat is None or lon is None:
        return RedirectResponse(
            url="/bdrrmo/facilities?error=Invalid+coordinates", status_code=302
        )
    if not is_within_san_pedro(lat, lon):
        return RedirectResponse(
            url=f"/bdrrmo/facilities?edit={facility.id}&error=" + quote_plus(
                "Location must be within San Pedro City, Laguna."
            ),
            status_code=302,
        )

    details, details_err = validate_facility_details({
        "classification": classification,
        "floor_area_sqm": floor_area_sqm,
        "capacity_families": capacity_families,
        "capacity_individuals": capacity_individuals,
        "ereid_capacity_families": ereid_capacity_families,
        "ereid_capacity_individuals": ereid_capacity_individuals,
        "supports_tropical_cyclone": supports_tropical_cyclone,
        "supports_flooding": supports_flooding,
        "supports_landslide": supports_landslide,
        "supports_fire": supports_fire,
        "hazard_reference_master_list": hazard_reference_master_list,
        "eo_moa_mou_status": eo_moa_mou_status,
        "eo_moa_mou_reference": eo_moa_mou_reference,
        "notes": notes,
    })
    if details_err:
        return RedirectResponse(
            url=f"/bdrrmo/facilities?edit={facility.id}&error=" + quote_plus(details_err),
            status_code=302,
        )

    dup = _find_duplicate(db, barangay.id, name_clean, lat, lon, exclude_id=facility.id)
    if dup:
        return RedirectResponse(
            url=f"/bdrrmo/facilities?edit={facility.id}&error=" + quote_plus(
                f"Another facility already matches this name/location: '{dup.name}'."
            ),
            status_code=302,
        )

    facility.name = name_clean
    facility.facility_type = FacilityType(facility_type)
    facility.latitude = lat
    facility.longitude = lon
    facility.address = address_clean
    facility.operational_status = FacilityStatus(operational_status)
    for key, value in details.items():
        setattr(facility, key, value)
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
# BARANGAY EQUIPMENT & VEHICLES (barangay-scoped CRUD)
# A dedicated barangay inventory, separate from the admin/CFAU Equipment
# table and the admin Resource logistics — never shared with them. Mirrors
# the facilities CRUD pattern: list (active/archived), add, edit, quick
# status-set, archive/restore. One table holds both vehicles and gear.
# ══════════════════════════════════════════════════════════════════════

# Types representing individually-tracked vehicles (for UI grouping); the
# remaining types are gear that may be tracked in bulk via quantity.
VEHICLE_TYPES = {
    EquipmentType.fire_truck.value,
    EquipmentType.ambulance.value,
    EquipmentType.rescue_vehicle.value,
    EquipmentType.rescue_boat.value,
}


def _own_equipment(db, barangay, equipment_id):
    """Fetch an equipment item only if it belongs to the user's barangay."""
    return db.query(BarangayEquipment).filter(
        BarangayEquipment.id == equipment_id,
        BarangayEquipment.barangay_id == barangay.id,
    ).first()


def _parse_optional_date(value):
    """Coerce an ISO date string to a date, or None if blank/invalid."""
    try:
        return date.fromisoformat((value or "").strip())
    except (ValueError, TypeError):
        return None


@router.get("/equipment", response_class=HTMLResponse)
def equipment(request: Request, db: Session = Depends(get_db)):
    user, barangay = _resolve_scope(request, db)
    if isinstance(user, RedirectResponse):
        return user

    show_archived = request.query_params.get("archived") == "1"
    rows = []
    archived_count = 0
    if barangay:
        archived_count = db.query(BarangayEquipment).filter(
            BarangayEquipment.barangay_id == barangay.id,
            BarangayEquipment.is_archived == True,
        ).count()
        rows = db.query(BarangayEquipment).filter(
            BarangayEquipment.barangay_id == barangay.id,
            BarangayEquipment.is_archived == show_archived,
        ).order_by(BarangayEquipment.equipment_type, BarangayEquipment.name).all()

    return templates.TemplateResponse(
        request=request,
        name="bdrrmo/equipment.html",
        context={
            "user": user,
            "active_nav": "bdrrmo_equipment",
            "barangay": barangay,
            "equipment": rows,
            "equipment_types": [t.value for t in EquipmentType],
            "equipment_statuses": [s.value for s in FacilityStatus],
            "vehicle_types": VEHICLE_TYPES,
            "show_archived": show_archived,
            "archived_count": archived_count,
            "edit_id": request.query_params.get("edit"),
            "success": request.query_params.get("success"),
            "error": request.query_params.get("error"),
        },
    )


@router.post("/equipment")
def equipment_create(
    request: Request,
    db: Session = Depends(get_db),
    name: str = Form(...),
    equipment_type: str = Form(...),
    status: str = Form("available"),
    quantity: int = Form(1),
    plate_or_serial: str = Form(""),
    maintenance_notes: str = Form(""),
    last_inspected: str = Form(""),
):
    """Add a barangay equipment/vehicle item within own barangay."""
    user, barangay = _resolve_scope(request, db)
    if isinstance(user, RedirectResponse):
        return user
    if not barangay:
        return RedirectResponse(
            url="/bdrrmo/equipment?error=No+barangay+is+assigned+to+your+account",
            status_code=302,
        )
    if equipment_type not in {t.value for t in EquipmentType}:
        return RedirectResponse(
            url="/bdrrmo/equipment?error=Invalid+equipment+type", status_code=302
        )
    if status not in {s.value for s in FacilityStatus}:
        status = FacilityStatus.available.value

    item = BarangayEquipment(
        barangay_id=barangay.id,                 # scoped to own barangay
        updated_by=user["id"],
        name=name.strip(),
        equipment_type=EquipmentType(equipment_type),
        status=FacilityStatus(status),
        quantity=max(0, quantity or 0),
        plate_or_serial=(plate_or_serial or "").strip() or None,
        maintenance_notes=(maintenance_notes or "").strip() or None,
        last_inspected=_parse_optional_date(last_inspected),
        is_archived=False,
    )
    db.add(item)
    db.commit()
    db.refresh(item)

    log_action(
        db, user["id"], "created", "barangay_equipment", item.id,
        f"BDRRMO added equipment '{item.name}' "
        f"({item.equipment_type.value}, qty {item.quantity}, "
        f"status={item.status.value}) in {barangay.name}",
    )
    return RedirectResponse(
        url="/bdrrmo/equipment?success=Equipment+added", status_code=302
    )


@router.post("/equipment/{equipment_id}/edit")
def equipment_edit(
    equipment_id: int,
    request: Request,
    db: Session = Depends(get_db),
    name: str = Form(...),
    equipment_type: str = Form(...),
    status: str = Form("available"),
    quantity: int = Form(1),
    plate_or_serial: str = Form(""),
    maintenance_notes: str = Form(""),
    last_inspected: str = Form(""),
):
    """Update an existing equipment/vehicle item within own barangay."""
    user, barangay = _resolve_scope(request, db)
    if isinstance(user, RedirectResponse):
        return user
    if not barangay:
        return RedirectResponse(
            url="/bdrrmo/equipment?error=No+barangay+is+assigned+to+your+account",
            status_code=302,
        )
    item = _own_equipment(db, barangay, equipment_id)
    if not item:
        return RedirectResponse(
            url="/bdrrmo/equipment?error=Equipment+not+found", status_code=302
        )
    if equipment_type not in {t.value for t in EquipmentType}:
        return RedirectResponse(
            url=f"/bdrrmo/equipment?edit={item.id}&error=Invalid+equipment+type",
            status_code=302,
        )
    if status not in {s.value for s in FacilityStatus}:
        status = FacilityStatus.available.value

    item.name = name.strip()
    item.equipment_type = EquipmentType(equipment_type)
    item.status = FacilityStatus(status)
    item.quantity = max(0, quantity or 0)
    item.plate_or_serial = (plate_or_serial or "").strip() or None
    item.maintenance_notes = (maintenance_notes or "").strip() or None
    item.last_inspected = _parse_optional_date(last_inspected)
    item.updated_by = user["id"]
    db.commit()

    log_action(
        db, user["id"], "updated", "barangay_equipment", item.id,
        f"BDRRMO updated equipment '{item.name}' "
        f"(status={item.status.value}) in {barangay.name}",
    )
    return RedirectResponse(
        url="/bdrrmo/equipment?success=Equipment+updated", status_code=302
    )


@router.post("/equipment/{equipment_id}/status")
def equipment_set_status(
    equipment_id: int,
    request: Request,
    db: Session = Depends(get_db),
    status: str = Form(...),
):
    """Quick-set an item's operational status
    (available / under_maintenance / unavailable)."""
    user, barangay = _resolve_scope(request, db)
    if isinstance(user, RedirectResponse):
        return user
    if not barangay:
        return RedirectResponse(
            url="/bdrrmo/equipment?error=No+barangay+is+assigned+to+your+account",
            status_code=302,
        )
    item = _own_equipment(db, barangay, equipment_id)
    if not item:
        return RedirectResponse(
            url="/bdrrmo/equipment?error=Equipment+not+found", status_code=302
        )
    if status not in {s.value for s in FacilityStatus}:
        return RedirectResponse(
            url="/bdrrmo/equipment?error=Invalid+status", status_code=302
        )

    item.status = FacilityStatus(status)
    item.updated_by = user["id"]
    db.commit()
    log_action(
        db, user["id"], "updated", "barangay_equipment", item.id,
        f"BDRRMO set equipment '{item.name}' status to {item.status.value}",
    )
    return RedirectResponse(
        url="/bdrrmo/equipment?success=Equipment+status+updated", status_code=302
    )


@router.post("/equipment/{equipment_id}/archive")
def equipment_archive(
    equipment_id: int, request: Request, db: Session = Depends(get_db)
):
    """Archive (soft delete) or restore an equipment/vehicle item."""
    user, barangay = _resolve_scope(request, db)
    if isinstance(user, RedirectResponse):
        return user
    if not barangay:
        return RedirectResponse(
            url="/bdrrmo/equipment?error=No+barangay+is+assigned+to+your+account",
            status_code=302,
        )
    item = _own_equipment(db, barangay, equipment_id)
    if not item:
        return RedirectResponse(
            url="/bdrrmo/equipment?error=Equipment+not+found", status_code=302
        )

    item.is_archived = not bool(item.is_archived)
    item.updated_by = user["id"]
    db.commit()
    verb = "archived" if item.is_archived else "restored"
    log_action(
        db, user["id"], verb, "barangay_equipment", item.id,
        f"BDRRMO {verb} equipment '{item.name}' in {barangay.name}",
    )
    dest = "/bdrrmo/equipment?archived=1" if item.is_archived else "/bdrrmo/equipment"
    sep = "&" if "?" in dest else "?"
    return RedirectResponse(
        url=f"{dest}{sep}success=Equipment+{verb}", status_code=302
    )


# ══════════════════════════════════════════════════════════════════════
# POPULATION RECORDS (barangay-scoped, append-only logging)
# Each submission creates a NEW Population row — no edit/delete. History
# is kept intact. recorded_by = current user, barangay_id = own barangay.
# ══════════════════════════════════════════════════════════════════════

@router.get("/population", response_class=HTMLResponse)
def population(
    request: Request,
    db: Session = Depends(get_db),
    page: Optional[str] = None,
    per_page: Optional[str] = None,
):
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

    page_obj = paginate(history, parse_page(page), parse_per_page(per_page))

    return templates.TemplateResponse(
        request=request,
        name="bdrrmo/population.html",
        context={
            "user": user,
            "active_nav": "bdrrmo_population",
            "barangay": barangay,
            "latest": latest,
            "history": page_obj.items,
            "page_obj": page_obj,
            "base_query": "",
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

    # Integrity guard: total population and households must be positive — a
    # zero/blank snapshot is never a valid census and usually signals an
    # accidental partial submission. Reject it before it becomes the latest.
    # (PWD/elderly/children may legitimately be 0.)
    if (total_population or 0) <= 0 or (total_households or 0) <= 0:
        return RedirectResponse(
            url="/bdrrmo/population?error=Total+population+and+total+households+must+be+greater+than+0.",
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
# CONTACT DIRECTORY — read-only, all barangays, grouped per barangay
# (shared across roles; see app/services/contact_directory.py). Distinct
# from /contacts below, which edits only this user's own barangay.
# ══════════════════════════════════════════════════════════════════════

@router.get("/directory", response_class=HTMLResponse)
def contact_directory(
    request: Request,
    db: Session = Depends(get_db),
    q: Optional[str] = None,
    brgy: Optional[str] = None,
    page: Optional[str] = None,
    per_page: Optional[str] = None,
):
    user = require_role(request, ["bdrrmo"])
    if isinstance(user, RedirectResponse):
        return user

    context = build_directory_context(
        db, q=q, brgy=brgy, page=page, per_page=per_page,
        directory_url="/bdrrmo/directory", active_nav="bdrrmo_directory",
    )
    context["user"] = user
    return templates.TemplateResponse(
        request=request, name="shared/contact_directory.html", context=context,
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
