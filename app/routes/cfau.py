from fastapi import APIRouter, Request, Depends, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.database import get_db
from app.auth import require_role
from app.models import (
    Equipment, EquipmentReport, Incident, IncidentReport,
    Barangay, EquipmentStatus, Urgency, ServiceabilityStatus,
    DisasterType, log_action,
    UploadedReport, UploadHistory, ReportStatus, FileType,
    LifecycleStatus, UploadEvent, add_upload_history,
)
from typing import Optional
from datetime import datetime, timezone, timedelta
import os
from urllib.parse import quote_plus

# Reuse the Week 6 ETL building blocks rather than rebuilding them.
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

router = APIRouter(prefix="/cfau")
templates = Jinja2Templates(directory="app/templates")

# Display UTC timestamps in Philippine Standard Time (UTC+8), matching admin.
_PHT = timezone(timedelta(hours=8))


def _to_pht(dt):
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_PHT).strftime('%B %d, %Y at %I:%M %p')


templates.env.filters['pht'] = _to_pht

# Only CFAU OIC and Admin reach these screens. CFAU manages their own
# reports; Admin has full access (consistent with the agreed RBAC).
CFAU_ROLES = ["cfau_oic", "admin"]

# ── Serviceability report vocabulary ──────────────────────────────────
# The serviceability *finding* reuses a subset of EquipmentStatus.
FINDING_CHOICES = ["serviceable", "under_repair", "unserviceable"]
FINDING_LABELS = {
    "serviceable": "Serviceable",
    "under_repair": "Under Repair",
    "unserviceable": "Unserviceable",
}
REPORT_TYPE_CHOICES = ["inspection", "maintenance", "serviceability"]
REPORT_TYPE_LABELS = {
    "inspection": "Inspection",
    "maintenance": "Maintenance Finding",
    "serviceability": "Serviceability Assessment",
}
URGENCY_CHOICES = [u.value for u in Urgency]
WORKFLOW_LABELS = {
    "draft": "Draft",
    "submitted": "Submitted",
    "reviewed": "Reviewed",
    "resolved": "Resolved",
}


def _is_admin(user) -> bool:
    return user["role"] == "admin"


# ══════════════════════════════════════════════════════════════════════
# DASHBOARD
# ══════════════════════════════════════════════════════════════════════

@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    user = require_role(request, ["cfau_oic"])
    if isinstance(user, RedirectResponse):
        return user

    # Quick counts for the user's own reports, by workflow status.
    my_serviceability = db.query(EquipmentReport).filter(
        EquipmentReport.reported_by == user["id"]
    ).all()
    my_incident = db.query(IncidentReport).filter(
        IncidentReport.submitted_by == user["id"]
    ).all()

    def _count(rows, status):
        return sum(1 for r in rows if r.report_status == status)

    return templates.TemplateResponse(
        request=request,
        name="cfau/dashboard.html",
        context={
            "user": user,
            "svc_total": len(my_serviceability),
            "svc_draft": _count(my_serviceability, ServiceabilityStatus.draft),
            "svc_submitted": _count(my_serviceability, ServiceabilityStatus.submitted),
            "svc_reviewed": _count(my_serviceability, ServiceabilityStatus.reviewed),
            "svc_resolved": _count(my_serviceability, ServiceabilityStatus.resolved),
            "inc_total": len(my_incident),
            "inc_draft": _count(my_incident, ServiceabilityStatus.draft),
            "inc_submitted": _count(my_incident, ServiceabilityStatus.submitted),
        },
    )


# ══════════════════════════════════════════════════════════════════════
# MODULE A & B — EQUIPMENT SERVICEABILITY REPORTS
# ══════════════════════════════════════════════════════════════════════

