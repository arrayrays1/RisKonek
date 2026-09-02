from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from typing import Optional
from datetime import datetime, timedelta, timezone
from app.database import get_db
from app.auth import require_role
from app.models import AuditLog, User, Resource, ResourceCategory
from app.services.contact_directory import build_directory_context
from app.services import global_search
from app.utils.pagination import (
    paginate, parse_per_page, parse_page, build_base_query,
)
# The audit trail's filtering / categorisation / day-grouping rules live with
# the admin module; the logistics view is a scoped projection of the SAME
# AuditLog rows, so it reuses them rather than re-deriving them.
from app.routes.admin import (
    _audit_filtered_rows, _audit_category, _audit_day_label,
    _audit_export_time, _pdf_safe,
    # Dashboard: the stockpile alert rule, the fleet snapshot and the
    # repair-attention set are all owned by the admin module — the
    # dashboard reads them, it does not re-derive them.
    _resource_alert, _resource_summary, equipment_status_breakdown,
    assets_needing_repair_attention, EQUIPMENT_STATUS_LABELS,
)
import csv
import io

router = APIRouter(prefix="/staff")
templates = Jinja2Templates(directory="app/templates")

# Audit logs are stored in UTC; display in Philippine Standard Time (UTC+8).
_PHT = timezone(timedelta(hours=8))

def _to_pht(dt):
    """Jinja filter: convert a UTC-naive or aware datetime to PHT and format."""
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_PHT).strftime('%B %d, %Y at %I:%M %p')

templates.env.filters['pht'] = _to_pht

# Worst-first ordering for the "needs attention" list.
_ALERT_RANK = {"expired": 0, "near_expiry": 1, "low_stock": 2}
_ALERT_LABELS = {
    "expired": "Expired",
    "near_expiry": "Near Expiry",
    "low_stock": "Low Stock",
}


@router.get("/api/search")
def api_global_search(request: Request, db: Session = Depends(get_db), q: Optional[str] = None):
    """Sidebar global search (#rkSearchInput in base.html). See
    app/services/global_search.py for why this route didn't exist before."""
    user = require_role(request, ["cdrrmo_staff", "admin"])
    if isinstance(user, RedirectResponse):
        return user
    if not q or len(q.strip()) < 2:
        return {"results": []}
    return global_search.search_staff(db, q)


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    user = require_role(request, ["cdrrmo_staff"])
    if isinstance(user, RedirectResponse):
        return user

    # ── Stockpile ─────────────────────────────────────────────────────
    # Active inventory only; archived items are not actionable.
    resources = db.query(Resource).filter(Resource.is_archived == False).all()
    summary = _resource_summary(resources)

    # Items needing action: worst tier first, then the thinnest stock.
    attention = sorted(
        (
            {
                "id": r.id,
                "name": r.name,
                "quantity": r.quantity or 0,
                "unit": r.unit or "",
                "restock_threshold": r.restock_threshold or 0,
                "expiry_date": r.expiry_date,
                "storage_location": r.storage_location or "—",
                "alert": _resource_alert(r),
                "alert_label": _ALERT_LABELS[_resource_alert(r)],
            }
            for r in resources
            if _resource_alert(r) != "ok"
        ),
        key=lambda x: (_ALERT_RANK[x["alert"]], x["quantity"]),
    )

    # Stock spread by category — chart data (active inventory).
    category_counts = {c.value: 0 for c in ResourceCategory}
    for r in resources:
        if r.category:
            category_counts[r.category.value] += 1

    # ── Vehicles & equipment ──────────────────────────────────────────
    fleet = equipment_status_breakdown(db)
    repair_attention = len(assets_needing_repair_attention(db))

    # ── Alerts + recent activity ──────────────────────────────────────
    active_alerts = (summary["low_stock"] + summary["near_expiry"]
                     + summary["expired"] + repair_attention)
    recent_logs = _logistics_rows(db, None, None, None, None, None)[:8]

    return templates.TemplateResponse(
        request=request,
        name="staff/dashboard.html",
        context={
            "user": user,
            "active_nav": "staff_dashboard",
            "summary": summary,
            "attention": attention[:8],
            "attention_total": len(attention),
            "category_counts": category_counts,
            "fleet": fleet,
            "fleet_labels": EQUIPMENT_STATUS_LABELS,
            "repair_attention": repair_attention,
            "active_alerts": active_alerts,
            "recent_logs": recent_logs,
        },
    )


