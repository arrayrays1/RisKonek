from fastapi import APIRouter, Request, Depends, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from datetime import datetime, date, timezone, timedelta
from urllib.parse import quote_plus
import os
from app.database import get_db
from app.models import (
    Barangay, Incident, Facility, Population, log_action,
    DisasterType, Severity, FacilityType, FacilityStatus,
    UploadedReport, ReportStatus, FileType, LifecycleStatus, UploadEvent,
    add_upload_history, BarangayEquipment, EquipmentType,
)
from app.auth import require_role, require_barangay_access
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
from app.etl.structure import structure_text, structure_rows
from app.etl.ai_pipeline import summarize as ai_summarize, is_available as ai_available

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