@router.get("/serviceability", response_class=HTMLResponse)
def serviceability_list(
    request: Request,
    db: Session = Depends(get_db),
    status: Optional[str] = None,
):
    user = require_role(request, CFAU_ROLES)
    if isinstance(user, RedirectResponse):
        return user

    query = db.query(EquipmentReport)
    # CFAU sees only their own reports; Admin sees all.
    if not _is_admin(user):
        query = query.filter(EquipmentReport.reported_by == user["id"])
    if status in {s.value for s in ServiceabilityStatus}:
        query = query.filter(EquipmentReport.report_status == ServiceabilityStatus(status))

    reports = query.order_by(EquipmentReport.reported_at.desc()).all()

    rows = []
    for r in reports:
        rows.append({
            "id": r.id,
            "title": r.title or "(untitled)",
            "equipment": r.equipment.name if r.equipment else "—",
            "report_type": REPORT_TYPE_LABELS.get(r.report_type, r.report_type or "—"),
            "finding": FINDING_LABELS.get(r.status.value if r.status else "", "—"),
            "finding_value": r.status.value if r.status else "",
            "urgency": (r.urgency.value if r.urgency else "moderate"),
            "report_status": r.report_status.value if r.report_status else "draft",
            "report_status_label": WORKFLOW_LABELS.get(
                r.report_status.value if r.report_status else "draft", "Draft"
            ),
            "reported_at": r.reported_at,
            "reporter": r.reported_by_user.username if r.reported_by_user else "—",
        })

    # Summary counts always reflect the full visible set (own / all).
    base = db.query(EquipmentReport)
    if not _is_admin(user):
        base = base.filter(EquipmentReport.reported_by == user["id"])
    all_visible = base.all()
    summary = {
        "total": len(all_visible),
        "draft": sum(1 for x in all_visible if x.report_status == ServiceabilityStatus.draft),
        "submitted": sum(1 for x in all_visible if x.report_status == ServiceabilityStatus.submitted),
        "reviewed": sum(1 for x in all_visible if x.report_status == ServiceabilityStatus.reviewed),
        "resolved": sum(1 for x in all_visible if x.report_status == ServiceabilityStatus.resolved),
    }

    return templates.TemplateResponse(
        request=request,
        name="cfau/serviceability_list.html",
        context={
            "user": user,
            "active_nav": "serviceability",
            "rows": rows,
            "summary": summary,
            "statuses": [(s.value, WORKFLOW_LABELS[s.value]) for s in ServiceabilityStatus],
            "f_status": status or "",
            "is_admin_view": _is_admin(user),
        },
    )


def _equipment_options(db):
    return db.query(Equipment).filter(
        Equipment.is_archived == False
    ).order_by(Equipment.name).all()


@router.get("/serviceability/new", response_class=HTMLResponse)
def serviceability_new_form(request: Request, db: Session = Depends(get_db)):
    user = require_role(request, CFAU_ROLES)
    if isinstance(user, RedirectResponse):
        return user
    return templates.TemplateResponse(
        request=request,
        name="cfau/serviceability_form.html",
        context={
            "user": user,
            "active_nav": "serviceability",
            "edit_mode": False,
            "target": None,
            "equipment": _equipment_options(db),
            "report_types": [(v, REPORT_TYPE_LABELS[v]) for v in REPORT_TYPE_CHOICES],
            "findings": [(v, FINDING_LABELS[v]) for v in FINDING_CHOICES],
            "urgencies": URGENCY_CHOICES,
            "error": None,
        },
    )


@router.post("/serviceability/new")
def serviceability_create(
    request: Request,
    db: Session = Depends(get_db),
    equipment_id: int = Form(...),
    title: str = Form(""),
    report_type: str = Form("inspection"),
    finding: str = Form("serviceable"),
    urgency: str = Form("moderate"),
    issue_description: str = Form(""),
    action: str = Form("draft"),   # "draft" or "submit"
):
    user = require_role(request, CFAU_ROLES)
    if isinstance(user, RedirectResponse):
        return user

    def render_error(msg):
        return templates.TemplateResponse(
            request=request,
            name="cfau/serviceability_form.html",
            context={
                "user": user,
                "active_nav": "serviceability",
                "edit_mode": False,
                "target": None,
                "equipment": _equipment_options(db),
                "report_types": [(v, REPORT_TYPE_LABELS[v]) for v in REPORT_TYPE_CHOICES],
                "findings": [(v, FINDING_LABELS[v]) for v in FINDING_CHOICES],
                "urgencies": URGENCY_CHOICES,
                "error": msg,
            },
        )

    equipment = db.query(Equipment).filter(Equipment.id == equipment_id).first()
    if not equipment:
        return render_error("Please select a valid equipment unit.")
    title = title.strip()
    if not title:
        return render_error("Report title is required.")
    if report_type not in REPORT_TYPE_CHOICES:
        report_type = "inspection"
    if finding not in FINDING_CHOICES:
        finding = "serviceable"
    if urgency not in {u.value for u in Urgency}:
        urgency = "moderate"

    submitting = (action == "submit")
    r = EquipmentReport(
        equipment_id=equipment.id,
        reported_by=user["id"],
        title=title,
        report_type=report_type,
        status=EquipmentStatus(finding),
        urgency=Urgency(urgency),
        issue_description=issue_description.strip() or None,
        report_status=ServiceabilityStatus.submitted if submitting else ServiceabilityStatus.draft,
        submitted_at=datetime.utcnow() if submitting else None,
    )
    db.add(r)
    db.commit()
    db.refresh(r)

    log_action(
        db, user["id"], "submitted" if submitting else "created",
        "equipment_reports", r.id,
        f"Serviceability report '{r.title}' for '{equipment.name}' "
        f"{'submitted for review' if submitting else 'saved as draft'} "
        f"(finding: {FINDING_LABELS[finding]})",
    )

    msg = "Report+submitted+for+review" if submitting else "Draft+saved"
    return RedirectResponse(
        url=f"/cfau/serviceability?success={msg}", status_code=302
    )