# ══════════════════════════════════════════════════════════════════════
# CONTACT DIRECTORY — read-only, all barangays, grouped per barangay
# (shared across roles; see app/services/contact_directory.py).
# ══════════════════════════════════════════════════════════════════════

@router.get("/contacts", response_class=HTMLResponse)
def contact_directory(
    request: Request,
    db: Session = Depends(get_db),
    q: Optional[str] = None,
    brgy: Optional[str] = None,
    page: Optional[str] = None,
    per_page: Optional[str] = None,
):
    user = require_role(request, ["cdrrmo_staff"])
    if isinstance(user, RedirectResponse):
        return user

    context = build_directory_context(
        db, q=q, brgy=brgy, page=page, per_page=per_page,
        directory_url="/staff/contacts", active_nav="contacts",
    )
    context["user"] = user
    return templates.TemplateResponse(
        request=request, name="shared/contact_directory.html", context=context,
    )


# ══════════════════════════════════════════════════════════════════════
# LOGISTICS AUDIT TRAIL — read-only, scoped to the two modules this role
# operates: Resources (stockpile goods) and Vehicle & Equipment.
# Same AuditLog rows the admin trail reads; system-wide entries (auth,
# users, uploads, …) are never exposed here.
# ══════════════════════════════════════════════════════════════════════

# target_table values that belong to the logistics modules.
LOGISTICS_TABLES = ("resources", "equipment", "equipment_reports")
LOGISTICS_CATEGORIES = ["Resources", "Vehicle & Equipment"]


def _logistics_rows(db, q, action, category, date_from, date_to):
    """Filtered audit rows, newest-first, restricted to the logistics
    modules. The category filter is applied by the shared helper; the
    table scope is applied here so it can never be filtered away."""
    rows = _audit_filtered_rows(db, q, None, action, category, date_from, date_to)
    return [r for r in rows if (r.target_table or "") in LOGISTICS_TABLES]


@router.get("/audit", response_class=HTMLResponse)
def logistics_audit(
    request: Request,
    db: Session = Depends(get_db),
    q: Optional[str] = None,
    action: Optional[str] = None,
    category: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    page: Optional[str] = None,
    per_page: Optional[str] = None,
):
    user = require_role(request, ["cdrrmo_staff"])
    if isinstance(user, RedirectResponse):
        return user

    # An out-of-scope category would silently return nothing; drop it instead.
    if category not in LOGISTICS_CATEGORIES:
        category = None

    rows = _logistics_rows(db, q, action, category, date_from, date_to)
    page_obj = paginate(rows, parse_page(page), parse_per_page(per_page))

    # Group by PHT day, preserving newest-first order.
    grouped = []
    current_label, current_items = None, None
    for log in page_obj.items:
        item = {
            "id": log.id,
            "timestamp": log.timestamp,
            "occurred_at": log.occurred_at,
            "username": log.user.username if log.user else "—",
            "action": log.action,
            "category": _audit_category(log.action, log.target_table),
            "target_table": log.target_table,
            "target_id": log.target_id,
            "description": log.description,
            "reason": log.reason,
            "deployed_to": log.deployed_to,
        }
        label = _audit_day_label(log.timestamp)
        if label != current_label:
            current_label, current_items = label, []
            grouped.append((label, current_items))
        current_items.append(item)

    # Action choices are limited to actions that actually occur in scope.
    actions = sorted({r.action for r in _logistics_rows(
        db, None, None, None, None, None
    ) if r.action})

    base_query = build_base_query({
        "q": q or "", "action": action or "", "category": category or "",
        "date_from": date_from or "", "date_to": date_to or "",
    })

    return templates.TemplateResponse(
        request=request,
        name="staff/audit_list.html",
        context={
            "user": user,
            "active_nav": "staff_audit",
            "grouped": grouped,
            "total": page_obj.total,
            "page_obj": page_obj,
            "categories": LOGISTICS_CATEGORIES,
            "actions": actions,
            "base_query": base_query,
            "f_q": q or "",
            "f_action": action or "",
            "f_category": category or "",
            "f_date_from": date_from or "",
            "f_date_to": date_to or "",
        },
    )


LOGISTICS_AUDIT_COLUMNS = [
    "Logged (PHT)", "Occurred (PHT)", "User", "Module", "Action",
    "Record", "Description", "Reason", "Deployed To",
]


