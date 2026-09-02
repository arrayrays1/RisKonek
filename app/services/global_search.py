"""Global search bar (sidebar) — one function per role, each returning the
records that role is actually allowed to see, in the same {url, icon, type,
label, sub} shape the sidebar JS (base.html, #rkSearchInput) already expects.

This did not exist before: the sidebar always called `GET {prefix}/api/search`
but no such route was registered anywhere, so every keystroke 404'd and the
dropdown just closed — the search box looked present but did nothing. This
module is the fix; app/routes/{admin,staff,cfau,bdrrmo}.py each add a thin
`@router.get("/api/search")` that calls the matching function below.

Kept deliberately simple: substring `ILIKE` per field, capped per category,
no ranking/relevance model. That matches what a Ctrl+F-style "find the thing
I'm thinking of" sidebar search needs; a real full-text/relevance search
would be a separate, larger feature.
"""
from typing import Optional
from urllib.parse import quote_plus

from sqlalchemy.orm import Session

from app.models import (
    Barangay, Resource, Equipment, Facility, EquipmentReport,
    BarangayEquipment,
)

# Cap results so the dropdown stays a quick scan, not a second list page.
_PER_CATEGORY_LIMIT = 4
_TOTAL_LIMIT = 8


def _like(q: str) -> str:
    return f"%{q.strip()}%"


def _clip(results: list[dict]) -> dict:
    return {"results": results[:_TOTAL_LIMIT]}


def search_admin(db: Session, q: str) -> dict:
    """Barangay + Resource + Equipment + Facility — matches the admin
    sidebar's "Search barangays, resources…" placeholder and the admin's
    full visibility across every module."""
    like = _like(q)
    results: list[dict] = []

    for b in (
        db.query(Barangay).filter(Barangay.name.ilike(like))
        .order_by(Barangay.name).limit(_PER_CATEGORY_LIMIT)
    ):
        results.append({
            "url": f"/admin/barangays/{b.id}",
            "icon": "bi-geo-alt-fill", "type": "Barangay",
            "label": b.name,
            "sub": f"{b.risk_level.value.title()} risk" if b.risk_level else None,
        })

    for r in (
        db.query(Resource).filter(
            Resource.is_archived == False,
            (Resource.name.ilike(like)) | (Resource.storage_location.ilike(like)),
        ).order_by(Resource.name).limit(_PER_CATEGORY_LIMIT)
    ):
        results.append({
            "url": f"/admin/resources?q={quote_plus(r.name)}",
            "icon": "bi-box-seam-fill", "type": "Resource",
            "label": r.name,
            "sub": f"{r.quantity or 0} {r.unit or ''} · {r.storage_location or 'no location set'}".strip(),
        })

    for e in (
        db.query(Equipment).filter(
            Equipment.is_archived == False,
            (Equipment.name.ilike(like)) | (Equipment.plate_or_serial.ilike(like)),
        ).order_by(Equipment.name).limit(_PER_CATEGORY_LIMIT)
    ):
        results.append({
            "url": f"/admin/equipment?q={quote_plus(e.name)}",
            "icon": "bi-wrench-adjustable", "type": "Equipment",
            "label": e.name,
            "sub": e.plate_or_serial or (e.status.value.replace("_", " ").title() if e.status else None),
        })

    for f in (
        db.query(Facility).filter(
            Facility.is_archived == False,
            (Facility.name.ilike(like)) | (Facility.address.ilike(like)),
        ).order_by(Facility.name).limit(_PER_CATEGORY_LIMIT)
    ):
        results.append({
            # /admin/map?edit={id} auto-opens that facility's detail modal —
            # there's no separate read-only facility page to link to.
            "url": f"/admin/map?edit={f.id}",
            "icon": "bi-building", "type": "Facility",
            "label": f.name, "sub": f.address,
        })

    return _clip(results)