def _get_owned_report(db, report_id, user):
    """Fetch a report the current user is allowed to act on, else None.
    CFAU may only touch their own; Admin may touch any."""
    r = db.query(EquipmentReport).filter(EquipmentReport.id == report_id).first()
    if not r:
        return None
    if not _is_admin(user) and r.reported_by != user["id"]:
        return None
    return r


@router.get("/serviceability/{report_id}", response_class=HTMLResponse)
def serviceability_detail(report_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_role(request, CFAU_ROLES)
    if isinstance(user, RedirectResponse):
        return user
    r = _get_owned_report(db, report_id, user)
    if not r:
        return RedirectResponse(url="/cfau/serviceability", status_code=302)

    return templates.TemplateResponse(
        request=request,
        name="cfau/serviceability_detail.html",
        context={
            "user": user,
            "active_nav": "serviceability",
            "r": r,
            "finding_label": FINDING_LABELS.get(r.status.value if r.status else "", "—"),
            "report_type_label": REPORT_TYPE_LABELS.get(r.report_type, r.report_type or "—"),
            "workflow_label": WORKFLOW_LABELS.get(
                r.report_status.value if r.report_status else "draft", "Draft"
            ),
            "can_edit": (r.report_status == ServiceabilityStatus.draft)
                        and (_is_admin(user) or r.reported_by == user["id"]),
        },
    )


@router.get("/serviceability/{report_id}/edit", response_class=HTMLResponse)
def serviceability_edit_form(report_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_role(request, CFAU_ROLES)
    if isinstance(user, RedirectResponse):
        return user
    r = _get_owned_report(db, report_id, user)
    if not r:
        return RedirectResponse(url="/cfau/serviceability", status_code=302)
    # Only drafts are editable — submitted/reviewed/resolved reports are locked.
    if r.report_status != ServiceabilityStatus.draft:
        return RedirectResponse(
            url=f"/cfau/serviceability/{report_id}?error=Only+drafts+can+be+edited",
            status_code=302,
        )
    return templates.TemplateResponse(
        request=request,
        name="cfau/serviceability_form.html",
        context={
            "user": user,
            "active_nav": "serviceability",
            "edit_mode": True,
            "target": r,
            "equipment": _equipment_options(db),
            "report_types": [(v, REPORT_TYPE_LABELS[v]) for v in REPORT_TYPE_CHOICES],
            "findings": [(v, FINDING_LABELS[v]) for v in FINDING_CHOICES],
            "urgencies": URGENCY_CHOICES,
            "error": None,
        },
    )


@router.post("/serviceability/{report_id}/edit")
def serviceability_edit(
    report_id: int,
    request: Request,
    db: Session = Depends(get_db),
    equipment_id: int = Form(...),
    title: str = Form(""),
    report_type: str = Form("inspection"),
    finding: str = Form("serviceable"),
    urgency: str = Form("moderate"),
    issue_description: str = Form(""),
    action: str = Form("draft"),
):
    user = require_role(request, CFAU_ROLES)
    if isinstance(user, RedirectResponse):
        return user
    r = _get_owned_report(db, report_id, user)
    if not r:
        return RedirectResponse(url="/cfau/serviceability", status_code=302)
    if r.report_status != ServiceabilityStatus.draft:
        return RedirectResponse(
            url=f"/cfau/serviceability/{report_id}?error=Only+drafts+can+be+edited",
            status_code=302,
        )

    equipment = db.query(Equipment).filter(Equipment.id == equipment_id).first()
    title = title.strip()
    if equipment:
        r.equipment_id = equipment.id
    if title:
        r.title = title
    if report_type in REPORT_TYPE_CHOICES:
        r.report_type = report_type
    if finding in FINDING_CHOICES:
        r.status = EquipmentStatus(finding)
    if urgency in {u.value for u in Urgency}:
        r.urgency = Urgency(urgency)
    r.issue_description = issue_description.strip() or None

    submitting = (action == "submit")
    if submitting:
        r.report_status = ServiceabilityStatus.submitted
        r.submitted_at = datetime.utcnow()

    db.commit()

    log_action(
        db, user["id"], "submitted" if submitting else "edited",
        "equipment_reports", r.id,
        f"Serviceability report '{r.title}' "
        f"{'submitted for review' if submitting else 'draft edited'}",
    )

    msg = "Report+submitted+for+review" if submitting else "Draft+updated"
    return RedirectResponse(
        url=f"/cfau/serviceability?success={msg}", status_code=302
    )


@router.post("/serviceability/{report_id}/submit")
def serviceability_submit(report_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_role(request, CFAU_ROLES)
    if isinstance(user, RedirectResponse):
        return user
    r = _get_owned_report(db, report_id, user)
    if not r:
        return RedirectResponse(url="/cfau/serviceability", status_code=302)
    if r.report_status != ServiceabilityStatus.draft:
        return RedirectResponse(
            url=f"/cfau/serviceability?error=Only+drafts+can+be+submitted",
            status_code=302,
        )

    r.report_status = ServiceabilityStatus.submitted
    r.submitted_at = datetime.utcnow()
    db.commit()

    log_action(
        db, user["id"], "submitted", "equipment_reports", r.id,
        f"Serviceability report '{r.title}' submitted for review",
    )
    return RedirectResponse(
        url="/cfau/serviceability?success=Report+submitted+for+review",
        status_code=302,
    )


# ══════════════════════════════════════════════════════════════════════
# MODULE C — POST-INCIDENT REPORTS
# ══════════════════════════════════════════════════════════════════════

@router.get("/incident-reports", response_class=HTMLResponse)
def incident_report_list(
    request: Request,
    db: Session = Depends(get_db),
    status: Optional[str] = None,
):
    user = require_role(request, CFAU_ROLES)
    if isinstance(user, RedirectResponse):
        return user

    query = db.query(IncidentReport)
    if not _is_admin(user):
        query = query.filter(IncidentReport.submitted_by == user["id"])
    if status in {s.value for s in ServiceabilityStatus}:
        query = query.filter(IncidentReport.report_status == ServiceabilityStatus(status))

    reports = query.order_by(IncidentReport.created_at.desc()).all()

    rows = []
    for r in reports:
        inc = r.incident
        rows.append({
            "id": r.id,
            "disaster_type": (inc.disaster_type.value.replace("_", " ").title()
                              if inc and inc.disaster_type else "—"),
            "barangay": inc.barangay.name if (inc and inc.barangay) else "—",
            "date_occurred": inc.date_occurred if inc else None,
            "personnel_count": r.personnel_count or 0,
            "report_status": r.report_status.value if r.report_status else "draft",
            "report_status_label": WORKFLOW_LABELS.get(
                r.report_status.value if r.report_status else "draft", "Draft"
            ),
            "created_at": r.created_at,
            "reporter": r.submitted_by_user.username if r.submitted_by_user else "—",
        })

    return templates.TemplateResponse(
        request=request,
        name="cfau/incident_report_list.html",
        context={
            "user": user,
            "active_nav": "incident_reports",
            "rows": rows,
            # Only draft/submitted are meaningful for post-incident reports.
            "statuses": [("draft", "Draft"), ("submitted", "Submitted")],
            "f_status": status or "",
            "is_admin_view": _is_admin(user),
        },
    )


def _incident_options(db):
    return (
        db.query(Incident)
        .order_by(Incident.date_occurred.desc())
        .limit(200)
        .all()
    )


def _incident_label(inc) -> str:
    dtype = inc.disaster_type.value.replace("_", " ").title() if inc.disaster_type else "Incident"
    brgy = inc.barangay.name if inc.barangay else "Unknown barangay"
    when = inc.date_occurred.strftime('%Y-%m-%d') if inc.date_occurred else "—"
    return f"{dtype} — {brgy} ({when})"


@router.get("/incident-reports/new", response_class=HTMLResponse)
def incident_report_new_form(request: Request, db: Session = Depends(get_db)):
    user = require_role(request, CFAU_ROLES)
    if isinstance(user, RedirectResponse):
        return user
    incidents = _incident_options(db)
    return templates.TemplateResponse(
        request=request,
        name="cfau/incident_report_form.html",
        context={
            "user": user,
            "active_nav": "incident_reports",
            "edit_mode": False,
            "target": None,
            "incidents": [(i.id, _incident_label(i)) for i in incidents],
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
    user = require_role(request, CFAU_ROLES)
    if isinstance(user, RedirectResponse):
        return user

    def render_error(msg):
        return templates.TemplateResponse(
            request=request,
            name="cfau/incident_report_form.html",
            context={
                "user": user,
                "active_nav": "incident_reports",
                "edit_mode": False,
                "target": None,
                "incidents": [(i.id, _incident_label(i)) for i in _incident_options(db)],
                "error": msg,
            },
        )

    incident = db.query(Incident).filter(Incident.id == incident_id).first()
    if not incident:
        return render_error("Please select the disaster incident this report covers.")

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
        f"Post-incident report for {_incident_label(incident)} "
        f"{'submitted' if submitting else 'saved as draft'}",
    )

    msg = "Report+submitted" if submitting else "Draft+saved"
    return RedirectResponse(url=f"/cfau/incident-reports?success={msg}", status_code=302)


# ══════════════════════════════════════════════════════════════════════
# MODULE C — UPLOAD PATH (Week 8.1)
# Reuses the Week 6 ETL pipeline (Bronze storage + extraction + AI summary
# + UploadedReport lifecycle + UploadHistory + AuditLog). Post-incident
# uploads are tagged in extracted_data JSON (no new column) and converted
# into the SAME IncidentReport model as the manual path via an assisted
# review screen. These routes are declared BEFORE /incident-reports/{id}
# so the literal "upload" segment is matched first.
# ══════════════════════════════════════════════════════════════════════

UPLOAD_KIND_POST_INCIDENT = "post_incident"


def _is_post_incident_upload(report: "UploadedReport") -> bool:
    data = report.extracted_data or {}
    return data.get("report_kind") == UPLOAD_KIND_POST_INCIDENT


def _get_owned_upload(db, report_id, user):
    """A post-incident upload the current user may act on (own / admin)."""
    r = db.query(UploadedReport).filter(UploadedReport.id == report_id).first()
    if not r or not _is_post_incident_upload(r):
        return None
    if not _is_admin(user) and r.uploaded_by != user["id"]:
        return None
    return r


@router.get("/incident-reports/upload", response_class=HTMLResponse)
def incident_upload_form(request: Request, db: Session = Depends(get_db)):
    user = require_role(request, CFAU_ROLES)
    if isinstance(user, RedirectResponse):
        return user
    return templates.TemplateResponse(
        request=request,
        name="cfau/incident_upload_form.html",
        context={
            "user": user,
            "active_nav": "incident_reports",
            "error": request.query_params.get("error"),
            "ai_available": ai_available(),
        },
    )


@router.post("/incident-reports/upload")
async def incident_upload_submit(
    request: Request,
    db: Session = Depends(get_db),
    file: UploadFile = File(...),
):
    user = require_role(request, CFAU_ROLES)
    if isinstance(user, RedirectResponse):
        return user

    original_name = file.filename or "report"
    ext = _ext_of(original_name)
    if ext not in ALLOWED_EXTS:
        return RedirectResponse(
            url="/cfau/incident-reports/upload?error=Unsupported+file+type.+Allowed:+PDF,+XLSX,+XLS,+CSV.",
            status_code=302,
        )

    os.makedirs(UPLOAD_SUBDIR, exist_ok=True)
    stored_name = _safe_filename(original_name)
    stored_path = os.path.join(UPLOAD_SUBDIR, stored_name)

    ok, err = await save_validated_upload(file, ext, stored_path)
    if not ok:
        return RedirectResponse(
            url="/cfau/incident-reports/upload?error=" + quote_plus(err),
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
        new_value=f"CFAU uploaded post-incident document '{original_name}' "
                  f"({file_type_enum.value.upper()})",
    )
    db.commit()

    # ── Silver: extract raw text/rows + pre-fill the assisted report form ──
    # Tagged as a post-incident upload in JSON (no schema change).
    # report_fields → CFAU operational fields (IncidentReport); core_fields
    # → the incident triple used to strict-match an existing Incident.
    extracted = {
        "report_kind": UPLOAD_KIND_POST_INCIDENT,
        "raw_text": "", "rows": [], "columns": [], "error": None,
        "report_fields": empty_incident_report_fields(),
        "core_fields": {
            "barangay": "", "disaster_type": "", "date_occurred": "",
            "affected_families": 0, "casualties": 0, "description": "",
        },
        "matched_incident_id": None,
    }
    try:
        if file_type_enum == FileType.pdf:
            out = extract_pdf(stored_path)
            extracted["raw_text"] = out.get("text", "")
            # Free narrative — derive the incident triple for matching, and
            # pre-fill operational fields from any 'Label:' sections present.
            # operations_summary still falls back to the AI summary / raw text
            # below when no 'Operations Summary:' label is found.
            known_barangays = [b.name for b in db.query(Barangay).all()]
            core_row = structure_text(extracted["raw_text"], known_barangays)
            extracted["core_fields"] = {
                k: core_row.get(k, "") for k in extracted["core_fields"]
            }
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
                extracted["core_fields"] = {
                    k: core_rows[0].get(k, "") for k in extracted["core_fields"]
                }
        report.status = ReportStatus.reviewed
    except Exception as e:
        extracted["error"] = str(e)
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

    # Pre-fill operations_summary when no structured value was found — prefer
    # the AI summary, else a trimmed copy of the raw text. This keeps PDF
    # narrative uploads from arriving completely blank.
    if not extracted["report_fields"].get("operations_summary"):
        if ai_text:
            extracted["report_fields"]["operations_summary"] = ai_text
        elif extracted.get("raw_text"):
            extracted["report_fields"]["operations_summary"] = \
                extracted["raw_text"].strip()[:2000]

    # Strict incident match (barangay + disaster_type + date_occurred).
    core = extracted["core_fields"]
    matched = find_matching_incident(
        db, core.get("barangay"), core.get("disaster_type"),
        core.get("date_occurred"),
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
        url=f"/cfau/incident-reports/upload/{report.id}/review", status_code=302
    )


@router.get("/incident-reports/upload/{report_id}/review", response_class=HTMLResponse)
def incident_upload_review(report_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_role(request, CFAU_ROLES)
    if isinstance(user, RedirectResponse):
        return user
    report = _get_owned_upload(db, report_id, user)
    if not report:
        return RedirectResponse(url="/cfau/incident-reports", status_code=302)

    data = report.extracted_data or {}
    produced_id = data.get("produced_incident_report_id")
    # Already converted — send the user to the produced report.
    if produced_id:
        return RedirectResponse(
            url=f"/cfau/incident-reports/{produced_id}", status_code=302
        )

    # Strict-match result: only pre-select if the matched incident still
    # exists (it may have been deleted since extraction).
    matched_id = data.get("matched_incident_id")
    matched_incident = None
    if matched_id:
        matched_incident = db.query(Incident).filter(Incident.id == matched_id).first()
    selected_incident_id = matched_incident.id if matched_incident else None

    return templates.TemplateResponse(
        request=request,
        name="cfau/incident_upload_review.html",
        context={
            "user": user,
            "active_nav": "incident_reports",
            "report": report,
            "incidents": [(i.id, _incident_label(i)) for i in _incident_options(db)],
            "ai_summary": report.ai_summary,
            "ai_available": ai_available(),
            "raw_text_preview": (data.get("raw_text") or "")[:4000],
            "rows_preview": (data.get("rows") or [])[:10],
            "columns_preview": data.get("columns") or [],
            "extraction_error": data.get("error"),
            "error": request.query_params.get("error"),
            # Pre-fill: operational fields + the matched incident selection.
            "prefill": data.get("report_fields") or empty_incident_report_fields(),
            "selected_incident_id": selected_incident_id,
            "matched_incident_label": _incident_label(matched_incident) if matched_incident else None,
            # Core incident fields — editable so an Incident can be auto-created
            # (resolve-or-create) when no existing match is selected.
            "core": data.get("core_fields") or {
                "barangay": "", "disaster_type": "", "date_occurred": "",
                "affected_families": 0, "casualties": 0, "description": "",
            },
            "barangays": db.query(Barangay).order_by(Barangay.name).all(),
            "disaster_types": [dt.value for dt in DisasterType],
        },
    )


@router.get("/incident-reports/upload/{report_id}/file")
def incident_upload_file(report_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_role(request, CFAU_ROLES)
    if isinstance(user, RedirectResponse):
        return user
    report = _get_owned_upload(db, report_id, user)
    if not report or not report.file_path or not os.path.exists(report.file_path):
        return RedirectResponse(url="/cfau/incident-reports", status_code=302)
    return FileResponse(report.file_path, filename=report.file_name)


@router.post("/incident-reports/upload/{report_id}/submit")
def incident_upload_convert(
    report_id: int,
    request: Request,
    db: Session = Depends(get_db),
    # Optional: a manually selected existing incident takes precedence.
    # Empty/blank means "resolve-or-create from the core fields below".
    incident_id: Optional[str] = Form(None),
    # Core incident fields — used to strict-match or auto-create an Incident.
    barangay: str = Form(""),
    disaster_type: str = Form(""),
    date_occurred: str = Form(""),
    affected_families: int = Form(0),
    casualties: int = Form(0),
    incident_description: str = Form(""),
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
    user = require_role(request, CFAU_ROLES)
    if isinstance(user, RedirectResponse):
        return user
    report = _get_owned_upload(db, report_id, user)
    if not report:
        return RedirectResponse(url="/cfau/incident-reports", status_code=302)

    # Personnel deployed is optional: blank/invalid → NULL (not 0).
    _pc = (personnel_count or "").strip()
    personnel_value = max(0, int(_pc)) if _pc.isdigit() else None

    data = dict(report.extracted_data or {})
    if data.get("produced_incident_report_id"):
        # Idempotency guard — already converted.
        return RedirectResponse(
            url=f"/cfau/incident-reports/{data['produced_incident_report_id']}",
            status_code=302,
        )

    def _review_error(msg: str):
        return RedirectResponse(
            url=f"/cfau/incident-reports/upload/{report.id}/review?error={quote_plus(msg)}",
            status_code=302,
        )

    # ── Resolve the Incident this report attaches to ──────────────────
    # 1) A manually selected incident always wins.
    # 2) Otherwise resolve-or-create from the core fields: strict match an
    #    existing Incident, else auto-create one (incidents emerge from
    #    uploads). resolve_or_create_incident never mutates a matched
    #    Incident — canonical impact data is preserved.
    incident = None
    incident_created = False
    selected_id = (incident_id or "").strip()
    if selected_id:
        try:
            incident = db.query(Incident).filter(Incident.id == int(selected_id)).first()
        except ValueError:
            incident = None
        if not incident:
            return _review_error("Selected incident could not be found. Please choose again.")
    else:
        try:
            incident, incident_created = resolve_or_create_incident(
                db, user["id"],
                core={
                    "barangay": barangay,
                    "disaster_type": disaster_type,
                    "date_occurred": date_occurred,
                    "affected_families": affected_families,
                    "casualties": casualties,
                    "description": incident_description,
                },
                source=f"cfau_upload:{report.id}",
            )
        except ValueError as e:
            return _review_error(str(e))

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

    # Link the produced report back to the upload (JSON, Option A) and close
    # the upload lifecycle as confirmed (= converted). Provenance records the
    # resolved Incident and whether this upload created it.
    data["produced_incident_report_id"] = r.id
    data["linked_incident_id"] = incident.id
    data["incident_created"] = incident_created
    report.extracted_data = data
    # First-class contribution linkage (mirrors linked_incident_id JSON).
    report.incident_id = incident.id
    report.lifecycle_status = LifecycleStatus.confirmed
    report.status = ReportStatus.confirmed
    db.commit()

    # Audit the canonical Incident when this upload created it.
    if incident_created:
        log_action(
            db, user["id"], "created", "incidents", incident.id,
            f"Incident auto-created from CFAU upload '{report.file_name}' — "
            f"{_incident_label(incident)} (no existing match).",
        )

    _link_note = (f"new incident #{incident.id} auto-created" if incident_created
                  else f"linked to existing incident #{incident.id}")
    add_upload_history(
        db, report_id=report.id, user_id=user["id"],
        event_type=UploadEvent.confirmed,
        new_value=f"Converted to post-incident report #{r.id} "
                  f"({'submitted' if submitting else 'draft'}) — {_link_note}.",
    )
    db.commit()

    # Two audit entries: the upload conversion + the report creation —
    # mirroring the manual path's create/submit log.
    log_action(
        db, user["id"], "converted", "uploaded_reports", report.id,
        f"CFAU converted upload '{report.file_name}' to post-incident report #{r.id}",
    )
    log_action(
        db, user["id"], "submitted" if submitting else "created",
        "incident_reports", r.id,
        f"Post-incident report for {_incident_label(incident)} "
        f"{'submitted' if submitting else 'saved as draft'} (from upload #{report.id})",
    )

    msg = "Report+submitted+from+upload" if submitting else "Draft+saved+from+upload"
    return RedirectResponse(url=f"/cfau/incident-reports?success={msg}", status_code=302)


def _get_owned_incident_report(db, report_id, user):
    r = db.query(IncidentReport).filter(IncidentReport.id == report_id).first()
    if not r:
        return None
    if not _is_admin(user) and r.submitted_by != user["id"]:
        return None
    return r


def _find_source_upload(db, incident_report_id):
    """Reverse-lookup the upload that produced this report, using the
    existing JSON linkage (no schema change). Returns the UploadedReport
    or None. Scans only post-incident uploads — small at this scale."""
    candidates = (
        db.query(UploadedReport)
        .filter(UploadedReport.extracted_data.isnot(None))
        .all()
    )
    for up in candidates:
        data = up.extracted_data or {}
        if (data.get("report_kind") == UPLOAD_KIND_POST_INCIDENT
                and data.get("produced_incident_report_id") == incident_report_id):
            return up
    return None


@router.get("/incident-reports/{report_id}", response_class=HTMLResponse)
def incident_report_detail(report_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_role(request, CFAU_ROLES)
    if isinstance(user, RedirectResponse):
        return user
    r = _get_owned_incident_report(db, report_id, user)
    if not r:
        return RedirectResponse(url="/cfau/incident-reports", status_code=302)
    inc = r.incident
    return templates.TemplateResponse(
        request=request,
        name="cfau/incident_report_detail.html",
        context={
            "user": user,
            "active_nav": "incident_reports",
            "r": r,
            "incident": inc,
            "disaster_type": (inc.disaster_type.value.replace("_", " ").title()
                              if inc and inc.disaster_type else "—"),
            "barangay": inc.barangay.name if (inc and inc.barangay) else "—",
            "workflow_label": WORKFLOW_LABELS.get(
                r.report_status.value if r.report_status else "draft", "Draft"
            ),
            "can_edit": (r.report_status == ServiceabilityStatus.draft)
                        and (_is_admin(user) or r.submitted_by == user["id"]),
            "source_upload": _find_source_upload(db, r.id),
        },
    )


@router.get("/incident-reports/{report_id}/edit", response_class=HTMLResponse)
def incident_report_edit_form(report_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_role(request, CFAU_ROLES)
    if isinstance(user, RedirectResponse):
        return user
    r = _get_owned_incident_report(db, report_id, user)
    if not r:
        return RedirectResponse(url="/cfau/incident-reports", status_code=302)
    if r.report_status != ServiceabilityStatus.draft:
        return RedirectResponse(
            url=f"/cfau/incident-reports/{report_id}?error=Only+drafts+can+be+edited",
            status_code=302,
        )
    return templates.TemplateResponse(
        request=request,
        name="cfau/incident_report_form.html",
        context={
            "user": user,
            "active_nav": "incident_reports",
            "edit_mode": True,
            "target": r,
            "incidents": [(i.id, _incident_label(i)) for i in _incident_options(db)],
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
    user = require_role(request, CFAU_ROLES)
    if isinstance(user, RedirectResponse):
        return user
    r = _get_owned_incident_report(db, report_id, user)
    if not r:
        return RedirectResponse(url="/cfau/incident-reports", status_code=302)
    if r.report_status != ServiceabilityStatus.draft:
        return RedirectResponse(
            url=f"/cfau/incident-reports/{report_id}?error=Only+drafts+can+be+edited",
            status_code=302,
        )

    incident = db.query(Incident).filter(Incident.id == incident_id).first()
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
        f"Post-incident report #{r.id} "
        f"{'submitted' if submitting else 'draft edited'}",
    )

    msg = "Report+submitted" if submitting else "Draft+updated"
    return RedirectResponse(url=f"/cfau/incident-reports?success={msg}", status_code=302)


@router.post("/incident-reports/{report_id}/submit")
def incident_report_submit(report_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_role(request, CFAU_ROLES)
    if isinstance(user, RedirectResponse):
        return user
    r = _get_owned_incident_report(db, report_id, user)
    if not r:
        return RedirectResponse(url="/cfau/incident-reports", status_code=302)
    if r.report_status != ServiceabilityStatus.draft:
        return RedirectResponse(
            url="/cfau/incident-reports?error=Only+drafts+can+be+submitted",
            status_code=302,
        )

    r.report_status = ServiceabilityStatus.submitted
    r.submitted_at = datetime.utcnow()
    db.commit()

    log_action(
        db, user["id"], "submitted", "incident_reports", r.id,
        f"Post-incident report #{r.id} submitted",
    )
    return RedirectResponse(
        url="/cfau/incident-reports?success=Report+submitted", status_code=302
    )
