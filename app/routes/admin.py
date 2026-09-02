from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.database import get_db
from app.models import (
    User, UserRole, Barangay, AuditLog, Incident,
    Resource, Equipment, Population, EquipmentStatus,
    DisasterType, RiskLevel, Facility, FacilityType, FacilityStatus,
    UploadedReport, UploadHistory, UploadEvent,
    ResourceCategory, EquipmentType, log_action,
    EquipmentReport, IncidentReport, ServiceabilityStatus, Urgency,
)
from app.auth import require_role, hash_password
from app.analytics.simulator import compute_risk_score
from app.utils.geo import BARANGAY_COORDS
from app.services.geocoding import (
    nearby_landmarks, search_locations, reverse_geocode_san_pedro,
    get_san_pedro_bounds, get_san_pedro_boundary, is_within_san_pedro,
)
from app.services.facility_details import (
    parse_coord as _parse_coord,
    find_duplicate_facility as _find_duplicate,
    validate_name as _validate_facility_name,
    validate_address as _validate_facility_address,
    validate_facility_details,
    FACILITY_CLASSIFICATIONS,
    EO_MOA_MOU_STATUSES,
)
from app.services.contact_directory import build_directory_context
from app.services import global_search
from app.utils.pagination import (
    paginate, parse_per_page, parse_page, build_base_query,
)
from typing import Optional
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote_plus
import csv
import io
import re

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory="app/templates")

# Audit logs are stored in UTC (SQLite func.now() / datetime.utcnow()).
# Display in Philippine Standard Time (UTC+8).
_PHT = timezone(timedelta(hours=8))

def _to_pht(dt):
    """Jinja filter: convert a UTC-naive or aware datetime to PHT and format."""
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_PHT).strftime('%B %d, %Y at %I:%M %p')

templates.env.filters['pht'] = _to_pht


# ─────────────────────────────────────────────────────────────────────
# DASHBOARD
# ─────────────────────────────────────────────────────────────────────

@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    user = require_role(request, ["admin"])
    if isinstance(user, RedirectResponse):
        return user

    # ── Stat cards ───────────────────────────────────────────────────
    total_barangays = db.query(Barangay).count()
    total_population = db.query(
        func.sum(Population.total_population)
    ).scalar() or 0

    total_equip = db.query(Equipment).count()
    serviceable = db.query(Equipment).filter(
        Equipment.status == EquipmentStatus.serviceable
    ).count()
    equip_ratio = (serviceable / total_equip * 100) if total_equip > 0 else 0

    resources = db.query(Resource).filter(Resource.is_archived == False).all()
    adequate = sum(1 for r in resources if r.quantity >= r.restock_threshold)
    resource_ratio = (adequate / len(resources) * 100) if resources else 0
    readiness_score = round((equip_ratio * 0.5) + (resource_ratio * 0.5))

    six_months_ago = date.today() - timedelta(days=180)
    recent_pops = db.query(
        func.count(func.distinct(Population.barangay_id))
    ).filter(
        Population.recorded_at >= six_months_ago
    ).scalar() or 0
    data_relevance = round((recent_pops / total_barangays * 100)) if total_barangays > 0 else 0

    low_stock = [r for r in resources if r.quantity < r.restock_threshold]
    expiring = [
        r for r in resources
        if r.expiry_date and r.expiry_date <= date.today() + timedelta(days=30)
    ]
    # Distinct assets with an overdue / due-today repair reminder.
    repair_attention_count = len(assets_needing_repair_attention(db))
    active_alerts = len(low_stock) + len(expiring) + repair_attention_count

    # ── Charts ───────────────────────────────────────────────────────
    disaster_counts = {}
    for dtype in DisasterType:
        count = db.query(Incident).filter(
            Incident.disaster_type == dtype
        ).count()
        disaster_counts[dtype.value] = count

    current_year = date.today().year
    yearly_data = {}
    for y in range(current_year - 5, current_year + 1):
        count = db.query(Incident).filter(
            func.strftime('%Y', Incident.date_occurred) == str(y)
        ).count()
        yearly_data[str(y)] = count

    # ── Barangay risk scores ─────────────────────────────────────────
    all_barangays = db.query(Barangay).all()
    barangay_scores = []
    for brgy in all_barangays:
        incidents = brgy.incidents
        population = db.query(Population).filter(
            Population.barangay_id == brgy.id
        ).order_by(Population.recorded_at.desc()).first()
        result = compute_risk_score(brgy, incidents, population)
        barangay_scores.append({
            "id": brgy.id,
            "name": brgy.name,
            "score": result["score"],
            "level": result["level"].value,
            "hazard_types": brgy.hazard_types or "",
            "population": population.total_population if population else 0,
        })

    barangay_scores.sort(key=lambda x: x["score"], reverse=True)
    top5 = barangay_scores[:5]
    all_scores = barangay_scores

    # ── Map markers ──────────────────────────────────────────────────
    map_markers = []
    for b in barangay_scores:
        coords = BARANGAY_COORDS.get(b["name"])
        if coords:
            map_markers.append({
                "name": b["name"],
                "lat": coords["lat"],
                "lng": coords["lng"],
                "score": b["score"],
                "level": b["level"],
            })

    # ── Recent activity feed ─────────────────────────────────────────
    recent_logs = db.query(AuditLog).order_by(
        AuditLog.timestamp.desc()
    ).limit(5).all()

    return templates.TemplateResponse(
        request=request,
        name="admin/dashboard.html",
        context={
            "user": user,
            "total_barangays": total_barangays,
            "total_population": f"{total_population:,}",
            "readiness_score": readiness_score,
            "data_relevance": data_relevance,
            "active_alerts": active_alerts,
            # Pass raw objects; the template renders them with the |tojson
            # filter, which HTML-escapes <, >, & so values can't break out of
            # the inline <script> block (XSS-safe).
            "disaster_counts": disaster_counts,
            "yearly_data": yearly_data,
            "top5": top5,
            "all_scores": all_scores,
            "map_markers": map_markers,
            "recent_logs": recent_logs,
            "serviceable_count": serviceable,
            "total_equip": total_equip,
            "low_stock_count": len(low_stock),
            "expiring_count": len(expiring),
            "repair_attention_count": repair_attention_count,
        }
    )


# ─────────────────────────────────────────────────────────────────────
# USER MANAGEMENT
# ─────────────────────────────────────────────────────────────────────

@router.get("/users", response_class=HTMLResponse)
def user_list(
    request: Request,
    db: Session = Depends(get_db),
    q: Optional[str] = None,
    role: Optional[str] = None,
    status: Optional[str] = None,
    page: Optional[str] = None,
    per_page: Optional[str] = None,
):
    user = require_role(request, ["admin"])
    if isinstance(user, RedirectResponse):
        return user

    query = db.query(User)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(
            (User.username.ilike(like)) | (User.email.ilike(like))
        )
    if role and role in {r.value for r in UserRole}:
        query = query.filter(User.role == UserRole(role))
    if status == "active":
        query = query.filter(User.is_active == True)
    elif status == "inactive":
        query = query.filter(User.is_active == False)

    all_users = query.order_by(User.created_at.desc()).all()
    page_obj = paginate(all_users, parse_page(page), parse_per_page(per_page))
    base_query = build_base_query({
        "q": q or "", "role": role or "", "status": status or "",
    })

    return templates.TemplateResponse(
        request=request,
        name="admin/users.html",
        context={
            "user": user,
            "all_users": page_obj.items,
            "page_obj": page_obj,
            "base_query": base_query,
            "roles": [r.value for r in UserRole],
            "f_q": q or "",
            "f_role": role or "",
            "f_status": status or "",
        }
    )


@router.get("/users/create", response_class=HTMLResponse)
def create_user_form(request: Request, db: Session = Depends(get_db)):
    user = require_role(request, ["admin"])
    if isinstance(user, RedirectResponse):
        return user

    barangays = db.query(Barangay).order_by(Barangay.name).all()
    return templates.TemplateResponse(
        request=request,
        name="admin/user_form.html",
        context={
            "user": user,
            "barangays": barangays,
            "roles": [r.value for r in UserRole],
            "edit_mode": False,
            "target_user": None,
            "error": None
        }
    )


