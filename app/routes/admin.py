from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.database import get_db
from app.models import (
    User, UserRole, Barangay, AuditLog, Incident,
    Resource, Equipment, Population, EquipmentStatus,
    DisasterType, RiskLevel, Facility, FacilityType,
    UploadedReport, UploadHistory, UploadEvent,
    ResourceCategory, EquipmentType, log_action,
)
from app.auth import require_role, hash_password
from app.analytics.simulator import compute_risk_score
from app.utils.geo import BARANGAY_COORDS
from typing import Optional
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode
import json

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
    recent_pops = db.query(Population).filter(
        Population.recorded_at >= six_months_ago
    ).count()
    data_relevance = round((recent_pops / total_barangays * 100)) if total_barangays > 0 else 0

    low_stock = [r for r in resources if r.quantity < r.restock_threshold]
    expiring = [
        r for r in resources
        if r.expiry_date and r.expiry_date <= date.today() + timedelta(days=30)
    ]
    active_alerts = len(low_stock) + len(expiring)

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
            "disaster_counts": json.dumps(disaster_counts),
            "yearly_data": json.dumps(yearly_data),
            "top5": top5,
            "all_scores": all_scores,
            "map_markers": json.dumps(map_markers),
            "recent_logs": recent_logs,
            "serviceable_count": serviceable,
            "total_equip": total_equip,
            "low_stock_count": len(low_stock),
            "expiring_count": len(expiring),
        }
    )


# ─────────────────────────────────────────────────────────────────────
# USER MANAGEMENT
# ─────────────────────────────────────────────────────────────────────

@router.get("/users", response_class=HTMLResponse)
def user_list(request: Request, db: Session = Depends(get_db)):
    user = require_role(request, ["admin"])
    if isinstance(user, RedirectResponse):
        return user

    all_users = db.query(User).order_by(User.created_at.desc()).all()
    return templates.TemplateResponse(
        request=request,
        name="admin/users.html",
        context={"user": user, "all_users": all_users}
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

_AUDIT_PER_PAGE = 25


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
    page: int = 1,
):
    user = require_role(request, ["admin"])
    if isinstance(user, RedirectResponse):
        return user

    # Empty form values arrive as "" (selects/inputs left blank). Treat them
    # as None and coerce the numeric user filter safely — never parse "" as int.
    uid = int(user_id) if (user_id or "").strip().isdigit() else None

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

    total = len(rows)
    total_pages = max(1, (total + _AUDIT_PER_PAGE - 1) // _AUDIT_PER_PAGE)
    page = max(1, min(page, total_pages))
    start = (page - 1) * _AUDIT_PER_PAGE
    page_rows = rows[start:start + _AUDIT_PER_PAGE]

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

    # Query string (filters minus page) for building pagination links.
    filter_params = {
        k: v for k, v in {
            "q": q or "", "user_id": uid or "", "action": action or "",
            "category": category or "", "date_from": date_from or "",
            "date_to": date_to or "",
        }.items() if v not in ("", None)
    }
    base_query = urlencode(filter_params)

    return templates.TemplateResponse(
        request=request,
        name="admin/audit_list.html",
        context={
            "user": user,
            "active_nav": "audit",
            "grouped": grouped,
            "total": total,
            "page": page,
            "total_pages": total_pages,
            "per_page": _AUDIT_PER_PAGE,
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

    return templates.TemplateResponse(
        request=request,
        name="admin/barangays_list.html",
        context={
            "user": user,
            "rows": rows,
            "q": q or "",
            "risk_filter": risk or "",
            "risk_levels": [r.value for r in RiskLevel],
            "total_count": len(rows),
        },
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

    population = db.query(Population).filter(
        Population.barangay_id == brgy.id
    ).order_by(Population.recorded_at.desc()).first()

    incidents = db.query(Incident).filter(
        Incident.barangay_id == brgy.id
    ).order_by(Incident.date_occurred.desc()).all()

    facilities = db.query(Facility).filter(
        Facility.barangay_id == brgy.id
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

    # ── Risk trends (rule-based) ─────────────────────────────────────
    flood_trend = _risk_trend(incidents, DisasterType.flood)
    fire_trend = _risk_trend(incidents, DisasterType.fire)

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

    return templates.TemplateResponse(
        request=request,
        name="admin/barangay_profile.html",
        context={
            "user": user,
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
            "flood_trend": flood_trend,
            "fire_trend": fire_trend,
            "facility_rows": facility_rows,
            "incident_counts": incident_counts_by_type,
            "recent_incidents": recent_incidents[:10],
            "hazard_types": [h.strip() for h in (brgy.hazard_types or "").split(",") if h.strip()],
        },
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

    return templates.TemplateResponse(
        request=request,
        name="admin/map.html",
        context={
            "user": user,
            "active_nav": "map",
            "barangays": barangays,
            "facility_types": facility_types,
            "statuses": statuses,
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
            "is_approximate_location": bool(f.is_approximate_location),
            "is_city_level": bool(f.is_city_level),
            "is_active": bool(f.is_active),
        })

    return JSONResponse(payload)


# ─────────────────────────────────────────────────────────────────────
# WEEK 7 — RESOURCE GOODS INVENTORY (Module A)
# Roles: admin, cdrrmo_staff
# Tracks consumable disaster-response resources (food packs, water,
# medicine, hygiene kits, blankets, tarpaulins, sleeping kits, …).
# ─────────────────────────────────────────────────────────────────────

RESOURCE_ROLES = ["admin", "cdrrmo_staff"]
EQUIPMENT_ROLES = ["admin", "cdrrmo_staff", "cfau_oic"]

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
    alert: Optional[str] = None,
    archived: Optional[str] = None,
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

    return templates.TemplateResponse(
        request=request,
        name="admin/resources_list.html",
        context={
            "user": user,
            "active_nav": "resources",
            "rows": view_rows,
            "summary": summary,
            "categories": [c.value for c in ResourceCategory],
            "f_q": q or "",
            "f_category": category or "",
            "f_alert": alert or "",
            "f_archived": "1" if show_archived else "",
            "show_archived": show_archived,
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

    r = Resource(
        name=name,
        category=ResourceCategory(category),
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


@router.post("/resources/{resource_id}/stock")
def resource_stock_change(
    resource_id: int,
    request: Request,
    db: Session = Depends(get_db),
    action: str = Form(...),       # "add" or "deduct"
    amount: int = Form(...),
    reason: str = Form(""),
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

    note = f" — reason: {reason.strip()}" if reason.strip() else ""
    log_action(
        db, user["id"], verb, "resources", r.id,
        f"Resource '{r.name}' quantity {before} → {after} ({'+' if action == 'add' else '-'}{amount} {r.unit or ''}){note}",
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


@router.get("/equipment", response_class=HTMLResponse)
def equipment_list(
    request: Request,
    db: Session = Depends(get_db),
    q: Optional[str] = None,
    equipment_type: Optional[str] = None,
    status: Optional[str] = None,
    archived: Optional[str] = None,
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

    view_rows = []
    for e in rows:
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
    }

    return templates.TemplateResponse(
        request=request,
        name="admin/equipment_list.html",
        context={
            "user": user,
            "active_nav": "equipment",
            "rows": view_rows,
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

    e.status = EquipmentStatus(status)
    db.commit()

    note = f" — reason: {reason.strip()}" if reason.strip() else ""
    log_action(
        db, user["id"], "status_changed", "equipment", e.id,
        f"Equipment '{e.name}' status {old} → {status}{note}",
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