from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.database import get_db
from app.models import (
    User, UserRole, Barangay, AuditLog, Incident,
    Resource, Equipment, Population, EquipmentStatus,
    DisasterType, RiskLevel, Facility, FacilityType
)
from app.auth import require_role, hash_password
from app.analytics.simulator import compute_risk_score
from app.utils.geo import BARANGAY_COORDS
from typing import Optional
from datetime import date, datetime, timedelta, timezone
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