def _logistics_records(rows):
    """Flatten AuditLog rows into export rows (shared by CSV + PDF)."""
    records = []
    for log in rows:
        record = log.target_table or ""
        if log.target_id:
            record = f"{record} #{log.target_id}".strip()
        records.append({
            "time": _audit_export_time(log.timestamp),
            "occurred": _audit_export_time(log.occurred_at),
            "user": log.user.username if log.user else "—",
            "category": _audit_category(log.action, log.target_table),
            "action": log.action or "",
            "record": record or "—",
            "description": log.description or "",
            "reason": log.reason or "",
            "deployed_to": log.deployed_to or "",
        })
    return records


def _logistics_filter_summary(q, action, category, date_from, date_to) -> str:
    """Human-readable description of the active filters, for the PDF header."""
    parts = []
    if q:
        parts.append(f'search "{q.strip()}"')
    if category:
        parts.append(f"module {category}")
    if action:
        parts.append(f"action {action}")
    if date_from:
        parts.append(f"from {date_from}")
    if date_to:
        parts.append(f"to {date_to}")
    return ", ".join(parts) if parts else "none (all entries)"


def _logistics_pdf_bytes(records, filter_summary) -> bytes:
    """Same layout as the other audit exports, with the logistics columns
    (module, reason, deployed-to, occurrence time) this module needs."""
    from fpdf import FPDF
    from fpdf.enums import XPos, YPos

    pdf = FPDF(orientation="L", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=12)
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 9, _pdf_safe("RisKonek — Logistics Audit Trail"),
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

    pdf.set_font("Helvetica", "", 7)
    with pdf.table(col_widths=(14, 14, 11, 16, 14, 13, 34, 20, 17),
                   text_align="LEFT", line_height=4.4) as table:
        head = table.row()
        for h in LOGISTICS_AUDIT_COLUMNS:
            head.cell(h)
        for r in records:
            row = table.row()
            for key in ("time", "occurred", "user", "category", "action",
                        "record", "description", "reason", "deployed_to"):
                row.cell(_pdf_safe(r[key]))

    return bytes(pdf.output())


@router.get("/audit/export")
def logistics_audit_export(
    request: Request,
    db: Session = Depends(get_db),
    q: Optional[str] = None,
    action: Optional[str] = None,
    category: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    format: str = "csv",
):
    """Export the currently-filtered logistics trail as CSV or PDF — the full
    filtered set (all pages), newest-first, mirroring the list filters."""
    user = require_role(request, ["cdrrmo_staff"])
    if isinstance(user, RedirectResponse):
        return user

    if category not in LOGISTICS_CATEGORIES:
        category = None

    records = _logistics_records(
        _logistics_rows(db, q, action, category, date_from, date_to)
    )

    stamp = datetime.now(_PHT).strftime("%Y-%m-%d")
    fmt = (format or "csv").lower()

    if fmt == "pdf":
        summary = _logistics_filter_summary(q, action, category, date_from, date_to)
        return Response(
            content=_logistics_pdf_bytes(records, summary),
            media_type="application/pdf",
            headers={"Content-Disposition":
                     f'attachment; filename="logistics-audit-{stamp}.pdf"'},
        )

    # CSV (default). UTF-8 BOM so Excel renders accented names correctly.
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(LOGISTICS_AUDIT_COLUMNS)
    for r in records:
        writer.writerow([
            r["time"], r["occurred"], r["user"], r["category"], r["action"],
            r["record"], r["description"], r["reason"], r["deployed_to"],
        ])
    content = ("﻿" + buf.getvalue()).encode("utf-8")
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition":
                 f'attachment; filename="logistics-audit-{stamp}.csv"'},
    )


@router.get("/audit/{log_id}", response_class=HTMLResponse)
def logistics_audit_detail(log_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_role(request, ["cdrrmo_staff"])
    if isinstance(user, RedirectResponse):
        return user

    log = db.query(AuditLog).filter(AuditLog.id == log_id).first()
    # Out-of-scope entries are not viewable here even by direct URL.
    if not log or (log.target_table or "") not in LOGISTICS_TABLES:
        return RedirectResponse(url="/staff/audit", status_code=302)

    return templates.TemplateResponse(
        request=request,
        name="staff/audit_detail.html",
        context={
            "user": user,
            "active_nav": "staff_audit",
            "log": log,
            "actor": db.query(User).filter(User.id == log.user_id).first(),
            "category": _audit_category(log.action, log.target_table),
        },
    )