def search_staff(db: Session, q: str) -> dict:
    """Resource + Equipment only — CDRRMO Logistics Staff use the shared
    /admin/resources and /admin/equipment pages (role-gated there, not
    URL-scoped), matching the "Search resources, equipment…" placeholder."""
    like = _like(q)
    results: list[dict] = []

    for r in (
        db.query(Resource).filter(
            Resource.is_archived == False,
            (Resource.name.ilike(like)) | (Resource.storage_location.ilike(like)),
        ).order_by(Resource.name).limit(_PER_CATEGORY_LIMIT)
    ):
        results.append({
            "url": f"/admin/resources?q={quote_plus(r.name)}",
            "icon": "bi-box-seam-fill", "type": "Resource",
            "label": r.name,
            "sub": f"{r.quantity or 0} {r.unit or ''} · {r.storage_location or 'no location set'}".strip(),
        })

    for e in (
        db.query(Equipment).filter(
            Equipment.is_archived == False,
            (Equipment.name.ilike(like)) | (Equipment.plate_or_serial.ilike(like)),
        ).order_by(Equipment.name).limit(_PER_CATEGORY_LIMIT)
    ):
        results.append({
            "url": f"/admin/equipment?q={quote_plus(e.name)}",
            "icon": "bi-wrench-adjustable", "type": "Equipment",
            "label": e.name,
            "sub": e.plate_or_serial or (e.status.value.replace("_", " ").title() if e.status else None),
        })

    return _clip(results)


def search_cfau(db: Session, q: str, user_id: int, is_admin: bool) -> dict:
    """Equipment + Serviceability Reports — matches "Search equipment,
    reports…". Reports are scoped to the CFAU officer's own submissions
    unless the caller is admin, mirroring /cfau/serviceability's own rule.

    Post-incident reports (IncidentReport) are deliberately out of scope for
    now: unlike EquipmentReport, that model has no title/free-text field to
    search against — only structured fields tied to a parent Incident — so a
    substring match there would need its own design pass rather than
    reusing this pattern."""
    like = _like(q)
    results: list[dict] = []

    for e in (
        db.query(Equipment).filter(
            Equipment.is_archived == False,
            (Equipment.name.ilike(like)) | (Equipment.plate_or_serial.ilike(like)),
        ).order_by(Equipment.name).limit(_PER_CATEGORY_LIMIT)
    ):
        results.append({
            "url": f"/admin/equipment?q={quote_plus(e.name)}",
            "icon": "bi-wrench-adjustable", "type": "Equipment",
            "label": e.name,
            "sub": e.plate_or_serial or (e.status.value.replace("_", " ").title() if e.status else None),
        })

    report_q = db.query(EquipmentReport).filter(EquipmentReport.title.ilike(like))
    if not is_admin:
        report_q = report_q.filter(EquipmentReport.reported_by == user_id)
    for r in report_q.order_by(EquipmentReport.reported_at.desc()).limit(_PER_CATEGORY_LIMIT):
        results.append({
            "url": f"/cfau/serviceability/{r.id}",
            "icon": "bi-clipboard2-check", "type": "Report",
            "label": r.title or f"Serviceability report #{r.id}",
            "sub": r.equipment.name if r.equipment else None,
        })

    return _clip(results)


def search_bdrrmo(db: Session, q: str, barangay_id: Optional[int]) -> dict:
    """Facility + barangay-owned Equipment, scoped to the caller's own
    barangay only (TR-BDR-10) — matches "Search facilities, equipment…".
    Neither list page currently supports a ?q= filter, so results link to
    the (short, single-barangay) list itself rather than a deep link."""
    if not barangay_id:
        return _clip([])
    like = _like(q)
    results: list[dict] = []

    for f in (
        db.query(Facility).filter(
            Facility.barangay_id == barangay_id,
            Facility.is_archived == False,
            (Facility.name.ilike(like)) | (Facility.address.ilike(like)),
        ).order_by(Facility.name).limit(_PER_CATEGORY_LIMIT)
    ):
        results.append({
            "url": "/bdrrmo/facilities",
            "icon": "bi-building", "type": "Facility",
            "label": f.name, "sub": f.address,
        })

    for e in (
        db.query(BarangayEquipment).filter(
            BarangayEquipment.barangay_id == barangay_id,
            BarangayEquipment.is_archived == False,
            (BarangayEquipment.name.ilike(like)) | (BarangayEquipment.plate_or_serial.ilike(like)),
        ).order_by(BarangayEquipment.name).limit(_PER_CATEGORY_LIMIT)
    ):
        results.append({
            "url": "/bdrrmo/equipment",
            "icon": "bi-wrench-adjustable", "type": "Equipment",
            "label": e.name,
            "sub": f"Qty {e.quantity}" if e.quantity else (e.plate_or_serial or None),
        })

    return _clip(results)