@router.post("/users/create")
async def create_user_submit(
    request: Request,
    db: Session = Depends(get_db),
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form(...),
    email: str = Form(""),
    contact_number: str = Form(""),
    barangay_id: Optional[int] = Form(None)
):
    user = require_role(request, ["admin"])
    if isinstance(user, RedirectResponse):
        return user

    barangays = db.query(Barangay).order_by(Barangay.name).all()
    username = username.strip()
    email_clean = email.strip() or None

    def render_error(msg):
        return templates.TemplateResponse(
            request=request,
            name="admin/user_form.html",
            context={
                "user": user,
                "barangays": barangays,
                "roles": [r.value for r in UserRole],
                "edit_mode": False,
                "target_user": None,
                "error": msg,
            }
        )

    if db.query(User).filter(User.username == username).first():
        return render_error(f"Username '{username}' is already taken.")

    if email_clean and db.query(User).filter(User.email == email_clean).first():
        return render_error(f"Email '{email_clean}' is already registered.")

    # Enforce barangay rule per the spec: only BDRRMO Chairpersons are tied
    # to a barangay; for any other role, barangay_id must be cleared.
    if role == UserRole.bdrrmo.value:
        if not barangay_id:
            return render_error("A barangay must be selected for the BDRRMO Chairperson role.")
        final_barangay_id = barangay_id
    else:
        final_barangay_id = None

    new_user = User(
        username=username,
        email=email_clean,
        password_hash=hash_password(password),
        role=UserRole(role),
        contact_number=contact_number,
        barangay_id=final_barangay_id,
        is_active=True
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    log = AuditLog(
        user_id=user["id"],
        action="created",
        target_table="users",
        target_id=new_user.id,
        description=f"Admin created new user: {username} with role: {role}"
    )
    db.add(log)
    db.commit()

    return RedirectResponse(url="/admin/users?success=User+created+successfully", status_code=302)


@router.get("/users/{user_id}/edit", response_class=HTMLResponse)
def edit_user_form(user_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_role(request, ["admin"])
    if isinstance(user, RedirectResponse):
        return user

    target_user = db.query(User).filter(User.id == user_id).first()
    if not target_user:
        return RedirectResponse(url="/admin/users", status_code=302)

    barangays = db.query(Barangay).order_by(Barangay.name).all()
    return templates.TemplateResponse(
        request=request,
        name="admin/user_form.html",
        context={
            "user": user,
            "barangays": barangays,
            "roles": [r.value for r in UserRole],
            "edit_mode": True,
            "target_user": target_user,
            "error": None
        }
    )


@router.post("/users/{user_id}/edit")
async def edit_user_submit(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    role: str = Form(...),
    email: str = Form(""),
    contact_number: str = Form(""),
    barangay_id: Optional[int] = Form(None),
    new_password: str = Form("")
):
    user = require_role(request, ["admin"])
    if isinstance(user, RedirectResponse):
        return user

    target_user = db.query(User).filter(User.id == user_id).first()
    if not target_user:
        return RedirectResponse(url="/admin/users", status_code=302)

    barangays = db.query(Barangay).order_by(Barangay.name).all()
    email_clean = email.strip() or None

    def render_error(msg):
        return templates.TemplateResponse(
            request=request,
            name="admin/user_form.html",
            context={
                "user": user,
                "barangays": barangays,
                "roles": [r.value for r in UserRole],
                "edit_mode": True,
                "target_user": target_user,
                "error": msg,
            }
        )

    if email_clean:
        clash = db.query(User).filter(
            User.email == email_clean, User.id != user_id
        ).first()
        if clash:
            return render_error(f"Email '{email_clean}' is already registered.")

    # Enforce barangay rule on the server too, not only in the UI.
    if role == UserRole.bdrrmo.value:
        if not barangay_id:
            return render_error("A barangay must be selected for the BDRRMO Chairperson role.")
        final_barangay_id = barangay_id
    else:
        final_barangay_id = None

    target_user.email = email_clean
    target_user.role = UserRole(role)
    target_user.contact_number = contact_number
    target_user.barangay_id = final_barangay_id

    if new_password.strip():
        target_user.password_hash = hash_password(new_password)

    db.commit()

    log = AuditLog(
        user_id=user["id"],
        action="updated",
        target_table="users",
        target_id=user_id,
        description=f"Admin updated user: {target_user.username} (role: {role})"
    )
    db.add(log)
    db.commit()

    return RedirectResponse(url="/admin/users?success=User+updated+successfully", status_code=302)


@router.post("/users/{user_id}/toggle")
def toggle_user_status(user_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_role(request, ["admin"])
    if isinstance(user, RedirectResponse):
        return user

    if user_id == user["id"]:
        return RedirectResponse(
            url="/admin/users?error=You+cannot+deactivate+your+own+account",
            status_code=302
        )

    target_user = db.query(User).filter(User.id == user_id).first()
    if not target_user:
        return RedirectResponse(url="/admin/users", status_code=302)

    target_user.is_active = not target_user.is_active
    db.commit()

    action = "activated" if target_user.is_active else "deactivated"

    log = AuditLog(
        user_id=user["id"],
        action=action,
        target_table="users",
        target_id=user_id,
        description=f"Admin {action} user: {target_user.username}"
    )
    db.add(log)
    db.commit()

    return RedirectResponse(
        url=f"/admin/users?success=User+{action}+successfully",
        status_code=302
    )


# ─────────────────────────────────────────────────────────────────────
# AUDIT TRAIL MODULE — admin-only system activity log
# Aggregates from AuditLog (the same source as the dashboard feed).
# ─────────────────────────────────────────────────────────────────────

AUDIT_CATEGORIES = [
    "Authentication", "User Management", "Uploads", "Incident Reports",
    "Barangay Data", "Resources", "Vehicle & Equipment", "System Actions",
]


def _audit_category(action: str, target_table: str) -> str:
    """Rule-based category for an audit entry, from its action + target
    table. Explainable and future-proof for tables not yet logged."""
    a = (action or "").lower()
    t = (target_table or "").lower()
    if t == "users":
        return "Authentication" if a in ("login", "logout") else "User Management"
    if t == "uploaded_reports":
        return "Uploads"
    if t in ("incidents", "incident_reports"):
        return "Incident Reports"
    if t in ("barangays", "populations", "facilities"):
        return "Barangay Data"
    if t == "resources":
        return "Resources"
    if t in ("equipment", "equipment_reports"):
        return "Vehicle & Equipment"
    return "System Actions"


def _parse_audit_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d")
    except ValueError:
        return None


def _audit_day_label(dt) -> str:
    """PHT date heading used to group the trail chronologically."""
    if dt is None:
        return "Unknown date"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_PHT).strftime("%B %d, %Y")


def _audit_filtered_rows(db, q, uid, action, category, date_from, date_to):
    """Apply the audit-trail filters and return matching rows, newest-first.
    Shared by the list view and the export endpoint so both see the same set."""
    query = db.query(AuditLog).outerjoin(User, AuditLog.user_id == User.id)

    if uid:
        query = query.filter(AuditLog.user_id == uid)
    if action:
        query = query.filter(AuditLog.action == action)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(
            (AuditLog.description.ilike(like)) | (User.username.ilike(like))
        )
    df = _parse_audit_date(date_from)
    if df:
        query = query.filter(AuditLog.timestamp >= df)
    dt_to = _parse_audit_date(date_to)
    if dt_to:
        query = query.filter(AuditLog.timestamp < dt_to + timedelta(days=1))

    query = query.order_by(AuditLog.timestamp.desc(), AuditLog.id.desc())
    rows = query.all()

    # Category is derived, so apply it in Python after the SQL filters.
    if category and category in AUDIT_CATEGORIES:
        rows = [r for r in rows if _audit_category(r.action, r.target_table) == category]
    return rows


@router.get("/audit", response_class=HTMLResponse)
def audit_trail(
    request: Request,
    db: Session = Depends(get_db),
    q: Optional[str] = None,
    user_id: Optional[str] = None,
    action: Optional[str] = None,
    category: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    page: Optional[str] = None,
    per_page: Optional[str] = None,
):
    user = require_role(request, ["admin"])
    if isinstance(user, RedirectResponse):
        return user

    page_num = parse_page(page)
    per_page_val = parse_per_page(per_page)

    # Empty form values arrive as "" (selects/inputs left blank). Treat them
    # as None and coerce the numeric user filter safely — never parse "" as int.
    uid = int(user_id) if (user_id or "").strip().isdigit() else None

    rows = _audit_filtered_rows(db, q, uid, action, category, date_from, date_to)

    page_obj = paginate(rows, page_num, per_page_val)
    page_rows = page_obj.items

    # Build view items grouped by PHT day, preserving newest-first order.
    grouped = []
    current_label, current_items = None, None
    for log in page_rows:
        item = {
            "id": log.id,
            "timestamp": log.timestamp,
            "username": log.user.username if log.user else "—",
            "action": log.action,
            "category": _audit_category(log.action, log.target_table),
            "target_table": log.target_table,
            "target_id": log.target_id,
            "description": log.description,
        }
        label = _audit_day_label(log.timestamp)
        if label != current_label:
            current_label, current_items = label, []
            grouped.append((label, current_items))
        current_items.append(item)

    actions = [a[0] for a in db.query(AuditLog.action).distinct().all() if a[0]]
    users = db.query(User).order_by(User.username).all()
    focus_user = db.query(User).filter(User.id == uid).first() if uid else None

    # Query string (filters minus page / per_page) for pagination links.
    base_query = build_base_query({
        "q": q or "", "user_id": uid or "", "action": action or "",
        "category": category or "", "date_from": date_from or "",
        "date_to": date_to or "",
    })

    return templates.TemplateResponse(
        request=request,
        name="admin/audit_list.html",
        context={
            "user": user,
            "active_nav": "audit",
            "grouped": grouped,
            "total": page_obj.total,
            "page_obj": page_obj,
            "categories": AUDIT_CATEGORIES,
            "actions": sorted(actions),
            "users": users,
            "focus_user": focus_user,
            "base_query": base_query,
            # Echo current filters back into the form.
            "f_q": q or "",
            "f_user_id": uid or "",
            "f_action": action or "",
            "f_category": category or "",
            "f_date_from": date_from or "",
            "f_date_to": date_to or "",
        },
    )


def _audit_export_time(dt) -> str:
    """Sortable PHT timestamp for export cells (distinct from the prose `pht`)."""
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_PHT).strftime("%Y-%m-%d %I:%M %p")


def _pdf_safe(s) -> str:
    """Core PDF fonts are latin-1 only; fold common punctuation and replace
    anything else so a stray unicode char never aborts the whole export."""
    s = (s or "")
    for bad, good in (("—", "-"), ("–", "-"), ("•", "-"),
                      ("’", "'"), ("‘", "'"),
                      ("“", '"'), ("”", '"'), ("·", "-")):
        s = s.replace(bad, good)
    return s.encode("latin-1", "replace").decode("latin-1")


def _audit_records(rows):
    """Flatten AuditLog rows into export-ready dicts (shared by CSV + PDF)."""
    records = []
    for log in rows:
        entity = log.target_table or ""
        if log.target_id:
            entity = f"{entity} #{log.target_id}".strip()
        records.append({
            "time": _audit_export_time(log.timestamp),
            "user": log.user.username if log.user else "—",
            "category": _audit_category(log.action, log.target_table),
            "action": log.action or "",
            "entity": entity or "—",
            "description": log.description or "",
        })
    return records


def _audit_filter_summary(db, q, uid, action, category, date_from, date_to) -> str:
    """Human-readable description of the active filters, for the PDF header."""
    parts = []
    if q:
        parts.append(f'search "{q.strip()}"')
    if uid:
        u = db.query(User).filter(User.id == uid).first()
        parts.append(f"user {u.username}" if u else f"user #{uid}")
    if category:
        parts.append(f"category {category}")
    if action:
        parts.append(f"action {action}")
    if date_from:
        parts.append(f"from {date_from}")
    if date_to:
        parts.append(f"to {date_to}")
    return ", ".join(parts) if parts else "none (all entries)"


def _audit_pdf_bytes(records, filter_summary) -> bytes:
    from fpdf import FPDF
    from fpdf.enums import XPos, YPos

    pdf = FPDF(orientation="L", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=12)
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 9, _pdf_safe("RisKonek — Audit Trail"),
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    n = len(records)
    stamp = datetime.now(_PHT).strftime("%B %d, %Y at %I:%M %p")
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(90, 90, 90)
    pdf.cell(0, 5, _pdf_safe(f"Generated {stamp} PHT - {n} entr"
                             f"{'y' if n == 1 else 'ies'}"),
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.multi_cell(0, 5, _pdf_safe(f"Filters: {filter_summary}"),
                   new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_text_color(0, 0, 0)
    pdf.ln(2)

    headings = ["Time (PHT)", "User", "Category", "Action", "Entity", "Description"]
    pdf.set_font("Helvetica", "", 7.5)
    with pdf.table(col_widths=(16, 11, 13, 12, 12, 36),
                   text_align="LEFT", line_height=4.6) as table:
        head = table.row()
        for h in headings:
            head.cell(h)
        for r in records:
            row = table.row()
            row.cell(_pdf_safe(r["time"]))
            row.cell(_pdf_safe(r["user"]))
            row.cell(_pdf_safe(r["category"]))
            row.cell(_pdf_safe(r["action"]))
            row.cell(_pdf_safe(r["entity"]))
            row.cell(_pdf_safe(r["description"]))

    return bytes(pdf.output())


@router.get("/audit/export")
def audit_export(
    request: Request,
    db: Session = Depends(get_db),
    q: Optional[str] = None,
    user_id: Optional[str] = None,
    action: Optional[str] = None,
    category: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    format: str = "csv",
):
    """Export the currently-filtered audit trail as CSV or PDF. Exports the
    full filtered set (all pages), newest-first — mirrors the list filters."""
    user = require_role(request, ["admin"])
    if isinstance(user, RedirectResponse):
        return user

    uid = int(user_id) if (user_id or "").strip().isdigit() else None
    rows = _audit_filtered_rows(db, q, uid, action, category, date_from, date_to)
    records = _audit_records(rows)

    stamp = datetime.now(_PHT).strftime("%Y-%m-%d")
    fmt = (format or "csv").lower()

    if fmt == "pdf":
        summary = _audit_filter_summary(
            db, q, uid, action, category, date_from, date_to
        )
        content = _audit_pdf_bytes(records, summary)
        return Response(
            content=content,
            media_type="application/pdf",
            headers={"Content-Disposition":
                     f'attachment; filename="audit-trail-{stamp}.pdf"'},
        )

    # CSV (default). UTF-8 BOM so Excel renders accented names correctly.
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Time (PHT)", "User", "Category", "Action", "Entity", "Description"])
    for r in records:
        writer.writerow([r["time"], r["user"], r["category"],
                         r["action"], r["entity"], r["description"]])
    content = ("﻿" + buf.getvalue()).encode("utf-8")
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition":
                 f'attachment; filename="audit-trail-{stamp}.csv"'},
    )


@router.get("/audit/{log_id}", response_class=HTMLResponse)
def audit_detail(log_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_role(request, ["admin"])
    if isinstance(user, RedirectResponse):
        return user

    log = db.query(AuditLog).filter(AuditLog.id == log_id).first()
    if not log:
        return RedirectResponse(url="/admin/audit", status_code=302)

    actor = db.query(User).filter(User.id == log.user_id).first()
    category = _audit_category(log.action, log.target_table)

    # Surface before/after diffs + a link when the entry targets an upload.
    related_upload = None
    upload_changes = []
    if log.target_table == "uploaded_reports" and log.target_id:
        related_upload = db.query(UploadedReport).filter(
            UploadedReport.id == log.target_id
        ).first()
        if related_upload:
            upload_changes = (
                db.query(UploadHistory)
                .filter(
                    UploadHistory.report_id == related_upload.id,
                    UploadHistory.event_type == UploadEvent.edited,
                )
                .order_by(UploadHistory.timestamp.desc(), UploadHistory.id.desc())
                .all()
            )

    return templates.TemplateResponse(
        request=request,
        name="admin/audit_detail.html",
        context={
            "user": user,
            "active_nav": "audit",
            "log": log,
            "actor": actor,
            "category": category,
            "related_upload": related_upload,
            "upload_changes": upload_changes,
        },
    )


# ─────────────────────────────────────────────────────────────────────
# LOGIN HISTORY — admin-only view of authentication events. A focused,
# read-only projection of the SAME AuditLog rows the auth flow already
# writes (login / logout / login_failed / login_lockout /
# login_blocked_inactive). No new table and no change to auth.
# ─────────────────────────────────────────────────────────────────────

# Auth action → (human status label, badge class). Order also drives the
# status filter dropdown.
LOGIN_STATUS_META = {
    "login": ("Success", "status-operational"),
    "logout": ("Logout", "rk-cat-system"),
    "login_failed": ("Failed", "status-maintenance"),
    "login_lockout": ("Locked", "status-unavailable"),
    "login_blocked_inactive": ("Blocked (inactive)", "status-unavailable"),
}
_LOGIN_ACTIONS = list(LOGIN_STATUS_META.keys())

# IP is embedded in the audit description as "(from X)" — pull it back out
# for a dedicated column rather than adding a schema column.
_IP_RE = re.compile(r"\(from\s+([^)]+)\)")


def _extract_ip(description: str) -> str:
    if not description:
        return "—"
    m = _IP_RE.search(description)
    return m.group(1).strip() if m else "—"


@router.get("/login-history", response_class=HTMLResponse)
def login_history(
    request: Request,
    db: Session = Depends(get_db),
    q: Optional[str] = None,
    user_id: Optional[str] = None,
    status: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    page: Optional[str] = None,
    per_page: Optional[str] = None,
):
    user = require_role(request, ["admin"])
    if isinstance(user, RedirectResponse):
        return user

    uid = int(user_id) if (user_id or "").strip().isdigit() else None

    query = (
        db.query(AuditLog)
        .outerjoin(User, AuditLog.user_id == User.id)
        .filter(AuditLog.action.in_(_LOGIN_ACTIONS))
    )
    if uid:
        query = query.filter(AuditLog.user_id == uid)
    if status in LOGIN_STATUS_META:
        query = query.filter(AuditLog.action == status)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(
            (User.username.ilike(like)) | (AuditLog.description.ilike(like))
        )
    df = _parse_audit_date(date_from)
    if df:
        query = query.filter(AuditLog.timestamp >= df)
    dt_to = _parse_audit_date(date_to)
    if dt_to:
        query = query.filter(AuditLog.timestamp < dt_to + timedelta(days=1))

    events = query.order_by(AuditLog.timestamp.desc(), AuditLog.id.desc()).all()

    rows = []
    for e in events:
        label, badge = LOGIN_STATUS_META.get(e.action, (e.action, "rk-cat-system"))
        rows.append({
            "id": e.id,
            "timestamp": e.timestamp,
            "username": e.user.username if e.user else "—",
            "status_label": label,
            "status_badge": badge,
            "ip": _extract_ip(e.description),
        })

    # Summary over the full (unpaginated) filtered set.
    summary = {
        "total": len(rows),
        "success": sum(1 for r in rows if r["status_label"] == "Success"),
        "failed": sum(1 for r in rows if r["status_label"] == "Failed"),
        "locked": sum(1 for r in rows if r["status_label"] == "Locked"),
    }

    page_obj = paginate(rows, parse_page(page), parse_per_page(per_page))
    base_query = build_base_query({
        "q": q or "", "user_id": uid or "", "status": status or "",
        "date_from": date_from or "", "date_to": date_to or "",
    })

    users = db.query(User).order_by(User.username).all()
    focus_user = db.query(User).filter(User.id == uid).first() if uid else None

    return templates.TemplateResponse(
        request=request,
        name="admin/login_history.html",
        context={
            "user": user,
            "active_nav": "login_history",
            "rows": page_obj.items,
            "page_obj": page_obj,
            "base_query": base_query,
            "summary": summary,
            "statuses": [(k, v[0]) for k, v in LOGIN_STATUS_META.items()],
            "users": users,
            "focus_user": focus_user,
            "f_q": q or "",
            "f_user_id": uid or "",
            "f_status": status or "",
            "f_date_from": date_from or "",
            "f_date_to": date_to or "",
        },
    )


# ─────────────────────────────────────────────────────────────────────
# BARANGAY FIELD DATA — list + profile
# TR-ADM-22, TR-ADM-23
# ─────────────────────────────────────────────────────────────────────

def _vulnerable_percent(pop: Population) -> float:
    """Combined PWD + elderly + children share of total population."""
    if not pop or not pop.total_population:
        return 0.0
    vulnerable = (pop.pwd_count or 0) + (pop.elderly_count or 0) + (pop.children_count or 0)
    return round((vulnerable / pop.total_population) * 100, 1)


def _risk_trend(incidents, disaster_type: DisasterType) -> str:
    """Rule-based trend: last 12 months count > previous 12 months → Increasing,
    else Stable. Operates on the in-memory incident list to avoid extra queries.
    """
    today = date.today()
    last_year_start = today - timedelta(days=365)
    prev_year_start = today - timedelta(days=730)

    last_12 = sum(
        1 for inc in incidents
        if inc.disaster_type == disaster_type
        and inc.date_occurred >= last_year_start
    )
    prev_12 = sum(
        1 for inc in incidents
        if inc.disaster_type == disaster_type
        and prev_year_start <= inc.date_occurred < last_year_start
    )
    return "Increasing" if last_12 > prev_12 else "Stable"


@router.get("/barangays", response_class=HTMLResponse)
def barangay_list(
    request: Request,
    db: Session = Depends(get_db),
    q: Optional[str] = None,
    risk: Optional[str] = None,
    page: Optional[str] = None,
    per_page: Optional[str] = None,
):
    user = require_role(request, ["admin"])
    if isinstance(user, RedirectResponse):
        return user

    query = db.query(Barangay)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(Barangay.name.ilike(like))
    if risk and risk in {r.value for r in RiskLevel}:
        query = query.filter(Barangay.risk_level == RiskLevel(risk))

    barangays = query.order_by(Barangay.name).all()

    rows = []
    for brgy in barangays:
        pop = db.query(Population).filter(
            Population.barangay_id == brgy.id
        ).order_by(Population.recorded_at.desc()).first()
        rows.append({
            "id": brgy.id,
            "name": brgy.name,
            "population": pop.total_population if pop else 0,
            "vulnerable_pct": _vulnerable_percent(pop),
            "risk_level": brgy.risk_level.value if brgy.risk_level else "low",
        })

    total_count = len(rows)
    page_obj = paginate(rows, parse_page(page), parse_per_page(per_page))
    base_query = build_base_query({"q": q or "", "risk": risk or ""})

    return templates.TemplateResponse(
        request=request,
        name="admin/barangays_list.html",
        context={
            "user": user,
            "rows": page_obj.items,
            "page_obj": page_obj,
            "base_query": base_query,
            "q": q or "",
            "risk_filter": risk or "",
            "risk_levels": [r.value for r in RiskLevel],
            "total_count": total_count,
        },
    )


def barangay_profile_context(db, brgy: Barangay) -> dict:
    """Build the full barangay-profile context for `brgy`.

    Shared by the admin barangay profile (TR-ADM-23) and the
    barangay-scoped BDRRMO profile (Week 9) so both render identical
    population / incident / facility / planning-priority data without
    duplicating the logic. Does NOT include `user` — the caller adds it.
    """
    population = db.query(Population).filter(
        Population.barangay_id == brgy.id
    ).order_by(Population.recorded_at.desc()).first()

    incidents = db.query(Incident).filter(
        Incident.barangay_id == brgy.id
    ).order_by(Incident.date_occurred.desc()).all()

    facilities = db.query(Facility).filter(
        Facility.barangay_id == brgy.id,
        Facility.is_archived == False,
    ).order_by(Facility.facility_type, Facility.name).all()

    # ── Risk score (reuse existing formula) ──────────────────────────
    risk_result = compute_risk_score(brgy, incidents, population)

    # ── Historical disaster counts (last 5 years) ────────────────────
    five_years_ago = date.today() - timedelta(days=365 * 5)
    recent_incidents = [i for i in incidents if i.date_occurred >= five_years_ago]
    incident_counts_by_type = {dt.value: 0 for dt in DisasterType}
    for inc in recent_incidents:
        incident_counts_by_type[inc.disaster_type.value] += 1

    # ── Vulnerable group breakdown ───────────────────────────────────
    total_pop = population.total_population if population else 0
    elderly = population.elderly_count if population else 0
    pwd = population.pwd_count if population else 0
    children = population.children_count if population else 0
    households = population.total_households if population else 0

    def pct(n):
        return round((n / total_pop) * 100, 1) if total_pop else 0.0

    # ── Critical facilities (formatted) ──────────────────────────────
    facility_rows = []
    for f in facilities:
        if not f.is_active:
            status_label, status_class = "Under Maintenance", "status-maintenance"
        else:
            status_label, status_class = "Operational", "status-operational"
        facility_rows.append({
            "name": f.name,
            "type": f.facility_type.value.replace("_", " ").title(),
            "capacity": f.capacity if f.capacity else "—",
            "status_label": status_label,
            "status_class": status_class,
        })

    return {
        "barangay": brgy,
        "risk_level": brgy.risk_level.value if brgy.risk_level else "low",
        "risk_score": risk_result["score"],
        "risk_breakdown": risk_result["breakdown"],
        "total_population": total_pop,
        "households": households,
        "vulnerable_pct": _vulnerable_percent(population),
        "elderly": elderly,
        "pwd": pwd,
        "children": children,
        "elderly_pct": pct(elderly),
        "pwd_pct": pct(pwd),
        "children_pct": pct(children),
        "flood_trend": _risk_trend(incidents, DisasterType.flood),
        "fire_trend": _risk_trend(incidents, DisasterType.fire),
        "facility_rows": facility_rows,
        "incident_counts": incident_counts_by_type,
        "recent_incidents": recent_incidents[:10],
        "hazard_types": [h.strip() for h in (brgy.hazard_types or "").split(",") if h.strip()],
    }


# ─────────────────────────────────────────────────────────────────────
# CONTACT DIRECTORY — admin-wide, read-only view over Barangay contact
# columns (captain / chairperson / emergency responders). Reuses the SAME
# Barangay record the BDRRMO Chairperson maintains at /bdrrmo/contacts, so
# there is a single source of truth — no separate contacts table.
# ─────────────────────────────────────────────────────────────────────

@router.get("/contacts", response_class=HTMLResponse)
def contact_directory(
    request: Request,
    db: Session = Depends(get_db),
    q: Optional[str] = None,
    brgy: Optional[str] = None,
    page: Optional[str] = None,
    per_page: Optional[str] = None,
):
    user = require_role(request, ["admin"])
    if isinstance(user, RedirectResponse):
        return user

    context = build_directory_context(
        db, q=q, brgy=brgy, page=page, per_page=per_page,
        directory_url="/admin/contacts", active_nav="contacts",
    )
    context["user"] = user
    return templates.TemplateResponse(
        request=request, name="shared/contact_directory.html", context=context,
    )


@router.get("/barangays/{barangay_id}", response_class=HTMLResponse)
def barangay_profile(
    barangay_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = require_role(request, ["admin"])
    if isinstance(user, RedirectResponse):
        return user

    brgy = db.query(Barangay).filter(Barangay.id == barangay_id).first()
    if not brgy:
        return RedirectResponse(url="/admin/barangays", status_code=302)

    context = barangay_profile_context(db, brgy)
    context["user"] = user
    return templates.TemplateResponse(
        request=request,
        name="admin/barangay_profile.html",
        context=context,
    )


# ─────────────────────────────────────────────────────────────────────
# GIS MAP — TR-ADM-10, TR-ADM-16, TR-ADM-17
# Official hazard polygon layers (TR-ADM-11) are deferred until valid
# GeoJSON / shapefile sources are confirmed by the client.
# ─────────────────────────────────────────────────────────────────────

@router.get("/map", response_class=HTMLResponse)
def gis_map(request: Request, db: Session = Depends(get_db)):
    user = require_role(request, ["admin"])
    if isinstance(user, RedirectResponse):
        return user

    barangays = db.query(Barangay).order_by(Barangay.name).all()
    facility_types = [t.value for t in FacilityType]
    statuses = ["Permanent", "Temporary", "Under Construction"]

    edit_id = request.query_params.get("edit")
    editing = None
    if edit_id:
        editing = db.query(Facility).filter(Facility.id == edit_id).first()

    return templates.TemplateResponse(
        request=request,
        name="admin/map.html",
        context={
            "user": user,
            "active_nav": "map",
            "barangays": barangays,
            "facility_types": facility_types,
            "statuses": statuses,
            "facility_statuses": [s.value for s in FacilityStatus],
            "facility_classifications": FACILITY_CLASSIFICATIONS,
            "eo_moa_mou_statuses": EO_MOA_MOU_STATUSES,
            "editing": editing,
            "sp_bounds": get_san_pedro_bounds(),
            "sp_boundary": get_san_pedro_boundary(),
            "success": request.query_params.get("success"),
            "error": request.query_params.get("error"),
        },
    )


@router.get("/api/facilities-map-data")
def facilities_map_data(request: Request, db: Session = Depends(get_db)):
    """JSON feed for the Leaflet map. Admin-only.

    Returns one record per facility with all popup fields pre-formatted.
    """
    user = require_role(request, ["admin"])
    if isinstance(user, RedirectResponse):
        return user

    facilities = (
        db.query(Facility)
        .join(Barangay, Facility.barangay_id == Barangay.id)
        .filter(Facility.is_archived == False)
        .order_by(Barangay.name, Facility.name)
        .all()
    )

    payload = []
    for f in facilities:
        payload.append({
            "id": f.id,
            "name": f.name,
            "barangay": f.barangay.name if f.barangay else None,
            "facility_type": f.facility_type.value if f.facility_type else None,
            "facility_type_label": (
                f.facility_type.value.replace("_", " ").title()
                if f.facility_type else None
            ),
            "lat": f.latitude,
            "lng": f.longitude,
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
            "operational_status": f.operational_status.value if f.operational_status else None,
            "barangay_id": f.barangay_id,
            "is_approximate_location": bool(f.is_approximate_location),
            "is_city_level": bool(f.is_city_level),
            "is_active": bool(f.is_active),
        })

    return JSONResponse(payload)


# ─────────────────────────────────────────────────────────────────────
# GIS MAP — Add / Edit Critical Facility (Admin has no barangay scope of
# its own; the submitter picks any barangay via a selector). Reuses the
# same shared validation/duplicate-detection helpers as the BDRRMO flow
# (app/services/facility_details.py) so both stay in lock step.
# ─────────────────────────────────────────────────────────────────────

@router.post("/facilities")
def admin_facility_create(
    request: Request,
    db: Session = Depends(get_db),
    barangay_id: str = Form(...),
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
    user = require_role(request, ["admin"])
    if isinstance(user, RedirectResponse):
        return user

    barangay = db.query(Barangay).filter(Barangay.id == barangay_id).first()
    if not barangay:
        return RedirectResponse(url="/admin/map?error=Invalid+barangay", status_code=302)

    name_clean, name_err = _validate_facility_name(name)
    if name_err:
        return RedirectResponse(url="/admin/map?error=" + quote_plus(name_err), status_code=302)
    if facility_type not in {t.value for t in FacilityType}:
        return RedirectResponse(url="/admin/map?error=Invalid+facility+type", status_code=302)
    if operational_status not in {s.value for s in FacilityStatus}:
        operational_status = FacilityStatus.available.value
    address_clean, address_err = _validate_facility_address(address)
    if address_err:
        return RedirectResponse(url="/admin/map?error=" + quote_plus(address_err), status_code=302)

    lat = _parse_coord(latitude, -90, 90)
    lon = _parse_coord(longitude, -180, 180)
    if lat is None or lon is None:
        return RedirectResponse(url="/admin/map?error=Invalid+coordinates", status_code=302)
    if not is_within_san_pedro(lat, lon):
        return RedirectResponse(
            url="/admin/map?error=" + quote_plus("Location must be within San Pedro City, Laguna."),
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
        return RedirectResponse(url="/admin/map?error=" + quote_plus(details_err), status_code=302)

    dup = _find_duplicate(db, barangay.id, name_clean, lat, lon)
    if dup:
        return RedirectResponse(
            url="/admin/map?error=" + quote_plus(
                f"A similar facility already exists: '{dup.name}'. "
                "Edit that record instead of adding a duplicate."
            ),
            status_code=302,
        )

    facility = Facility(
        barangay_id=barangay.id,
        name=name_clean,
        facility_type=FacilityType(facility_type),
        latitude=lat,
        longitude=lon,
        address=address_clean,
        operational_status=FacilityStatus(operational_status),
        is_archived=False,
        **details,
    )
    facility.is_active = facility.operational_status == FacilityStatus.available
    db.add(facility)
    db.commit()
    db.refresh(facility)

    log_action(
        db, user["id"], "created", "facilities", facility.id,
        f"Admin added critical facility '{facility.name}' "
        f"({facility.facility_type.value}) in {barangay.name}",
    )
    return RedirectResponse(url="/admin/map?success=Facility+added", status_code=302)


@router.post("/facilities/{facility_id}/edit")
def admin_facility_edit(
    facility_id: int,
    request: Request,
    db: Session = Depends(get_db),
    barangay_id: str = Form(...),
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
    user = require_role(request, ["admin"])
    if isinstance(user, RedirectResponse):
        return user

    facility = db.query(Facility).filter(Facility.id == facility_id).first()
    if not facility:
        return RedirectResponse(url="/admin/map?error=Facility+not+found", status_code=302)

    barangay = db.query(Barangay).filter(Barangay.id == barangay_id).first()
    if not barangay:
        return RedirectResponse(
            url=f"/admin/map?edit={facility.id}&error=Invalid+barangay", status_code=302
        )

    name_clean, name_err = _validate_facility_name(name)
    if name_err:
        return RedirectResponse(
            url=f"/admin/map?edit={facility.id}&error=" + quote_plus(name_err), status_code=302
        )
    if facility_type not in {t.value for t in FacilityType}:
        return RedirectResponse(url="/admin/map?error=Invalid+facility+type", status_code=302)
    if operational_status not in {s.value for s in FacilityStatus}:
        operational_status = FacilityStatus.available.value
    address_clean, address_err = _validate_facility_address(address)
    if address_err:
        return RedirectResponse(
            url=f"/admin/map?edit={facility.id}&error=" + quote_plus(address_err), status_code=302
        )

    lat = _parse_coord(latitude, -90, 90)
    lon = _parse_coord(longitude, -180, 180)
    if lat is None or lon is None:
        return RedirectResponse(url="/admin/map?error=Invalid+coordinates", status_code=302)
    if not is_within_san_pedro(lat, lon):
        return RedirectResponse(
            url=f"/admin/map?edit={facility.id}&error=" + quote_plus(
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
            url=f"/admin/map?edit={facility.id}&error=" + quote_plus(details_err), status_code=302
        )

    dup = _find_duplicate(db, barangay.id, name_clean, lat, lon, exclude_id=facility.id)
    if dup:
        return RedirectResponse(
            url=f"/admin/map?edit={facility.id}&error=" + quote_plus(
                f"Another facility already matches this name/location: '{dup.name}'."
            ),
            status_code=302,
        )

    facility.barangay_id = barangay.id
    facility.name = name_clean
    facility.facility_type = FacilityType(facility_type)
    facility.latitude = lat
    facility.longitude = lon
    facility.address = address_clean
    facility.operational_status = FacilityStatus(operational_status)
    for key, value in details.items():
        setattr(facility, key, value)
    facility.is_active = facility.operational_status == FacilityStatus.available
    db.commit()

    log_action(
        db, user["id"], "updated", "facilities", facility.id,
        f"Admin updated critical facility '{facility.name}' in {barangay.name}",
    )
    return RedirectResponse(url="/admin/map?success=Facility+updated", status_code=302)


@router.get("/api/search")
def api_global_search(request: Request, db: Session = Depends(get_db), q: Optional[str] = None):
    """Sidebar global search (#rkSearchInput in base.html). See
    app/services/global_search.py for why this route didn't exist before."""
    user = require_role(request, ["admin"])
    if isinstance(user, RedirectResponse):
        return user
    if not q or len(q.strip()) < 2:
        return {"results": []}
    return global_search.search_admin(db, q)


@router.get("/api/location-search")
def api_location_search(request: Request, db: Session = Depends(get_db), q: Optional[str] = None):
    """General San Pedro City place search for /admin/map — same shared
    service as the BDRRMO picker, but its own route since the BDRRMO route
    is guarded by require_role(["bdrrmo"]).
    """
    user = require_role(request, ["admin"])
    if isinstance(user, RedirectResponse):
        return user
    return search_locations(db, q or "")


@router.get("/api/reverse-geocode")
def api_reverse_geocode(
    request: Request, lat: Optional[str] = None, lng: Optional[str] = None,
):
    """Best-effort place name for a picked point, used by the Add/Edit
    Facility map picker on /admin/map (mirrors the BDRRMO equivalent)."""
    user = require_role(request, ["admin"])
    if isinstance(user, RedirectResponse):
        return user
    try:
        lat_f, lng_f = float(lat), float(lng)
    except (TypeError, ValueError):
        return {"available": True, "display_name": None}
    return reverse_geocode_san_pedro(lat_f, lng_f)


@router.get("/api/nearby-landmarks")
def api_nearby_landmarks(
    request: Request,
    lat: Optional[str] = None, lng: Optional[str] = None, radius: Optional[str] = None,
):
    """Optional reference layer for /admin/map — category-filtered OSM
    landmarks near a point, proxied server-side (see app/services/geocoding.py).
    These are read-only reference data, never counted as registered facilities.
    """
    user = require_role(request, ["admin"])
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


# ─────────────────────────────────────────────────────────────────────
# WEEK 7 — RESOURCE GOODS INVENTORY (Module A)
# Roles: admin, cdrrmo_staff
# Tracks consumable disaster-response resources (food packs, water,
# medicine, hygiene kits, blankets, tarpaulins, sleeping kits, …).
# ─────────────────────────────────────────────────────────────────────

RESOURCE_ROLES = ["admin", "cdrrmo_staff"]
EQUIPMENT_ROLES = ["admin", "cdrrmo_staff", "cfau_oic"]

# Optional food sub-classification (only meaningful when category == food).
# Stored as a plain string in Resource.food_type; NULL for non-food items.
FOOD_TYPE_LABELS = {
    "rice": "Rice",
    "canned_goods": "Canned Goods",
    "drinking_water": "Drinking Water",
    "rte_meals": "Ready-to-Eat Meals",
    "baby_food": "Baby Food",
    "medical_nutrition": "Medical Nutrition",
}
FOOD_TYPE_CHOICES = list(FOOD_TYPE_LABELS.keys())

_NEAR_EXPIRY_DAYS = 30


def _resource_alert(r: Resource) -> str:
    """Rule-based alert tier for a resource. Order matters: expired
    beats near-expiry, and stock alerts are reported alongside expiry.
    Returns one of: 'expired', 'near_expiry', 'low_stock', 'ok'.
    """
    today = date.today()
    if r.is_perishable and r.expiry_date:
        if r.expiry_date < today:
            return "expired"
        if r.expiry_date <= today + timedelta(days=_NEAR_EXPIRY_DAYS):
            return "near_expiry"
    if (r.quantity or 0) <= (r.restock_threshold or 0):
        return "low_stock"
    return "ok"


def _resource_summary(resources):
    """Counts for the dashboard cards at the top of the list page."""
    total = len(resources)
    low = sum(1 for r in resources if _resource_alert(r) == "low_stock")
    near = sum(1 for r in resources if _resource_alert(r) == "near_expiry")
    exp = sum(1 for r in resources if _resource_alert(r) == "expired")
    return {"total": total, "low_stock": low, "near_expiry": near, "expired": exp}


@router.get("/resources", response_class=HTMLResponse)
def resources_list(
    request: Request,
    db: Session = Depends(get_db),
    q: Optional[str] = None,
    category: Optional[str] = None,
    food_type: Optional[str] = None,
    alert: Optional[str] = None,
    archived: Optional[str] = None,
    page: Optional[str] = None,
    per_page: Optional[str] = None,
):
    user = require_role(request, RESOURCE_ROLES)
    if isinstance(user, RedirectResponse):
        return user

    query = db.query(Resource)
    show_archived = (archived == "1")
    query = query.filter(Resource.is_archived == show_archived)

    if q:
        like = f"%{q.strip()}%"
        query = query.filter(
            (Resource.name.ilike(like)) | (Resource.storage_location.ilike(like))
        )
    if category and category in {c.value for c in ResourceCategory}:
        query = query.filter(Resource.category == ResourceCategory(category))
    if food_type and food_type in FOOD_TYPE_CHOICES:
        query = query.filter(Resource.food_type == food_type)

    rows = query.order_by(Resource.name).all()

    # Alert filter is derived, so apply after SQL filtering.
    if alert in ("low_stock", "near_expiry", "expired"):
        rows = [r for r in rows if _resource_alert(r) == alert]

    # Summary cards always reflect the full *active* inventory, not the
    # filtered view — so users see the real backlog of issues.
    active_inventory = db.query(Resource).filter(Resource.is_archived == False).all()
    summary = _resource_summary(active_inventory)

    view_rows = []
    for r in rows:
        view_rows.append({
            "id": r.id,
            "name": r.name,
            "category": r.category.value if r.category else "",
            "category_label": r.category.value.title() if r.category else "—",
            "food_type": r.food_type or "",
            "food_type_label": FOOD_TYPE_LABELS.get(r.food_type or "", ""),
            "is_perishable": r.is_perishable,
            "quantity": r.quantity or 0,
            "unit": r.unit or "",
            "storage_location": r.storage_location or "—",
            "restock_threshold": r.restock_threshold or 0,
            "expiry_date": r.expiry_date,
            "is_archived": r.is_archived,
            "alert": _resource_alert(r),
            "last_updated": r.last_updated,
        })

    page_obj = paginate(view_rows, parse_page(page), parse_per_page(per_page))
    base_query = build_base_query({
        "q": q or "", "category": category or "", "food_type": food_type or "",
        "alert": alert or "", "archived": "1" if show_archived else "",
    })

    return templates.TemplateResponse(
        request=request,
        name="admin/resources_list.html",
        context={
            "user": user,
            "active_nav": "resources",
            "rows": page_obj.items,
            "page_obj": page_obj,
            "base_query": base_query,
            "summary": summary,
            "categories": [c.value for c in ResourceCategory],
            "food_types": [(v, FOOD_TYPE_LABELS[v]) for v in FOOD_TYPE_CHOICES],
            "f_q": q or "",
            "f_category": category or "",
            "f_food_type": food_type or "",
            "f_alert": alert or "",
            "f_archived": "1" if show_archived else "",
            "show_archived": show_archived,
            # Default + max for the stock modal's date-time field (PHT).
            "now_local": datetime.now(_PHT).strftime("%Y-%m-%dT%H:%M"),
        },
    )


@router.get("/resources/new", response_class=HTMLResponse)
def resource_new_form(request: Request, db: Session = Depends(get_db)):
    user = require_role(request, RESOURCE_ROLES)
    if isinstance(user, RedirectResponse):
        return user
    return templates.TemplateResponse(
        request=request,
        name="admin/resource_form.html",
        context={
            "user": user,
            "active_nav": "resources",
            "edit_mode": False,
            "target": None,
            "categories": [c.value for c in ResourceCategory],
            "food_types": [(v, FOOD_TYPE_LABELS[v]) for v in FOOD_TYPE_CHOICES],
            "error": None,
        },
    )


def _parse_date_or_none(s: Optional[str]):
    if not s:
        return None
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


@router.post("/resources/new")
def resource_create(
    request: Request,
    db: Session = Depends(get_db),
    name: str = Form(...),
    category: str = Form(...),
    food_type: str = Form(""),
    is_perishable: Optional[str] = Form(None),
    quantity: int = Form(0),
    unit: str = Form(""),
    storage_location: str = Form(""),
    restock_threshold: int = Form(0),
    expiry_date: str = Form(""),
):
    user = require_role(request, RESOURCE_ROLES)
    if isinstance(user, RedirectResponse):
        return user

    def render_error(msg):
        return templates.TemplateResponse(
            request=request,
            name="admin/resource_form.html",
            context={
                "user": user,
                "active_nav": "resources",
                "edit_mode": False,
                "target": None,
                "categories": [c.value for c in ResourceCategory],
                "food_types": [(v, FOOD_TYPE_LABELS[v]) for v in FOOD_TYPE_CHOICES],
                "error": msg,
            },
        )

    name = name.strip()
    if not name:
        return render_error("Resource name is required.")
    if category not in {c.value for c in ResourceCategory}:
        return render_error("Invalid category.")

    perish = bool(is_perishable)
    exp = _parse_date_or_none(expiry_date) if perish else None
    # Food sub-classification applies only to the food category.
    food_type_value = (
        food_type if (category == ResourceCategory.food.value
                      and food_type in FOOD_TYPE_CHOICES)
        else None
    )

    r = Resource(
        name=name,
        category=ResourceCategory(category),
        food_type=food_type_value,
        is_perishable=perish,
        quantity=max(0, quantity or 0),
        unit=unit.strip() or None,
        storage_location=storage_location.strip() or None,
        restock_threshold=max(0, restock_threshold or 0),
        expiry_date=exp,
        is_archived=False,
        updated_by=user["id"],
    )
    db.add(r)
    db.commit()
    db.refresh(r)

    log_action(
        db, user["id"], "created", "resources", r.id,
        f"Created resource '{r.name}' ({r.category.value}, qty={r.quantity} {r.unit or ''})".strip(),
    )

    return RedirectResponse(
        url="/admin/resources?success=Resource+created+successfully",
        status_code=302,
    )


@router.get("/resources/{resource_id}/edit", response_class=HTMLResponse)
def resource_edit_form(resource_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_role(request, RESOURCE_ROLES)
    if isinstance(user, RedirectResponse):
        return user

    r = db.query(Resource).filter(Resource.id == resource_id).first()
    if not r:
        return RedirectResponse(url="/admin/resources", status_code=302)

    return templates.TemplateResponse(
        request=request,
        name="admin/resource_form.html",
        context={
            "user": user,
            "active_nav": "resources",
            "edit_mode": True,
            "target": r,
            "categories": [c.value for c in ResourceCategory],
            "food_types": [(v, FOOD_TYPE_LABELS[v]) for v in FOOD_TYPE_CHOICES],
            "error": None,
        },
    )


@router.post("/resources/{resource_id}/edit")
def resource_edit(
    resource_id: int,
    request: Request,
    db: Session = Depends(get_db),
    name: str = Form(...),
    category: str = Form(...),
    food_type: str = Form(""),
    is_perishable: Optional[str] = Form(None),
    unit: str = Form(""),
    storage_location: str = Form(""),
    restock_threshold: int = Form(0),
    expiry_date: str = Form(""),
):
    user = require_role(request, RESOURCE_ROLES)
    if isinstance(user, RedirectResponse):
        return user

    r = db.query(Resource).filter(Resource.id == resource_id).first()
    if not r:
        return RedirectResponse(url="/admin/resources", status_code=302)

    # Quantity is intentionally not editable here — use Add/Deduct Stock so
    # every quantity change is auditable with before/after numbers.
    changes = []
    new_name = name.strip()
    if new_name and new_name != r.name:
        changes.append(f"name: '{r.name}' → '{new_name}'")
        r.name = new_name

    if category in {c.value for c in ResourceCategory} and r.category.value != category:
        changes.append(f"category: {r.category.value} → {category}")
        r.category = ResourceCategory(category)

    # Food sub-classification: kept only while the item is in the food
    # category, otherwise cleared so a re-categorised item doesn't keep a
    # stale food_type. r.category reflects any change applied just above.
    new_food_type = (
        food_type if (r.category == ResourceCategory.food
                      and food_type in FOOD_TYPE_CHOICES)
        else None
    )
    if new_food_type != r.food_type:
        changes.append(f"food_type: {r.food_type or '—'} → {new_food_type or '—'}")
        r.food_type = new_food_type

    perish = bool(is_perishable)
    if perish != bool(r.is_perishable):
        changes.append(f"is_perishable: {bool(r.is_perishable)} → {perish}")
        r.is_perishable = perish

    new_unit = unit.strip() or None
    if new_unit != r.unit:
        changes.append(f"unit: '{r.unit or ''}' → '{new_unit or ''}'")
        r.unit = new_unit

    new_loc = storage_location.strip() or None
    if new_loc != r.storage_location:
        changes.append(f"storage_location: '{r.storage_location or ''}' → '{new_loc or ''}'")
        r.storage_location = new_loc

    new_thr = max(0, restock_threshold or 0)
    if new_thr != (r.restock_threshold or 0):
        changes.append(f"restock_threshold: {r.restock_threshold or 0} → {new_thr}")
        r.restock_threshold = new_thr

    new_exp = _parse_date_or_none(expiry_date) if perish else None
    if new_exp != r.expiry_date:
        changes.append(f"expiry_date: {r.expiry_date} → {new_exp}")
        r.expiry_date = new_exp

    r.updated_by = user["id"]
    db.commit()

    if changes:
        log_action(
            db, user["id"], "updated", "resources", r.id,
            f"Updated resource '{r.name}': " + "; ".join(changes),
        )

    return RedirectResponse(
        url="/admin/resources?success=Resource+updated+successfully",
        status_code=302,
    )


# ── Logistics movement accountability ────────────────────────────────
# Shared by the stock-change modal (below) and the equipment status
# modal (Module B). Every movement must say WHY it happened, WHERE the
# goods / unit went (when it is a deployment), and WHEN it actually
# happened — the last one is user-stated and backdatable, unlike
# AuditLog.timestamp which stays the system record time.

def _movement_fields(reason, deployed_to, occurred_at, location_required):
    """Validate and normalise the accountability fields. Returns
    (values, error): `error` is a user-facing message when validation
    fails, otherwise None. Mirrors the client-side rules in rk-forms.js —
    JS is convenience, this is the enforcement."""
    r = (reason or "").strip()
    if len(r) < 3:
        return None, "Reason is required (at least 3 characters)."

    loc = (deployed_to or "").strip() or None
    if location_required and not loc:
        return None, "Deployment location is required for this change."

    raw = (occurred_at or "").strip()
    if not raw:
        return None, "Date and time is required."
    try:
        # <input type="datetime-local"> submits "YYYY-MM-DDTHH:MM" (PHT,
        # as typed by the user); seconds may be appended by some browsers.
        local = datetime.strptime(raw[:16], "%Y-%m-%dT%H:%M")
    except ValueError:
        return None, "Invalid date and time."

    # Stored UTC-naive, like every other timestamp in the system.
    dt_utc = local.replace(tzinfo=_PHT).astimezone(timezone.utc).replace(tzinfo=None)
    if dt_utc > datetime.utcnow() + timedelta(minutes=5):
        return None, "Date and time cannot be in the future."

    return {"reason": r, "deployed_to": loc, "occurred_at": dt_utc}, None


@router.post("/resources/{resource_id}/stock")
def resource_stock_change(
    resource_id: int,
    request: Request,
    db: Session = Depends(get_db),
    action: str = Form(...),       # "add" or "deduct"
    amount: int = Form(...),
    reason: str = Form(""),
    deployed_to: str = Form(""),
    occurred_at: str = Form(""),
):
    user = require_role(request, RESOURCE_ROLES)
    if isinstance(user, RedirectResponse):
        return user

    r = db.query(Resource).filter(Resource.id == resource_id).first()
    if not r:
        return RedirectResponse(url="/admin/resources", status_code=302)

    if action not in ("add", "deduct"):
        return RedirectResponse(
            url="/admin/resources?error=Invalid+stock+action", status_code=302
        )
    if not amount or amount <= 0:
        return RedirectResponse(
            url=f"/admin/resources/{resource_id}/edit?error=Amount+must+be+positive",
            status_code=302,
        )

    # Deducting stock moves goods somewhere, so a destination is required;
    # adding stock (supplier delivery, returned goods) does not need one.
    movement, err = _movement_fields(
        reason, deployed_to, occurred_at, location_required=(action == "deduct")
    )
    if err:
        return RedirectResponse(
            url=f"/admin/resources?error={quote_plus(err)}", status_code=302
        )

    before = r.quantity or 0
    if action == "add":
        after = before + amount
        verb = "stock_added"
    else:
        if amount > before:
            return RedirectResponse(
                url=f"/admin/resources/{resource_id}/edit?error=Cannot+deduct+more+than+current+stock",
                status_code=302,
            )
        after = before - amount
        verb = "stock_deducted"

    r.quantity = after
    r.updated_by = user["id"]
    db.commit()

    note = f" — reason: {movement['reason']}"
    if movement["deployed_to"]:
        note += f"; deployed to: {movement['deployed_to']}"
    log_action(
        db, user["id"], verb, "resources", r.id,
        f"Resource '{r.name}' quantity {before} → {after} ({'+' if action == 'add' else '-'}{amount} {r.unit or ''}){note}",
        reason=movement["reason"],
        deployed_to=movement["deployed_to"],
        occurred_at=movement["occurred_at"],
    )

    return RedirectResponse(
        url=f"/admin/resources?success=Stock+{'added' if action == 'add' else 'deducted'}+successfully",
        status_code=302,
    )


@router.post("/resources/{resource_id}/archive")
def resource_archive_toggle(
    resource_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = require_role(request, RESOURCE_ROLES)
    if isinstance(user, RedirectResponse):
        return user

    r = db.query(Resource).filter(Resource.id == resource_id).first()
    if not r:
        return RedirectResponse(url="/admin/resources", status_code=302)

    r.is_archived = not bool(r.is_archived)
    r.updated_by = user["id"]
    db.commit()

    verb = "archived" if r.is_archived else "restored"
    log_action(
        db, user["id"], verb, "resources", r.id,
        f"Resource '{r.name}' {verb}.",
    )

    url = (
        f"/admin/resources?archived=1&success=Resource+{verb}"
        if r.is_archived
        else f"/admin/resources?success=Resource+{verb}"
    )
    return RedirectResponse(url=url, status_code=302)


# ─────────────────────────────────────────────────────────────────────
# WEEK 7 — VEHICLE & EQUIPMENT MONITORING (Module B)
# Roles: admin, cdrrmo_staff, cfau_oic
# Tracks operational disaster-response equipment.
# ─────────────────────────────────────────────────────────────────────

# Client-aligned status labels for the UI (maps enum value → display).
EQUIPMENT_STATUS_LABELS = {
    "available": "Available",
    "deployed": "Deployed",
    "under_repair": "Under Repair",
    "unserviceable": "Unserviceable",
    # Legacy values — mapped to the closest client term so old rows still
    # display sensibly without us mutating data.
    "serviceable": "Available (legacy)",
    "not_serviceable": "Unserviceable (legacy)",
}

# Statuses exposed in the *change-status* dropdown. Legacy values are
# intentionally omitted to steer users onto client-aligned terms.
EQUIPMENT_STATUS_CHOICES = ["available", "deployed", "under_repair", "unserviceable"]

EQUIPMENT_TYPE_LABELS = {
    "fire_truck": "Fire Truck",
    "ambulance": "Ambulance",
    "rescue_vehicle": "Rescue Vehicle",
    "generator": "Generator",
    "chainsaw": "Chainsaw",
    "rescue_boat": "Rescue Boat",
    "radio": "Radio",
    "flashlight": "Flashlight",
    "life_vest": "Life Vest",
    "other": "Other",
}

# Legacy statuses fold into their client-aligned bucket so a fleet snapshot
# reads in the four terms the client uses, without mutating stored rows.
_EQUIPMENT_STATUS_BUCKET = {
    "available": "available",
    "serviceable": "available",
    "deployed": "deployed",
    "under_repair": "under_repair",
    "unserviceable": "unserviceable",
    "not_serviceable": "unserviceable",
}


def equipment_status_breakdown(db):
    """Fleet snapshot over active (non-archived) equipment:
    {"counts": {status_value: n}, "total": n}. Shared by the Logistics and
    CFAU dashboards. A unit with an unrecognised status is counted in the
    total but in no bucket, so the buckets never overstate the fleet."""
    counts = {v: 0 for v in EQUIPMENT_STATUS_CHOICES}
    units = db.query(Equipment).filter(Equipment.is_archived == False).all()
    for e in units:
        bucket = _EQUIPMENT_STATUS_BUCKET.get(e.status.value if e.status else "")
        if bucket:
            counts[bucket] += 1
    return {"counts": counts, "total": len(units)}


@router.get("/equipment", response_class=HTMLResponse)
def equipment_list(
    request: Request,
    db: Session = Depends(get_db),
    q: Optional[str] = None,
    equipment_type: Optional[str] = None,
    status: Optional[str] = None,
    archived: Optional[str] = None,
    page: Optional[str] = None,
    per_page: Optional[str] = None,
):
    user = require_role(request, EQUIPMENT_ROLES)
    if isinstance(user, RedirectResponse):
        return user

    query = db.query(Equipment)
    show_archived = (archived == "1")
    query = query.filter(Equipment.is_archived == show_archived)

    if q:
        like = f"%{q.strip()}%"
        query = query.filter(
            (Equipment.name.ilike(like)) | (Equipment.plate_or_serial.ilike(like))
        )
    if equipment_type and equipment_type in {t.value for t in EquipmentType}:
        query = query.filter(Equipment.equipment_type == EquipmentType(equipment_type))
    if status and status in {s.value for s in EquipmentStatus}:
        query = query.filter(Equipment.status == EquipmentStatus(status))

    rows = query.order_by(Equipment.name).all()

    # Repair-follow-up reminders, keyed by equipment id (archived excluded
    # by the helper). Reused as-is for both the badges and the count.
    reminders = equipment_repair_reminders(db)

    view_rows = []
    for e in rows:
        reminder = reminders.get(e.id)
        view_rows.append({
            "id": e.id,
            "name": e.name,
            "type_value": e.equipment_type.value if e.equipment_type else "",
            "type_label": EQUIPMENT_TYPE_LABELS.get(
                e.equipment_type.value if e.equipment_type else "", "—"
            ),
            "status_value": e.status.value if e.status else "",
            "status_label": EQUIPMENT_STATUS_LABELS.get(
                e.status.value if e.status else "", "—"
            ),
            "plate_or_serial": e.plate_or_serial or "—",
            "assigned": e.assigned_to_user.username if e.assigned_to_user else "—",
            "last_inspected": e.last_inspected,
            "is_archived": e.is_archived,
            "repair_reminder": reminder["state"] if reminder else None,
        })

    # Summary cards for at-a-glance fleet readiness.
    active = db.query(Equipment).filter(Equipment.is_archived == False).all()
    summary = {
        "total": len(active),
        "available": sum(
            1 for x in active
            if x.status and x.status.value in ("available", "serviceable")
        ),
        "deployed": sum(1 for x in active if x.status and x.status.value == "deployed"),
        "under_repair": sum(1 for x in active if x.status and x.status.value == "under_repair"),
        "unserviceable": sum(
            1 for x in active
            if x.status and x.status.value in ("unserviceable", "not_serviceable")
        ),
        # Distinct non-archived assets with an active repair reminder.
        "repair_attention": len(reminders),
    }

    page_obj = paginate(view_rows, parse_page(page), parse_per_page(per_page))
    base_query = build_base_query({
        "q": q or "", "equipment_type": equipment_type or "",
        "status": status or "", "archived": "1" if show_archived else "",
    })

    return templates.TemplateResponse(
        request=request,
        name="admin/equipment_list.html",
        context={
            "user": user,
            "active_nav": "equipment",
            "rows": page_obj.items,
            "page_obj": page_obj,
            "base_query": base_query,
            "summary": summary,
            "types": [(t.value, EQUIPMENT_TYPE_LABELS.get(t.value, t.value.title()))
                      for t in EquipmentType],
            "statuses": [(s.value, EQUIPMENT_STATUS_LABELS.get(s.value, s.value.title()))
                         for s in EquipmentStatus],
            "status_choices": [(v, EQUIPMENT_STATUS_LABELS[v]) for v in EQUIPMENT_STATUS_CHOICES],
            "f_q": q or "",
            "f_type": equipment_type or "",
            "f_status": status or "",
            "f_archived": "1" if show_archived else "",
            "show_archived": show_archived,
            # Default + max for the status modal's date-time field (PHT).
            "now_local": datetime.now(_PHT).strftime("%Y-%m-%dT%H:%M"),
        },
    )


@router.get("/equipment/new", response_class=HTMLResponse)
def equipment_new_form(request: Request, db: Session = Depends(get_db)):
    user = require_role(request, EQUIPMENT_ROLES)
    if isinstance(user, RedirectResponse):
        return user
    return templates.TemplateResponse(
        request=request,
        name="admin/equipment_form.html",
        context={
            "user": user,
            "active_nav": "equipment",
            "edit_mode": False,
            "target": None,
            "types": [(t.value, EQUIPMENT_TYPE_LABELS.get(t.value, t.value.title()))
                      for t in EquipmentType],
            "status_choices": [(v, EQUIPMENT_STATUS_LABELS[v]) for v in EQUIPMENT_STATUS_CHOICES],
            "error": None,
        },
    )


@router.post("/equipment/new")
def equipment_create(
    request: Request,
    db: Session = Depends(get_db),
    name: str = Form(...),
    equipment_type: str = Form(...),
    status: str = Form("available"),
    plate_or_serial: str = Form(""),
    last_inspected: str = Form(""),
):
    user = require_role(request, EQUIPMENT_ROLES)
    if isinstance(user, RedirectResponse):
        return user

    def render_error(msg):
        return templates.TemplateResponse(
            request=request,
            name="admin/equipment_form.html",
            context={
                "user": user,
                "active_nav": "equipment",
                "edit_mode": False,
                "target": None,
                "types": [(t.value, EQUIPMENT_TYPE_LABELS.get(t.value, t.value.title()))
                          for t in EquipmentType],
                "status_choices": [(v, EQUIPMENT_STATUS_LABELS[v]) for v in EQUIPMENT_STATUS_CHOICES],
                "error": msg,
            },
        )

    name = name.strip()
    if not name:
        return render_error("Equipment name is required.")
    if equipment_type not in {t.value for t in EquipmentType}:
        return render_error("Invalid equipment type.")
    if status not in {s.value for s in EquipmentStatus}:
        status = "available"

    e = Equipment(
        name=name,
        equipment_type=EquipmentType(equipment_type),
        status=EquipmentStatus(status),
        plate_or_serial=plate_or_serial.strip() or None,
        last_inspected=_parse_date_or_none(last_inspected),
        is_archived=False,
    )
    db.add(e)
    db.commit()
    db.refresh(e)

    log_action(
        db, user["id"], "created", "equipment", e.id,
        f"Created equipment '{e.name}' ({e.equipment_type.value}, status={e.status.value})",
    )

    return RedirectResponse(
        url="/admin/equipment?success=Equipment+created+successfully",
        status_code=302,
    )


@router.get("/equipment/{equipment_id}/edit", response_class=HTMLResponse)
def equipment_edit_form(equipment_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_role(request, EQUIPMENT_ROLES)
    if isinstance(user, RedirectResponse):
        return user
    e = db.query(Equipment).filter(Equipment.id == equipment_id).first()
    if not e:
        return RedirectResponse(url="/admin/equipment", status_code=302)

    # Repair follow-up for this unit (most-relevant report, if any).
    reminder = equipment_repair_reminders(db).get(e.id)
    return templates.TemplateResponse(
        request=request,
        name="admin/equipment_form.html",
        context={
            "user": user,
            "active_nav": "equipment",
            "edit_mode": True,
            "target": e,
            "types": [(t.value, EQUIPMENT_TYPE_LABELS.get(t.value, t.value.title()))
                      for t in EquipmentType],
            "status_choices": [(v, EQUIPMENT_STATUS_LABELS[v]) for v in EQUIPMENT_STATUS_CHOICES],
            "error": None,
            "repair_reminder": reminder["state"] if reminder else None,
            "repair_report": reminder["report"] if reminder else None,
            "live_status_label": EQUIPMENT_STATUS_LABELS.get(
                e.status.value if e.status else "", "—"
            ),
        },
    )


@router.post("/equipment/{equipment_id}/edit")
def equipment_edit(
    equipment_id: int,
    request: Request,
    db: Session = Depends(get_db),
    name: str = Form(...),
    equipment_type: str = Form(...),
    plate_or_serial: str = Form(""),
    last_inspected: str = Form(""),
):
    user = require_role(request, EQUIPMENT_ROLES)
    if isinstance(user, RedirectResponse):
        return user
    e = db.query(Equipment).filter(Equipment.id == equipment_id).first()
    if not e:
        return RedirectResponse(url="/admin/equipment", status_code=302)

    # Status is changed via the dedicated status-change action so the
    # audit log captures it as a status transition, not a generic edit.
    changes = []
    new_name = name.strip()
    if new_name and new_name != e.name:
        changes.append(f"name: '{e.name}' → '{new_name}'")
        e.name = new_name

    if equipment_type in {t.value for t in EquipmentType} and e.equipment_type.value != equipment_type:
        changes.append(f"type: {e.equipment_type.value} → {equipment_type}")
        e.equipment_type = EquipmentType(equipment_type)

    new_ps = plate_or_serial.strip() or None
    if new_ps != e.plate_or_serial:
        changes.append(f"plate_or_serial: '{e.plate_or_serial or ''}' → '{new_ps or ''}'")
        e.plate_or_serial = new_ps

    new_insp = _parse_date_or_none(last_inspected)
    if new_insp != e.last_inspected:
        changes.append(f"last_inspected: {e.last_inspected} → {new_insp}")
        e.last_inspected = new_insp

    db.commit()

    if changes:
        log_action(
            db, user["id"], "updated", "equipment", e.id,
            f"Updated equipment '{e.name}': " + "; ".join(changes),
        )

    return RedirectResponse(
        url="/admin/equipment?success=Equipment+updated+successfully",
        status_code=302,
    )


@router.post("/equipment/{equipment_id}/status")
def equipment_status_change(
    equipment_id: int,
    request: Request,
    db: Session = Depends(get_db),
    status: str = Form(...),
    reason: str = Form(""),
    deployed_to: str = Form(""),
    occurred_at: str = Form(""),
):
    user = require_role(request, EQUIPMENT_ROLES)
    if isinstance(user, RedirectResponse):
        return user
    e = db.query(Equipment).filter(Equipment.id == equipment_id).first()
    if not e:
        return RedirectResponse(url="/admin/equipment", status_code=302)

    if status not in EQUIPMENT_STATUS_CHOICES:
        return RedirectResponse(
            url="/admin/equipment?error=Invalid+status", status_code=302
        )

    old = e.status.value if e.status else "—"
    if status == old:
        return RedirectResponse(
            url="/admin/equipment?success=Status+unchanged", status_code=302
        )

    # A unit going to "deployed" must name where; repairs and returns
    # to the motorpool may leave the location blank.
    movement, err = _movement_fields(
        reason, deployed_to, occurred_at, location_required=(status == "deployed")
    )
    if err:
        return RedirectResponse(
            url=f"/admin/equipment?error={quote_plus(err)}", status_code=302
        )

    e.status = EquipmentStatus(status)
    db.commit()

    note = f" — reason: {movement['reason']}"
    if movement["deployed_to"]:
        note += f"; deployed to: {movement['deployed_to']}"
    log_action(
        db, user["id"], "status_changed", "equipment", e.id,
        f"Equipment '{e.name}' status {old} → {status}{note}",
        reason=movement["reason"],
        deployed_to=movement["deployed_to"],
        occurred_at=movement["occurred_at"],
    )

    return RedirectResponse(
        url="/admin/equipment?success=Status+updated+successfully",
        status_code=302,
    )


@router.post("/equipment/{equipment_id}/archive")
def equipment_archive_toggle(
    equipment_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = require_role(request, EQUIPMENT_ROLES)
    if isinstance(user, RedirectResponse):
        return user
    e = db.query(Equipment).filter(Equipment.id == equipment_id).first()
    if not e:
        return RedirectResponse(url="/admin/equipment", status_code=302)

    e.is_archived = not bool(e.is_archived)
    db.commit()

    verb = "archived" if e.is_archived else "restored"
    log_action(
        db, user["id"], verb, "equipment", e.id,
        f"Equipment '{e.name}' {verb}.",
    )

    url = "/admin/equipment?archived=1&success=" + verb.title() if e.is_archived \
        else "/admin/equipment?success=" + verb.title()
    return RedirectResponse(url=url, status_code=302)


# ─────────────────────────────────────────────────────────────────────
# WEEK 8 — EQUIPMENT SERVICEABILITY REVIEW (admin side of Module B)
# Roles: admin only. CFAU files reports under /cfau/serviceability;
# the admin reviews them here, updates the workflow status, and adds
# remarks. Reuses the existing EquipmentReport model + AuditLog.
# ─────────────────────────────────────────────────────────────────────

SERVICEABILITY_WORKFLOW_LABELS = {
    "draft": "Draft",
    "submitted": "Submitted",
    "reviewed": "Reviewed",
    "resolved": "Resolved",
}
SERVICEABILITY_FINDING_LABELS = {
    "serviceable": "Serviceable",
    "under_repair": "Under Repair",
    "unserviceable": "Unserviceable",
    # Legacy finding values still render sensibly.
    "not_serviceable": "Unserviceable (legacy)",
    "available": "Serviceable",
    "deployed": "Deployed",
}
SERVICEABILITY_TYPE_LABELS = {
    "inspection": "Inspection",
    "maintenance": "Maintenance Finding",
    "serviceability": "Serviceability Assessment",
}

# Maps a report *finding* (EquipmentStatus subset) onto the live fleet
# status terms used by the Week 7 monitoring module. A "serviceable"
# finding becomes "available" so the live module stays on client-aligned
# vocabulary rather than the legacy value.
FINDING_TO_LIVE_STATUS = {
    "serviceable": "available",
    "under_repair": "under_repair",
    "unserviceable": "unserviceable",
}

# Urgency drives the admin review queue: higher rank surfaces first so a
# critical report (e.g. an unserviceable ambulance) is seen immediately.
URGENCY_RANK = {"critical": 3, "high": 2, "moderate": 1, "low": 0}

# Live equipment statuses that mean a unit is still out of service. Repair
# reminders only fire while the equipment sits in one of these (the legacy
# not_serviceable value is included).
_REPAIR_OPEN_STATUSES = {"under_repair", "unserviceable", "not_serviceable"}


def repair_reminder_state(report, today=None):
    """Repair-reminder state for an EquipmentReport — one of:
        "overdue"   — repair_scheduled_date is before today
        "due_today" — repair_scheduled_date is today
        None        — no reminder

    A reminder applies only while a scheduled repair date has arrived
    (today or past) AND the linked equipment is still out of service
    (under_repair / unserviceable / legacy not_serviceable). It clears by
    itself once the unit is returned to service — Equipment.status stays
    the source of truth and is never changed here. Report workflow state
    is intentionally ignored: a resolved report whose equipment was never
    returned to service keeps reminding.
    """
    if report is None or report.repair_scheduled_date is None:
        return None
    equipment = report.equipment
    if not equipment or not equipment.status:
        return None
    if equipment.status.value not in _REPAIR_OPEN_STATUSES:
        return None
    today = today or date.today()
    if report.repair_scheduled_date < today:
        return "overdue"
    if report.repair_scheduled_date == today:
        return "due_today"
    return None


# Reminder severity for picking the "most relevant" report per asset.
_REPAIR_REMINDER_RANK = {"overdue": 2, "due_today": 1}


def equipment_repair_reminders(db, today=None):
    """Map of {equipment_id: {"state", "report"}} for active, non-archived
    equipment with at least one due/overdue repair whose unit is still out
    of service. Deduped per asset: when a unit has several qualifying
    reports the most relevant one wins — Overdue over Due Today, then the
    earliest repair date. Reuses repair_reminder_state() for the rule so no
    business logic is duplicated."""
    today = today or date.today()
    reports = (
        db.query(EquipmentReport)
        .join(Equipment, EquipmentReport.equipment_id == Equipment.id)
        .filter(Equipment.is_archived == False)
        .filter(EquipmentReport.repair_scheduled_date.isnot(None))
        .filter(EquipmentReport.repair_scheduled_date <= today)
        .all()
    )
    best = {}
    for r in reports:
        state = repair_reminder_state(r, today)
        if state is None:
            continue
        current = best.get(r.equipment_id)
        candidate = (_REPAIR_REMINDER_RANK[state], r.repair_scheduled_date, r)
        if current is None:
            best[r.equipment_id] = candidate
        else:
            # Higher rank wins; tie broken by the earliest repair date.
            if (candidate[0] > current[0]
                    or (candidate[0] == current[0] and candidate[1] < current[1])):
                best[r.equipment_id] = candidate
    return {
        eid: {"state": ("overdue" if rank == 2 else "due_today"), "report": rep}
        for eid, (rank, _d, rep) in best.items()
    }


def assets_needing_repair_attention(db, today=None):
    """Set of distinct (non-archived) Equipment ids with an active repair
    reminder. Single source of truth derived from
    equipment_repair_reminders() so the dashboard, serviceability list and
    equipment module all agree."""
    return set(equipment_repair_reminders(db, today).keys())


@router.get("/serviceability", response_class=HTMLResponse)
def serviceability_review_list(
    request: Request,
    db: Session = Depends(get_db),
    status: Optional[str] = None,
    urgency: Optional[str] = None,
    page: Optional[str] = None,
    per_page: Optional[str] = None,
):
    user = require_role(request, ["admin"])
    if isinstance(user, RedirectResponse):
        return user

    query = db.query(EquipmentReport)
    if status and status in {s.value for s in ServiceabilityStatus}:
        query = query.filter(EquipmentReport.report_status == ServiceabilityStatus(status))
    if urgency and urgency in {u.value for u in Urgency}:
        query = query.filter(EquipmentReport.urgency == Urgency(urgency))

    reports = query.all()

    rows = []
    for r in reports:
        urgency_value = r.urgency.value if r.urgency else "moderate"
        status_value = r.report_status.value if r.report_status else "draft"
        # A report needs attention when it is submitted (awaiting review);
        # high/critical urgency in that state is flagged as priority.
        needs_review = status_value == "submitted"
        is_priority = needs_review and urgency_value in ("high", "critical")
        rows.append({
            "id": r.id,
            "title": r.title or "(untitled)",
            "equipment": r.equipment.name if r.equipment else "—",
            "repair_scheduled_date": r.repair_scheduled_date,
            "repair_reminder": repair_reminder_state(r),
            "report_type": SERVICEABILITY_TYPE_LABELS.get(r.report_type, r.report_type or "—"),
            "finding": SERVICEABILITY_FINDING_LABELS.get(
                r.status.value if r.status else "", "—"
            ),
            "urgency": urgency_value,
            "urgency_label": urgency_value.title(),
            "urgency_rank": URGENCY_RANK.get(urgency_value, 1),
            "report_status": status_value,
            "report_status_label": SERVICEABILITY_WORKFLOW_LABELS.get(status_value, "Draft"),
            "reporter": r.reported_by_user.username if r.reported_by_user else "—",
            "reported_at": r.reported_at,
            "submitted_at": r.submitted_at,
            "needs_review": needs_review,
            "is_priority": is_priority,
        })

    # Priority queue ordering: urgency is the primary key (critical → low),
    # so the most urgent reports always surface at the top by default.
    # Within the same urgency, items still awaiting review come first, then
    # the most recent. reported_at is the final tiebreaker (datetime.min
    # guards against NULLs).
    rows.sort(
        key=lambda x: (
            x["urgency_rank"],
            x["needs_review"],
            x["submitted_at"] or x["reported_at"] or datetime.min,
        ),
        reverse=True,
    )

    all_reports = db.query(EquipmentReport).all()
    summary = {
        "submitted": sum(1 for x in all_reports if x.report_status == ServiceabilityStatus.submitted),
        "reviewed": sum(1 for x in all_reports if x.report_status == ServiceabilityStatus.reviewed),
        "resolved": sum(1 for x in all_reports if x.report_status == ServiceabilityStatus.resolved),
        "total": len(all_reports),
        # Submitted reports at high/critical urgency — the ones that should
        # be acted on immediately.
        "priority": sum(
            1 for x in all_reports
            if x.report_status == ServiceabilityStatus.submitted
            and x.urgency and x.urgency.value in ("high", "critical")
        ),
        # Distinct assets (not reports) with an active repair reminder.
        "repair_attention": len(assets_needing_repair_attention(db)),
    }

    page_obj = paginate(rows, parse_page(page), parse_per_page(per_page))
    base_query = build_base_query({"status": status or "", "urgency": urgency or ""})

    return templates.TemplateResponse(
        request=request,
        name="admin/serviceability_review.html",
        context={
            "user": user,
            "active_nav": "serviceability_review",
            "rows": page_obj.items,
            "page_obj": page_obj,
            "base_query": base_query,
            "summary": summary,
            "statuses": [(s.value, SERVICEABILITY_WORKFLOW_LABELS[s.value]) for s in ServiceabilityStatus],
            "urgencies": [(u.value, u.value.title()) for u in Urgency],
            "f_status": status or "",
            "f_urgency": urgency or "",
            "detail": None,
        },
    )


@router.get("/serviceability/{report_id}", response_class=HTMLResponse)
def serviceability_review_detail(report_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_role(request, ["admin"])
    if isinstance(user, RedirectResponse):
        return user

    r = db.query(EquipmentReport).filter(EquipmentReport.id == report_id).first()
    if not r:
        return RedirectResponse(url="/admin/serviceability", status_code=302)

    # ── Admin-assisted live-status sync state ─────────────────────────
    # The finding and the live Equipment.status are kept separate; the
    # admin can optionally push the finding onto the live record.
    equipment = r.equipment
    finding_value = r.status.value if r.status else None
    mapped_live = FINDING_TO_LIVE_STATUS.get(finding_value, finding_value)
    live_status_value = equipment.status.value if (equipment and equipment.status) else None

    # Only submitted/reviewed/resolved reports may be applied (not drafts),
    # and only when the live status actually differs from the finding.
    statuses_differ = (
        equipment is not None
        and mapped_live is not None
        and live_status_value != mapped_live
    )
    can_apply = statuses_differ and r.report_status != ServiceabilityStatus.draft

    return templates.TemplateResponse(
        request=request,
        name="admin/serviceability_review_detail.html",
        context={
            "user": user,
            "active_nav": "serviceability_review",
            "r": r,
            "finding_label": SERVICEABILITY_FINDING_LABELS.get(finding_value or "", "—"),
            "report_type_label": SERVICEABILITY_TYPE_LABELS.get(r.report_type, r.report_type or "—"),
            "workflow_label": SERVICEABILITY_WORKFLOW_LABELS.get(
                r.report_status.value if r.report_status else "draft", "Draft"
            ),
            # Only reports that have been submitted by CFAU can be acted on.
            "can_review": r.report_status in (
                ServiceabilityStatus.submitted, ServiceabilityStatus.reviewed
            ),
            # A resolved report can be reopened back to Reviewed.
            "can_reopen": r.report_status == ServiceabilityStatus.resolved,
            # Live-status sync context.
            "live_status_label": EQUIPMENT_STATUS_LABELS.get(live_status_value or "", "—"),
            "mapped_live_label": EQUIPMENT_STATUS_LABELS.get(mapped_live or "", "—"),
            "can_apply_finding": can_apply,
            "finding_applied": r.finding_applied_at is not None,
            # Repair-scheduling reminder ("overdue" / "due_today" / None).
            "repair_reminder": repair_reminder_state(r),
        },
    )


@router.post("/serviceability/{report_id}/apply-finding")
def serviceability_apply_finding(report_id: int, request: Request, db: Session = Depends(get_db)):
    """Admin-assisted sync: push this report's finding onto the live
    Equipment.status. Never triggered by review/resolve — it is an
    explicit, separate action so a human always confirms a fleet change.
    """
    user = require_role(request, ["admin"])
    if isinstance(user, RedirectResponse):
        return user

    r = db.query(EquipmentReport).filter(EquipmentReport.id == report_id).first()
    if not r:
        return RedirectResponse(url="/admin/serviceability", status_code=302)

    if r.report_status == ServiceabilityStatus.draft:
        return RedirectResponse(
            url=f"/admin/serviceability/{report_id}?error=Report+is+still+a+draft",
            status_code=302,
        )

    equipment = r.equipment
    finding_value = r.status.value if r.status else None
    mapped_live = FINDING_TO_LIVE_STATUS.get(finding_value, finding_value)
    if not equipment or not mapped_live or mapped_live not in {s.value for s in EquipmentStatus}:
        return RedirectResponse(
            url=f"/admin/serviceability/{report_id}?error=Cannot+apply+finding",
            status_code=302,
        )

    old_status = equipment.status.value if equipment.status else "—"
    if old_status == mapped_live:
        # Already in sync — just record that the finding has been applied.
        r.finding_applied_at = datetime.utcnow()
        db.commit()
        return RedirectResponse(
            url=f"/admin/serviceability/{report_id}?success=Live+status+already+matches",
            status_code=302,
        )

    equipment.status = EquipmentStatus(mapped_live)
    r.finding_applied_at = datetime.utcnow()
    db.commit()

    log_action(
        db, user["id"], "status_changed", "equipment", equipment.id,
        f"Equipment #{equipment.id} '{equipment.name}' live status "
        f"{old_status} → {mapped_live}, applied from serviceability report "
        f"#{r.id} (finding: {SERVICEABILITY_FINDING_LABELS.get(finding_value, finding_value)})",
    )

    return RedirectResponse(
        url=f"/admin/serviceability/{report_id}?success=Live+equipment+status+updated",
        status_code=302,
    )


@router.post("/serviceability/{report_id}/review")
def serviceability_review_action(
    report_id: int,
    request: Request,
    db: Session = Depends(get_db),
    new_status: str = Form(...),     # "reviewed" or "resolved"
    admin_remarks: str = Form(""),
    repair_scheduled_date: str = Form(""),   # optional ISO date, may be blank
    repair_notes: str = Form(""),
):
    user = require_role(request, ["admin"])
    if isinstance(user, RedirectResponse):
        return user

    r = db.query(EquipmentReport).filter(EquipmentReport.id == report_id).first()
    if not r:
        return RedirectResponse(url="/admin/serviceability", status_code=302)

    if new_status not in ("reviewed", "resolved"):
        return RedirectResponse(
            url=f"/admin/serviceability/{report_id}?error=Invalid+status",
            status_code=302,
        )
    # Drafts have not been submitted yet — nothing for the admin to review.
    if r.report_status == ServiceabilityStatus.draft:
        return RedirectResponse(
            url=f"/admin/serviceability/{report_id}?error=Report+is+still+a+draft",
            status_code=302,
        )

    # Optional repair scheduling. Blank clears the date; a present value
    # must parse as an ISO date. This is a reminder aid only and never
    # touches Equipment.status.
    sched = repair_scheduled_date.strip()
    if sched:
        try:
            r.repair_scheduled_date = date.fromisoformat(sched)
        except ValueError:
            return RedirectResponse(
                url=f"/admin/serviceability/{report_id}?error=Invalid+repair+date",
                status_code=302,
            )
    else:
        r.repair_scheduled_date = None
    r.repair_notes = repair_notes.strip() or None

    old = r.report_status.value if r.report_status else "—"
    r.report_status = ServiceabilityStatus(new_status)
    if admin_remarks.strip():
        r.admin_remarks = admin_remarks.strip()
    # Record who reviewed it (set on first review, kept thereafter).
    if r.reviewed_by is None:
        r.reviewed_by = user["id"]
        r.reviewed_at = datetime.utcnow()
    db.commit()

    note = f" — remarks: {admin_remarks.strip()}" if admin_remarks.strip() else ""
    if r.repair_scheduled_date:
        note += f" — repair scheduled {r.repair_scheduled_date.isoformat()}"
    log_action(
        db, user["id"], new_status, "equipment_reports", r.id,
        f"Serviceability report '{r.title}' {old} → {new_status}{note}",
    )

    return RedirectResponse(
        url=f"/admin/serviceability?success=Report+marked+{new_status}",
        status_code=302,
    )


@router.post("/serviceability/{report_id}/reopen")
def serviceability_reopen(report_id: int, request: Request, db: Session = Depends(get_db)):
    """Reopen a resolved report back to Reviewed — a lightweight guard
    against accidental resolution. Reuses the existing workflow states (no
    new enum) and never touches Equipment.status; repair reminders are
    driven by Equipment.status, so they are unaffected."""
    user = require_role(request, ["admin"])
    if isinstance(user, RedirectResponse):
        return user

    r = db.query(EquipmentReport).filter(EquipmentReport.id == report_id).first()
    if not r:
        return RedirectResponse(url="/admin/serviceability", status_code=302)

    if r.report_status != ServiceabilityStatus.resolved:
        return RedirectResponse(
            url=f"/admin/serviceability/{report_id}?error=Only+resolved+reports+can+be+reopened",
            status_code=302,
        )

    r.report_status = ServiceabilityStatus.reviewed
    db.commit()

    log_action(
        db, user["id"], "reopened", "equipment_reports", r.id,
        f"Serviceability report '{r.title}' reopened (resolved → reviewed)",
    )

    return RedirectResponse(
        url=f"/admin/serviceability/{report_id}?success=Report+reopened",
        status_code=302,
    )


# ─────────────────────────────────────────────────────────────────────
# WEEK 9 (Part A) — ADMIN POST-INCIDENT REPORTS (thin management layer)
# Read-only oversight over the SAME IncidentReport records CFAU files.
# IncidentReport stays the single source of truth — no new model, and
# the admin cannot edit/submit submitted reports here (view + filter
# + open detail only). Mirrors the Serviceability Review layer.
# ─────────────────────────────────────────────────────────────────────

# Only draft / submitted are meaningful for post-incident reports.
INCIDENT_REPORT_WORKFLOW_LABELS = {
    "draft": "Draft",
    "submitted": "Submitted",
    "reviewed": "Reviewed",
    "resolved": "Resolved",
}

# Week 8.1 tags CFAU post-incident uploads in extracted_data JSON with this
# kind, and links the produced report via `produced_incident_report_id`.
_UPLOAD_KIND_POST_INCIDENT = "post_incident"


def _source_uploads_by_report_id(db) -> dict:
    """Map produced IncidentReport id → its source UploadedReport.

    Built in ONE query so the admin list can show provenance ("Uploaded"
    vs "Manual") and link to the source upload without an N+1 reverse
    scan. Reuses the existing Week 8.1 JSON linkage — there is no FK
    column between the two tables.
    """
    uploads = (
        db.query(UploadedReport)
        .filter(UploadedReport.extracted_data.isnot(None))
        .all()
    )
    mapping = {}
    for up in uploads:
        data = up.extracted_data or {}
        if data.get("report_kind") == _UPLOAD_KIND_POST_INCIDENT:
            produced = data.get("produced_incident_report_id")
            if produced:
                mapping[produced] = up
    return mapping


@router.get("/incident-reports", response_class=HTMLResponse)
def incident_reports_list(
    request: Request,
    db: Session = Depends(get_db),
    reporter: Optional[str] = None,
    barangay: Optional[str] = None,
    disaster_type: Optional[str] = None,
    status: Optional[str] = None,
    origin: Optional[str] = None,
    page: Optional[str] = None,
    per_page: Optional[str] = None,
):
    user = require_role(request, ["admin"])
    if isinstance(user, RedirectResponse):
        return user

    # Empty form values arrive as "" (selects left blank). Treat them as None
    # and coerce the numeric filters safely — never parse "" as int.
    reporter_id = int(reporter) if (reporter or "").strip().isdigit() else None
    barangay_id = int(barangay) if (barangay or "").strip().isdigit() else None

    # Join Incident so barangay / disaster-type filters can be applied.
    query = db.query(IncidentReport).join(
        Incident, IncidentReport.incident_id == Incident.id
    )
    if reporter_id:
        query = query.filter(IncidentReport.submitted_by == reporter_id)
    if barangay_id:
        query = query.filter(Incident.barangay_id == barangay_id)
    if disaster_type and disaster_type in {d.value for d in DisasterType}:
        query = query.filter(Incident.disaster_type == DisasterType(disaster_type))
    if status and status in {s.value for s in ServiceabilityStatus}:
        query = query.filter(IncidentReport.report_status == ServiceabilityStatus(status))

    reports = query.order_by(IncidentReport.created_at.desc()).all()

    # One batch lookup for provenance — avoids an N+1 reverse scan.
    source_map = _source_uploads_by_report_id(db)
    origin_filter = origin if origin in ("uploaded", "manual") else ""

    rows = []
    for r in reports:
        inc = r.incident
        status_value = r.report_status.value if r.report_status else "draft"
        src = source_map.get(r.id)
        origin_value = "uploaded" if src else "manual"
        # Origin is derived (not a column), so it's filtered in-memory.
        if origin_filter and origin_value != origin_filter:
            continue
        rows.append({
            "id": r.id,
            "disaster_type": (inc.disaster_type.value.replace("_", " ").title()
                              if inc and inc.disaster_type else "—"),
            "barangay": inc.barangay.name if (inc and inc.barangay) else "—",
            "date_occurred": inc.date_occurred if inc else None,
            "personnel_count": r.personnel_count or 0,
            "report_status": status_value,
            "report_status_label": INCIDENT_REPORT_WORKFLOW_LABELS.get(status_value, "Draft"),
            "reporter": r.submitted_by_user.username if r.submitted_by_user else "—",
            "created_at": r.created_at,
            "origin": origin_value,
            "source_upload_id": src.id if src else None,
        })

    # Summary over ALL reports (independent of the active filters).
    all_reports = db.query(IncidentReport).all()
    summary = {
        "total": len(all_reports),
        "draft": sum(1 for x in all_reports if x.report_status == ServiceabilityStatus.draft),
        "submitted": sum(1 for x in all_reports if x.report_status == ServiceabilityStatus.submitted),
    }

    # Filter dropdown options.
    reporter_users = (
        db.query(User)
        .join(IncidentReport, IncidentReport.submitted_by == User.id)
        .distinct()
        .order_by(User.username)
        .all()
    )
    barangays = db.query(Barangay).order_by(Barangay.name).all()

    page_obj = paginate(rows, parse_page(page), parse_per_page(per_page))
    base_query = build_base_query({
        "reporter": reporter or "", "barangay": barangay or "",
        "disaster_type": disaster_type or "", "status": status or "",
        "origin": origin_filter or "",
    })

    return templates.TemplateResponse(
        request=request,
        name="admin/incident_reports_list.html",
        context={
            "user": user,
            "active_nav": "incident_reports_review",
            "rows": page_obj.items,
            "page_obj": page_obj,
            "base_query": base_query,
            "summary": summary,
            "reporters": [(u.id, u.username) for u in reporter_users],
            "barangays": [(b.id, b.name) for b in barangays],
            "disaster_types": [(d.value, d.value.replace("_", " ").title()) for d in DisasterType],
            "statuses": [("draft", "Draft"), ("submitted", "Submitted")],
            "origins": [("uploaded", "Uploaded"), ("manual", "Manual")],
            "f_reporter": reporter or "",
            "f_barangay": barangay or "",
            "f_disaster_type": disaster_type or "",
            "f_status": status or "",
            "f_origin": origin_filter,
        },
    )


@router.get("/incident-reports/{report_id}", response_class=HTMLResponse)
def incident_report_detail(report_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_role(request, ["admin"])
    if isinstance(user, RedirectResponse):
        return user

    r = db.query(IncidentReport).filter(IncidentReport.id == report_id).first()
    if not r:
        return RedirectResponse(url="/admin/incident-reports", status_code=302)

    inc = r.incident
    # Provenance: the source upload (if this report came from a CFAU upload).
    source_upload = _source_uploads_by_report_id(db).get(r.id)
    return templates.TemplateResponse(
        request=request,
        name="admin/incident_report_detail.html",
        context={
            "user": user,
            "active_nav": "incident_reports_review",
            "r": r,
            "incident": inc,
            "disaster_type": (inc.disaster_type.value.replace("_", " ").title()
                              if inc and inc.disaster_type else "—"),
            "barangay": inc.barangay.name if (inc and inc.barangay) else "—",
            "workflow_label": INCIDENT_REPORT_WORKFLOW_LABELS.get(
                r.report_status.value if r.report_status else "draft", "Draft"
            ),
            "source_upload": source_upload,
        },
    )