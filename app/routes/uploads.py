"""Week 6 — Admin Data Upload & ETL Pipeline (Bronze → Silver → Gold).

Bronze: raw uploaded file saved unchanged under uploads/incident_reports/.
Silver: extracted text/rows + rule-based structured fields cached in
        UploadedReport.extracted_data JSON, shown on the review screen.
Gold:   only after Admin clicks Confirm Save — validated rows become
        Incident records and UploadedReport.status = confirmed.

Re-process, BDRRMO upload access, CFAU post-incident, OCR, and the
disaster simulator are intentionally out of scope for Week 6.
"""

from fastapi import APIRouter, Request, Depends, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from typing import Optional, List
from datetime import datetime, date, timezone, timedelta
import os
import re
import uuid
import json
from urllib.parse import quote_plus

from app.database import get_db
from app.auth import require_role
from app.models import (
    UploadedReport, UploadHistory, ReportStatus, FileType,
    LifecycleStatus, UploadEvent,
    Incident, IncidentReport, DisasterType, Severity, Barangay,
    log_action, add_upload_history,
)
from app.etl.extract_pdf import extract_pdf
from app.etl.extract_excel import extract_excel, extract_csv
from app.etl.structure import (
    structure_rows, structure_row, empty_field_row, structure_text,
)
from app.etl.ai_pipeline import summarize as ai_summarize, is_available as ai_available


router = APIRouter(prefix="/admin/uploads")
templates = Jinja2Templates(directory="app/templates")

_PHT = timezone(timedelta(hours=8))


def _to_pht(dt):
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_PHT).strftime("%b %d, %Y %I:%M %p")


templates.env.filters["pht"] = _to_pht


# ─────────────────────────────────────────────────────────────────────
# File-type handling (Bronze layer)
# ─────────────────────────────────────────────────────────────────────

ALLOWED_EXTS = {
    ".pdf":  FileType.pdf,
    ".xlsx": FileType.excel,
    ".xls":  FileType.excel,
    ".csv":  FileType.csv,
}

UPLOAD_SUBDIR = os.path.join("uploads", "incident_reports")


def _safe_filename(original: str) -> str:
    base = os.path.basename(original or "report")
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("_") or "report"
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    return f"{ts}_{uuid.uuid4().hex[:8]}_{base}"


def _ext_of(name: str) -> str:
    return os.path.splitext(name or "")[1].lower()


# ─────────────────────────────────────────────────────────────────────
# Upload validation (shared by admin + CFAU). Enforces — before the file
# is permanently kept — a configurable size cap, chunked streaming (no
# full-file in-memory read), and a magic-byte/text signature check so a
# renamed binary cannot pass the extension gate. Any partial file written
# during a failed validation is deleted.
# ─────────────────────────────────────────────────────────────────────

_UPLOAD_CHUNK = 64 * 1024  # 64 KB streaming chunks

# Leading-byte signatures keyed by extension. CSV has no binary signature
# and is validated as text instead (see _signature_ok).
_FILE_SIGNATURES = {
    ".pdf":  [b"%PDF"],
    ".xlsx": [b"PK\x03\x04"],                       # OOXML = ZIP container
    ".xls":  [b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1"],  # OLE2 compound document
}


class _UploadError(Exception):
    """Validation failure carrying a user-facing message."""


def _max_upload_bytes() -> int:
    """Max allowed upload size in bytes. Configurable via MAX_UPLOAD_MB
    (megabytes); defaults to 10 MB if unset, non-numeric, or non-positive."""
    try:
        mb = float(os.getenv("MAX_UPLOAD_MB", "10"))
    except (TypeError, ValueError):
        mb = 10.0
    if mb <= 0:
        mb = 10.0
    return int(mb * 1024 * 1024)


def _signature_ok(ext: str, head: bytes) -> bool:
    """True if the leading bytes match the claimed extension.

    Binary formats (PDF/XLSX/XLS) must start with their known signature.
    CSV (no signature) must look like text: no NUL byte (a reliable binary
    marker). Non-UTF-8 single-byte encodings (e.g. Windows-1252) are
    tolerated so legitimate names with accented characters aren't rejected.
    """
    sigs = _FILE_SIGNATURES.get(ext)
    if sigs is not None:
        return any(head.startswith(s) for s in sigs)
    # CSV / text path.
    if b"\x00" in head:
        return False
    return True


def _silent_remove(path: str) -> None:
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


async def save_validated_upload(file: UploadFile, ext: str, dest_path: str):
    """Stream `file` to `dest_path`, validating size and signature before it
    is permanently stored. Returns (ok: bool, error_message: Optional[str]).

    On any failure the partial file at `dest_path` is removed.
    """
    max_bytes = _max_upload_bytes()
    total = 0
    first = True
    try:
        with open(dest_path, "wb") as out:
            while True:
                chunk = await file.read(_UPLOAD_CHUNK)
                if not chunk:
                    break
                if first:
                    # Signature is checked on the first chunk, before the
                    # bulk of the file is written to disk.
                    if not _signature_ok(ext, chunk):
                        raise _UploadError(
                            "File content does not match its extension "
                            "(possible renamed or corrupt file)."
                        )
                    first = False
                total += len(chunk)
                if total > max_bytes:
                    raise _UploadError(
                        f"File too large. Maximum allowed size is "
                        f"{max_bytes // (1024 * 1024)} MB."
                    )
                out.write(chunk)
        if first:
            raise _UploadError("Uploaded file is empty.")
    except _UploadError as e:
        _silent_remove(dest_path)
        return False, str(e)
    except Exception as e:
        # Keep details server-side; show a generic message (OWASP info-leak).
        print(f"[uploads] Failed to save upload to '{dest_path}': {e}")
        _silent_remove(dest_path)
        return False, "Failed to save the uploaded file. Please try again."
    return True, None


# ─────────────────────────────────────────────────────────────────────
# Structured-field helpers (shared by save-draft and confirm)
# ─────────────────────────────────────────────────────────────────────

# Order matters only for display; keys mirror app.etl.structure field rows.
FIELD_KEYS = [
    "barangay", "disaster_type", "date_occurred",
    "affected_families", "affected_individuals", "casualties",
    "description", "resources_used",
]


def _read_field_rows(form) -> List[dict]:
    """Rebuild the structured-field rows from the parallel form arrays.

    Returns every row in the form (regardless of include flag), in the
    same dict shape used by app.etl.structure, so it can be cached back
    into extracted_data['fields'] for a draft.
    """
    cols = {k: form.getlist(k) for k in FIELD_KEYS}
    total = max((len(v) for v in cols.values()), default=0)
    rows: List[dict] = []
    for i in range(total):
        rows.append({k: (cols[k][i].strip() if i < len(cols[k]) else "") for k in FIELD_KEYS})
    return rows


def _ensure_original_snapshot(data: dict) -> dict:
    """Preserve a one-time copy of the originally extracted rows before the
    first manual edit, for traceability. Idempotent."""
    if "original_fields" not in data:
        data["original_fields"] = [dict(r) for r in (data.get("fields") or [])]
    return data


def _diff_field_rows(old_rows: List[dict], new_rows: List[dict]):
    """Yield (row_index, field, old, new) for each changed cell."""
    n = max(len(old_rows), len(new_rows))
    for i in range(n):
        old = old_rows[i] if i < len(old_rows) else {}
        new = new_rows[i] if i < len(new_rows) else {}
        for k in FIELD_KEYS:
            ov = str(old.get(k, "") or "")
            nv = str(new.get(k, "") or "")
            if ov != nv:
                yield (i, k, ov, nv)


# ─────────────────────────────────────────────────────────────────────
# Shared strict incident matcher (Week 8.1)
#
# Given the core fields derived by app.etl.structure, find an EXISTING
# Incident that exactly matches on barangay + disaster_type + date. Strict
# only: no fuzzy dates, no scoring, no AI. Returns the Incident or None.
#
# Reused by CFAU upload review (pre-select the linked incident) and kept
# generic so future BDRRMO uploads and an Admin duplicate-warning can call
# the same logic — one consistent matching rule across the ETL pipeline.
# ─────────────────────────────────────────────────────────────────────

def find_matching_incident(db: Session, barangay_name: str,
                           disaster_value: str, date_str: str):
    """Exact match on (barangay name, disaster_type, date_occurred).

    All three must be present and valid; any miss returns None. Barangay is
    matched case-insensitively by name. Date must be ISO 'YYYY-MM-DD'. If
    several incidents share the same key, the earliest-created is returned.
    """
    if not (barangay_name and disaster_value and date_str):
        return None
    if disaster_value not in {dt.value for dt in DisasterType}:
        return None
    try:
        d_obj = datetime.strptime(date_str.strip(), "%Y-%m-%d").date()
    except (ValueError, TypeError, AttributeError):
        return None

    brgy = (
        db.query(Barangay)
        .filter(Barangay.name.ilike(barangay_name.strip()))
        .first()
    )
    if not brgy:
        return None

    return (
        db.query(Incident)
        .filter(
            Incident.barangay_id == brgy.id,
            Incident.disaster_type == DisasterType(disaster_value),
            Incident.date_occurred == d_obj,
        )
        .order_by(Incident.id.asc())
        .first()
    )


def _core_int(v) -> int:
    """Coerce an extracted/posted numeric core field to a non-negative int."""
    try:
        return max(0, int(float(str(v).strip())))
    except (TypeError, ValueError):
        return 0


def resolve_or_create_incident(db: Session, user_id: int, core: dict, source: str):
    """Strict-match an existing Incident on the core triple; create one only if
    no match exists. Returns (incident, created: bool).

    Enrichment policy: a match is returned UNCHANGED — callers attach their
    role-specific data as child/provenance records and never mutate the
    canonical Incident. Creation requires the NOT NULL core fields (barangay,
    disaster_type, date_occurred); raises ValueError with a user-facing
    message when they are insufficient.

    `core` keys: barangay (name), disaster_type (value), date_occurred (ISO),
    severity (value, optional — defaults to moderate), affected_families,
    casualties, description. Reused by CFAU and BDRRMO ETL.
    """
    barangay_name = (core.get("barangay") or "").strip()
    disaster_value = (core.get("disaster_type") or "").strip()
    date_str = (core.get("date_occurred") or "").strip()
    severity_value = (core.get("severity") or "").strip()

    # Re-run the strict match at call time (submit), so an Incident created
    # between extraction and submit links instead of duplicating.
    match = find_matching_incident(db, barangay_name, disaster_value, date_str)
    if match:
        return match, False

    # No match — validate the NOT NULL fields, then create.
    brgy = (
        db.query(Barangay).filter(Barangay.name.ilike(barangay_name)).first()
        if barangay_name else None
    )
    if not brgy:
        raise ValueError("A valid barangay is required to create the incident.")
    if disaster_value not in {dt.value for dt in DisasterType}:
        raise ValueError("A valid disaster type is required to create the incident.")
    try:
        d_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        raise ValueError(
            "A valid incident date (YYYY-MM-DD) is required to create the incident."
        )

    # Severity is optional — an invalid/blank value falls back to moderate
    # (same default as the Incident model column).
    severity = (
        Severity(severity_value)
        if severity_value in {s.value for s in Severity}
        else Severity.moderate
    )

    incident = Incident(
        barangay_id=brgy.id,
        reported_by=user_id,
        disaster_type=DisasterType(disaster_value),
        date_occurred=d_obj,
        severity=severity,
        affected_families=_core_int(core.get("affected_families")),
        casualties=_core_int(core.get("casualties")),
        description=(core.get("description") or "").strip() or None,
        source=source,
    )
    db.add(incident)
    db.flush()  # assign incident.id within the caller's transaction
    return incident, True


# ─────────────────────────────────────────────────────────────────────
# DSS aggregation entry point (Week 8.1)
#
# Single-incident upload paths (CFAU post-incident, BDRRMO incident) set the
# UploadedReport.incident_id FK to the canonical Incident they resolved to, in
# ADDITION to the JSON provenance kept on extracted_data:
#     report_kind            — which upload type produced the contribution
#     linked_incident_id     — mirrors the FK (kept for backward compatibility)
#     incident_created       — whether THIS upload created that Incident
#     source                 — Incident.source provenance tag
# CFAU adds `produced_incident_report_id`; BDRRMO adds `contributed_core`
# (the uploader's submitted values).
#
# incident_contributions() is the single, source-agnostic place a future DSS
# / analytics module reads from. It joins on the indexed FK (no full-table
# JSON scan); the JSON payload is read only for per-kind detail. New upload
# kinds participate automatically by setting incident_id — and the canonical
# Incident is never mutated (contributions accumulate AROUND it).
# ─────────────────────────────────────────────────────────────────────

def incident_contributions(db: Session, incident_id: int) -> dict:
    """Aggregate every upload-based contribution linked to one Incident.

    Resolved via the indexed UploadedReport.incident_id FK. Returns a stable
    structure regardless of which report kinds contributed:

        {
          "incident_id": int,
          "total": int,                      # number of linked uploads
          "by_kind": {report_kind: count},   # e.g. {"bdrrmo_incident": 2,
                                             #        "post_incident": 1}
          "created_by_upload_id": int|None,  # the upload that created it
          "contributions": [                 # oldest → newest (by upload id)
            {
              "upload_id": int,
              "report_kind": str,
              "file_name": str,
              "uploaded_by": int|None,
              "uploaded_at": str|None,       # ISO-8601
              "incident_created": bool,
              "source": str|None,            # Incident.source provenance tag
              "produced_incident_report_id": int|None,  # CFAU only
              "contributed_core": dict|None, # BDRRMO uploader's values
            }, ...
          ],
        }
    """
    reports = (
        db.query(UploadedReport)
        .filter(UploadedReport.incident_id == incident_id)
        .order_by(UploadedReport.id.asc())
        .all()
    )
    contributions = []
    by_kind: dict = {}
    created_by_upload_id = None
    for r in reports:
        data = r.extracted_data or {}
        kind = data.get("report_kind") or "unknown"
        by_kind[kind] = by_kind.get(kind, 0) + 1
        if data.get("incident_created"):
            created_by_upload_id = r.id
        contributions.append({
            "upload_id": r.id,
            "report_kind": kind,
            "file_name": r.file_name,
            "uploaded_by": r.uploaded_by,
            "uploaded_at": r.uploaded_at.isoformat() if r.uploaded_at else None,
            "incident_created": bool(data.get("incident_created")),
            "source": data.get("source"),
            "produced_incident_report_id": data.get("produced_incident_report_id"),
            "contributed_core": data.get("contributed_core"),
        })
    return {
        "incident_id": incident_id,
        "total": len(contributions),
        "by_kind": by_kind,
        "created_by_upload_id": created_by_upload_id,
        "contributions": contributions,
    }


# ─────────────────────────────────────────────────────────────────────
# Canonical equipment-used contract (Week 8.1)
#
# BDRRMO contributions store equipment as a structured list:
#     [{"name": str, "quantity": int, "barangay_equipment_id": int|None}]
# CFAU stores it as free text on IncidentReport.equipment_used. Rather than
# migrate CFAU, both shapes are unified at READ time into one canonical item
# shape, so a DSS layer never has to know which module produced the data.
# New structured sources slot in for free; CFAU may later adopt the same
# structured shape with no change here.
# ─────────────────────────────────────────────────────────────────────

def normalize_equipment_used(value) -> List[dict]:
    """Normalize any equipment-used value into the canonical item shape.

    Accepts a structured list (BDRRMO), a free-text string (CFAU), or None.
    Returns a list of:
        {"name": str, "quantity": int|None,
         "barangay_equipment_id": int|None, "origin": "structured"|"free_text"}

    Read-only; never mutates the source. Empty/blank → [].
    """
    if not value:
        return []
    items: List[dict] = []

    if isinstance(value, list):
        for raw in value:
            if isinstance(raw, dict):
                name = str(raw.get("name") or "").strip()
                if not name:
                    continue
                q = raw.get("quantity")
                try:
                    quantity = int(q) if q is not None and str(q).strip() != "" else None
                except (ValueError, TypeError):
                    quantity = None
                beid = raw.get("barangay_equipment_id")
                if not isinstance(beid, int):
                    beid = None
                items.append({
                    "name": name, "quantity": quantity,
                    "barangay_equipment_id": beid, "origin": "structured",
                })
            else:
                # Tolerate a bare string element inside a list.
                name = str(raw).strip()
                if name:
                    items.append({
                        "name": name, "quantity": None,
                        "barangay_equipment_id": None, "origin": "structured",
                    })

    elif isinstance(value, str):
        # Split on common delimiters used in free-text equipment lists.
        for part in re.split(r"[,;/\n]| and ", value):
            name = part.strip(" \t\r\n-•").strip()
            if name:
                items.append({
                    "name": name, "quantity": None,
                    "barangay_equipment_id": None, "origin": "free_text",
                })

    return items


def incident_equipment(db: Session, incident_id: int) -> dict:
    """Unified equipment-used view for one Incident, across all contributors.

    Sources (disjoint by design — no double counting):
      - BDRRMO structured contributions:
        UploadedReport.contributed_core.equipment_used (via incident_contributions)
      - CFAU free text: IncidentReport.equipment_used for this incident

    Both pass through normalize_equipment_used() and are grouped by
    case-insensitive name. Quantities are summed where known; every appearance
    is counted as a mention.

    Returns:
        {
          "incident_id": int,
          "items": [
            {"name": str, "total_quantity": int|None, "mentions": int,
             "barangay_equipment_id": int|None, "origins": [str, ...]},
            ...
          ],
          "raw": [ <canonical item incl. origin>, ... ],   # flat, pre-grouping
        }
    """
    raw: List[dict] = []

    # BDRRMO structured contributions (CFAU uploads carry no contributed_core,
    # so their equipment is not double-counted here).
    contrib = incident_contributions(db, incident_id)
    for c in contrib["contributions"]:
        core = c.get("contributed_core") or {}
        raw.extend(normalize_equipment_used(core.get("equipment_used")))

    # CFAU free text lives on the IncidentReport child of the Incident.
    reports = (
        db.query(IncidentReport)
        .filter(IncidentReport.incident_id == incident_id)
        .all()
    )
    for r in reports:
        raw.extend(normalize_equipment_used(r.equipment_used))

    grouped: dict = {}
    for it in raw:
        key = it["name"].strip().lower()
        g = grouped.setdefault(key, {
            "name": it["name"],
            "total_quantity": None,
            "mentions": 0,
            "barangay_equipment_id": None,
            "origins": [],
        })
        g["mentions"] += 1
        if it.get("quantity") is not None:
            g["total_quantity"] = (g["total_quantity"] or 0) + it["quantity"]
        if it.get("barangay_equipment_id") and g["barangay_equipment_id"] is None:
            g["barangay_equipment_id"] = it["barangay_equipment_id"]
        if it["origin"] not in g["origins"]:
            g["origins"].append(it["origin"])

    items = sorted(grouped.values(), key=lambda x: x["name"].lower())
    return {"incident_id": incident_id, "items": items, "raw": raw}


# ─────────────────────────────────────────────────────────────────────
# LIST — upload history (TR-ADM-12..15)
# ─────────────────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def upload_list(request: Request, db: Session = Depends(get_db)):
    user = require_role(request, ["admin"])
    if isinstance(user, RedirectResponse):
        return user

    show_archived = request.query_params.get("show_archived") == "1"

    reports = (
        db.query(UploadedReport)
        .order_by(UploadedReport.uploaded_at.desc())
        .all()
    )

    # CFAU post-incident and BDRRMO incident uploads (Week 8.1) reuse the same
    # UploadedReport table but are tagged in extracted_data JSON and managed
    # under /cfau and /bdrrmo respectively. Keep this admin incident-upload
    # list showing only the admin's own medallion uploads.
    _MODULE_UPLOAD_KINDS = {"post_incident", "bdrrmo_incident"}
    reports = [
        r for r in reports
        if (r.extracted_data or {}).get("report_kind") not in _MODULE_UPLOAD_KINDS
    ]

    # Split by lifecycle. Drafts always get their own section; archived and
    # discarded uploads stay hidden until the admin opts to show them.
    drafts, active, hidden = [], [], []
    for r in reports:
        ls = r.lifecycle_status
        if ls == LifecycleStatus.draft:
            drafts.append(r)
        elif ls == LifecycleStatus.confirmed:
            active.append(r)
        else:  # archived or discarded
            hidden.append(r)

    return templates.TemplateResponse(
        request=request,
        name="admin/uploads_list.html",
        context={
            "user": user,
            "active_nav": "uploads",
            "drafts": drafts,
            "active_reports": active,
            "hidden_reports": hidden,
            "hidden_count": len(hidden),
            "show_archived": show_archived,
            "ai_available": ai_available(),
        },
    )


# ─────────────────────────────────────────────────────────────────────
# UPLOAD FORM
# ─────────────────────────────────────────────────────────────────────

@router.get("/new", response_class=HTMLResponse)
def upload_form(request: Request, db: Session = Depends(get_db)):
    user = require_role(request, ["admin"])
    if isinstance(user, RedirectResponse):
        return user

    return templates.TemplateResponse(
        request=request,
        name="admin/upload_form.html",
        context={
            "user": user,
            "active_nav": "uploads",
            "error": request.query_params.get("error"),
            "ai_available": ai_available(),
        },
    )


# ─────────────────────────────────────────────────────────────────────
# UPLOAD POST — Bronze + Silver in one request
# ─────────────────────────────────────────────────────────────────────

@router.post("/new")
async def upload_submit(
    request: Request,
    db: Session = Depends(get_db),
    file: UploadFile = File(...),
):
    user = require_role(request, ["admin"])
    if isinstance(user, RedirectResponse):
        return user

    original_name = file.filename or "report"
    ext = _ext_of(original_name)
    if ext not in ALLOWED_EXTS:
        return RedirectResponse(
            url="/admin/uploads/new?error=Unsupported+file+type.+Allowed:+PDF,+XLSX,+XLS,+CSV.",
            status_code=302,
        )

    os.makedirs(UPLOAD_SUBDIR, exist_ok=True)
    stored_name = _safe_filename(original_name)
    stored_path = os.path.join(UPLOAD_SUBDIR, stored_name)

    ok, err = await save_validated_upload(file, ext, stored_path)
    if not ok:
        return RedirectResponse(
            url="/admin/uploads/new?error=" + quote_plus(err),
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
        new_value=f"Uploaded '{original_name}' ({file_type_enum.value.upper()})",
    )
    db.commit()

    # ── Silver layer: extract + structure ────────────────────────────
    extracted = {"raw_text": "", "rows": [], "columns": [], "fields": [], "error": None}
    try:
        if file_type_enum == FileType.pdf:
            out = extract_pdf(stored_path)
            extracted["raw_text"] = out.get("text", "")
            # Best-effort: pre-fill only confidently detected fields from the
            # raw text; everything else keeps its blank-row default and stays
            # editable. Known barangay names make that detection reliable.
            known_barangays = [b.name for b in db.query(Barangay).all()]
            extracted["fields"] = [structure_text(extracted["raw_text"], known_barangays)]
        elif file_type_enum == FileType.excel:
            out = extract_excel(stored_path)
            extracted["columns"] = out["columns"]
            extracted["rows"] = out["rows"]
            extracted["fields"] = structure_rows(out["rows"]) or [empty_field_row()]
        elif file_type_enum == FileType.csv:
            out = extract_csv(stored_path)
            extracted["columns"] = out["columns"]
            extracted["rows"] = out["rows"]
            extracted["fields"] = structure_rows(out["rows"]) or [empty_field_row()]
        report.status = ReportStatus.reviewed
    except Exception as e:
        # Log details server-side; surface only a generic message to the UI.
        print(f"[uploads] Extraction failed for report '{original_name}': {e}")
        extracted["error"] = "Extraction failed. The file could not be read or was malformed."
        extracted["fields"] = [empty_field_row()]
        report.status = ReportStatus.failed

    # Optional AI summary — never blocks the flow
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

    report.extracted_data = extracted
    db.commit()

    if extracted.get("error"):
        extract_note = f"Extraction failed: {extracted['error']}"
    else:
        n_rows = len(extracted.get("fields") or [])
        extract_note = f"Extraction completed — {n_rows} structured row(s) ready for review."
    add_upload_history(
        db, report_id=report.id, user_id=user["id"],
        event_type=UploadEvent.extracted, new_value=extract_note,
    )
    db.commit()

    return RedirectResponse(url=f"/admin/uploads/{report.id}/review", status_code=302)


# ─────────────────────────────────────────────────────────────────────
# REVIEW SCREEN (Silver) — edit + confirm
# ─────────────────────────────────────────────────────────────────────

@router.get("/{report_id}/review", response_class=HTMLResponse)
def review(report_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_role(request, ["admin"])
    if isinstance(user, RedirectResponse):
        return user

    report = db.query(UploadedReport).filter(UploadedReport.id == report_id).first()
    if not report:
        return RedirectResponse(url="/admin/uploads", status_code=302)

    barangays = db.query(Barangay).order_by(Barangay.name).all()
    data = report.extracted_data or {}
    fields = data.get("fields") or [empty_field_row()]

    # Only drafts are editable / confirmable. Confirmed and archived
    # uploads are read-only; discarded uploads are terminal.
    is_draft = report.lifecycle_status == LifecycleStatus.draft
    editable = is_draft

    return templates.TemplateResponse(
        request=request,
        name="admin/upload_review.html",
        context={
            "user": user,
            "active_nav": "uploads",
            "report": report,
            "data": data,
            "fields": fields,
            "barangays": barangays,
            "disaster_types": [dt.value for dt in DisasterType],
            "editable": editable,
            "is_draft": is_draft,
            "lifecycle_status": report.lifecycle_status.value,
            "is_confirmed": report.lifecycle_status == LifecycleStatus.confirmed,
            "is_archived": report.lifecycle_status == LifecycleStatus.archived,
            "is_discarded": report.lifecycle_status == LifecycleStatus.discarded,
            "ai_available": ai_available(),
            "rows_preview": (data.get("rows") or [])[:10],
            "columns_preview": data.get("columns") or [],
            "raw_text_preview": (data.get("raw_text") or "")[:4000],
            "extraction_error": data.get("error"),
        },
    )


# ─────────────────────────────────────────────────────────────────────
# SAVE DRAFT — persist edits without confirming; append history rows
# ─────────────────────────────────────────────────────────────────────

@router.post("/{report_id}/save-draft")
async def save_draft(report_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_role(request, ["admin"])
    if isinstance(user, RedirectResponse):
        return user

    report = db.query(UploadedReport).filter(UploadedReport.id == report_id).first()
    if not report:
        return RedirectResponse(url="/admin/uploads", status_code=302)

    if report.lifecycle_status != LifecycleStatus.draft:
        return RedirectResponse(
            url=f"/admin/uploads/{report.id}/review?error=Only+draft+uploads+can+be+edited",
            status_code=302,
        )

    form = await request.form()
    reason = (form.get("edit_reason") or "").strip() or None

    data = dict(report.extracted_data or {})
    old_rows = data.get("fields") or []
    new_rows = _read_field_rows(form)

    # Preserve the original extracted rows once, before the first edit.
    _ensure_original_snapshot(data)

    change_count = 0
    for (row_i, key, ov, nv) in _diff_field_rows(old_rows, new_rows):
        label = key.replace("_", " ").title()
        add_upload_history(
            db, report_id=report.id, user_id=user["id"],
            event_type=UploadEvent.edited,
            field_changed=f"Row {row_i + 1} · {label}",
            old_value=ov, new_value=nv, reason=reason,
        )
        change_count += 1

    # Editable upload metadata: the display file name.
    new_name = (form.get("file_name") or "").strip()
    if new_name and new_name != report.file_name:
        add_upload_history(
            db, report_id=report.id, user_id=user["id"],
            event_type=UploadEvent.edited, field_changed="File Name",
            old_value=report.file_name, new_value=new_name, reason=reason,
        )
        report.file_name = new_name
        change_count += 1

    data["fields"] = new_rows
    data["validation_errors"] = None  # stale errors no longer apply
    report.extracted_data = data
    db.commit()

    if change_count:
        log_action(
            db, user_id=user["id"], action="edited",
            target_table="uploaded_reports", target_id=report.id,
            description=(
                f"Admin edited draft upload '{report.file_name}' "
                f"({change_count} field change(s))."
            ),
        )
        msg = f"Draft+saved+—+{change_count}+change(s)+recorded"
    else:
        msg = "Draft+saved+—+no+changes+detected"

    return RedirectResponse(
        url=f"/admin/uploads/{report.id}/review?success={msg}",
        status_code=302,
    )


# ─────────────────────────────────────────────────────────────────────
# CONFIRM SAVE (Gold) — write Incident rows + audit log
# ─────────────────────────────────────────────────────────────────────

def _parse_date(s: str) -> Optional[date]:
    """Accept ISO 'YYYY-MM-DD' only. The review screen submits dates via an
    <input type="date">, which always emits ISO, so we never have to guess
    between DD/MM and MM/DD here — ambiguous/blank values are rejected and the
    reviewer is sent back to disambiguate."""
    if not s:
        return None
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


@router.post("/{report_id}/confirm")
async def confirm_save(report_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_role(request, ["admin"])
    if isinstance(user, RedirectResponse):
        return user

    report = db.query(UploadedReport).filter(UploadedReport.id == report_id).first()
    if not report:
        return RedirectResponse(url="/admin/uploads", status_code=302)

    # Only drafts can be confirmed. Confirmed/archived/discarded are not.
    if report.lifecycle_status != LifecycleStatus.draft:
        return RedirectResponse(
            url=f"/admin/uploads/{report.id}/review",
            status_code=302,
        )

    form = await request.form()

    # Form fields arrive as parallel arrays — Form lists per row index.
    barangays = form.getlist("barangay")
    disaster_types = form.getlist("disaster_type")
    dates = form.getlist("date_occurred")
    families = form.getlist("affected_families")
    individuals = form.getlist("affected_individuals")
    casualties = form.getlist("casualties")
    descriptions = form.getlist("description")
    resources = form.getlist("resources_used")
    include_flags = form.getlist("include_row")  # only ticked rows are saved

    barangay_lookup = {b.name.lower(): b for b in db.query(Barangay).all()}
    valid_disaster_values = {dt.value for dt in DisasterType}

    saved_fields: List[dict] = []
    saved_incident_ids: List[int] = []
    errors: List[str] = []

    total = max(
        len(barangays), len(disaster_types), len(dates), len(families),
        len(individuals), len(casualties), len(descriptions), len(resources),
    )

    for i in range(total):
        idx = i + 1
        included = str(i) in include_flags  # checkbox value carries row index
        if not included:
            continue

        b_name = (barangays[i] if i < len(barangays) else "").strip()
        d_type = (disaster_types[i] if i < len(disaster_types) else "").strip()
        d_str  = (dates[i] if i < len(dates) else "").strip()
        fam    = (families[i] if i < len(families) else "0").strip() or "0"
        ind    = (individuals[i] if i < len(individuals) else "0").strip() or "0"
        cas    = (casualties[i] if i < len(casualties) else "0").strip() or "0"
        desc   = (descriptions[i] if i < len(descriptions) else "").strip()
        rsrc   = (resources[i] if i < len(resources) else "").strip()

        brgy = barangay_lookup.get(b_name.lower())
        if not brgy:
            errors.append(f"Row {idx}: unknown barangay '{b_name}'.")
            continue
        if d_type not in valid_disaster_values:
            errors.append(f"Row {idx}: invalid disaster type '{d_type}'.")
            continue
        d_obj = _parse_date(d_str)
        if not d_obj:
            errors.append(f"Row {idx}: invalid date '{d_str}'. Use YYYY-MM-DD.")
            continue
        try:
            fam_i = int(float(fam)); ind_i = int(float(ind)); cas_i = int(float(cas))
        except ValueError:
            errors.append(f"Row {idx}: numeric fields must be integers.")
            continue

        incident = Incident(
            barangay_id=brgy.id,
            reported_by=user["id"],
            disaster_type=DisasterType(d_type),
            date_occurred=d_obj,
            affected_families=fam_i,
            casualties=cas_i,
            description=desc,
            source=f"uploaded_report:{report.id}",
        )
        db.add(incident)
        db.flush()
        saved_incident_ids.append(incident.id)
        saved_fields.append({
            "barangay": brgy.name,
            "disaster_type": d_type,
            "date_occurred": d_obj.strftime("%Y-%m-%d"),
            "affected_families": fam_i,
            "affected_individuals": ind_i,
            "casualties": cas_i,
            "description": desc,
            "resources_used": rsrc,
            "incident_id": incident.id,
        })

    if errors and not saved_incident_ids:
        # Nothing valid — bounce back to review with error banner. Cache
        # errors in extracted_data so the review screen can show them.
        data = dict(report.extracted_data or {})
        data["validation_errors"] = errors
        report.extracted_data = data
        db.commit()
        return RedirectResponse(
            url=f"/admin/uploads/{report.id}/review?error=No+valid+rows+saved",
            status_code=302,
        )

    data = dict(report.extracted_data or {})
    _ensure_original_snapshot(data)  # keep the as-extracted rows for traceability
    data["fields"] = saved_fields if saved_fields else data.get("fields")
    data["linked_incident_ids"] = saved_incident_ids
    data["validation_errors"] = errors or None
    report.extracted_data = data
    report.status = ReportStatus.confirmed
    report.lifecycle_status = LifecycleStatus.confirmed
    if report.barangay_id is None and len(saved_incident_ids) == 1 and saved_fields:
        # Single-row PDF — attach barangay to the upload for the history filter.
        only = saved_fields[0]
        brgy = barangay_lookup.get(only["barangay"].lower())
        if brgy:
            report.barangay_id = brgy.id

    db.commit()

    add_upload_history(
        db, report_id=report.id, user_id=user["id"],
        event_type=UploadEvent.confirmed,
        new_value=f"Confirmed — saved {len(saved_incident_ids)} incident(s) to the database.",
    )
    db.commit()

    log_action(
        db,
        user_id=user["id"],
        action="confirmed",
        target_table="uploaded_reports",
        target_id=report.id,
        description=(
            f"Admin confirmed upload '{report.file_name}'; "
            f"saved {len(saved_incident_ids)} incident(s)."
        ),
    )

    return RedirectResponse(
        url=f"/admin/uploads/{report.id}/review?success=Saved+{len(saved_incident_ids)}+incident(s)",
        status_code=302,
    )


# ─────────────────────────────────────────────────────────────────────
# Optional: serve the raw Bronze file back (admin-only download)
# ─────────────────────────────────────────────────────────────────────

@router.get("/{report_id}/file")
def download_file(report_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_role(request, ["admin"])
    if isinstance(user, RedirectResponse):
        return user

    report = db.query(UploadedReport).filter(UploadedReport.id == report_id).first()
    if not report or not report.file_path or not os.path.exists(report.file_path):
        return RedirectResponse(url="/admin/uploads", status_code=302)

    return FileResponse(report.file_path, filename=report.file_name)


# ─────────────────────────────────────────────────────────────────────
# LIFECYCLE ACTIONS — archive / restore / discard
# ─────────────────────────────────────────────────────────────────────

@router.post("/{report_id}/archive")
def archive_upload(report_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_role(request, ["admin"])
    if isinstance(user, RedirectResponse):
        return user

    report = db.query(UploadedReport).filter(UploadedReport.id == report_id).first()
    if not report:
        return RedirectResponse(url="/admin/uploads", status_code=302)

    # Only confirmed uploads can be archived.
    if report.lifecycle_status != LifecycleStatus.confirmed:
        return RedirectResponse(
            url=f"/admin/uploads/{report.id}/review?error=Only+confirmed+uploads+can+be+archived",
            status_code=302,
        )

    report.lifecycle_status = LifecycleStatus.archived
    report.archived_at = datetime.utcnow()
    report.archived_by = user["id"]
    db.commit()

    add_upload_history(
        db, report_id=report.id, user_id=user["id"],
        event_type=UploadEvent.archived,
        new_value="Upload archived (kept searchable and auditable).",
    )
    db.commit()
    log_action(
        db, user_id=user["id"], action="archived",
        target_table="uploaded_reports", target_id=report.id,
        description=f"Admin archived upload '{report.file_name}'.",
    )
    return RedirectResponse(
        url=f"/admin/uploads/{report.id}/review?success=Upload+archived",
        status_code=302,
    )


@router.post("/{report_id}/restore")
def restore_upload(report_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_role(request, ["admin"])
    if isinstance(user, RedirectResponse):
        return user

    report = db.query(UploadedReport).filter(UploadedReport.id == report_id).first()
    if not report:
        return RedirectResponse(url="/admin/uploads", status_code=302)

    # Only archived uploads can be restored. Discarded uploads are terminal.
    if report.lifecycle_status != LifecycleStatus.archived:
        return RedirectResponse(
            url=f"/admin/uploads/{report.id}/review?error=Only+archived+uploads+can+be+restored",
            status_code=302,
        )

    report.lifecycle_status = LifecycleStatus.confirmed
    report.archived_at = None
    report.archived_by = None
    db.commit()

    add_upload_history(
        db, report_id=report.id, user_id=user["id"],
        event_type=UploadEvent.unarchived,
        new_value="Upload restored from archive to confirmed.",
    )
    db.commit()
    log_action(
        db, user_id=user["id"], action="restored",
        target_table="uploaded_reports", target_id=report.id,
        description=f"Admin restored upload '{report.file_name}' from archive.",
    )
    return RedirectResponse(
        url=f"/admin/uploads/{report.id}/review?success=Upload+restored",
        status_code=302,
    )


@router.post("/{report_id}/discard")
def discard_upload(report_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_role(request, ["admin"])
    if isinstance(user, RedirectResponse):
        return user

    report = db.query(UploadedReport).filter(UploadedReport.id == report_id).first()
    if not report:
        return RedirectResponse(url="/admin/uploads", status_code=302)

    # Only unconfirmed drafts can be discarded. Confirmed uploads cannot be
    # deleted; archived uploads must be restored first.
    if report.lifecycle_status != LifecycleStatus.draft:
        return RedirectResponse(
            url=f"/admin/uploads/{report.id}/review?error=Only+draft+uploads+can+be+discarded",
            status_code=302,
        )

    report.lifecycle_status = LifecycleStatus.discarded
    report.discarded_at = datetime.utcnow()
    report.discarded_by = user["id"]
    db.commit()

    add_upload_history(
        db, report_id=report.id, user_id=user["id"],
        event_type=UploadEvent.discarded,
        new_value="Unconfirmed draft discarded.",
    )
    db.commit()
    log_action(
        db, user_id=user["id"], action="discarded",
        target_table="uploaded_reports", target_id=report.id,
        description=f"Admin discarded draft upload '{report.file_name}'.",
    )
    return RedirectResponse(
        url="/admin/uploads?success=Draft+discarded",
        status_code=302,
    )


# ─────────────────────────────────────────────────────────────────────
# AUDIT TRAIL — per-upload history page
# ─────────────────────────────────────────────────────────────────────

@router.get("/{report_id}/history", response_class=HTMLResponse)
def upload_history(report_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_role(request, ["admin"])
    if isinstance(user, RedirectResponse):
        return user

    report = db.query(UploadedReport).filter(UploadedReport.id == report_id).first()
    if not report:
        return RedirectResponse(url="/admin/uploads", status_code=302)

    # Append-only trail — newest first (latest at the top, oldest at the bottom).
    events = (
        db.query(UploadHistory)
        .filter(UploadHistory.report_id == report.id)
        .order_by(UploadHistory.timestamp.desc(), UploadHistory.id.desc())
        .all()
    )

    # The preserved as-extracted snapshot, for distinguishing extracted vs
    # manually corrected values.
    data = report.extracted_data or {}
    original_fields = data.get("original_fields")

    # Round-trip traceability: if this is a CFAU post-incident upload that
    # was converted, surface a link back to the produced Post-Incident
    # Report (managed in the Post-Incident Reports module). Reuses the
    # existing Week 8.1 JSON linkage — no new column.
    produced_incident_report = None
    if data.get("report_kind") == "post_incident":
        produced_id = data.get("produced_incident_report_id")
        if produced_id:
            produced_incident_report = (
                db.query(IncidentReport)
                .filter(IncidentReport.id == produced_id)
                .first()
            )

    return templates.TemplateResponse(
        request=request,
        name="admin/upload_history.html",
        context={
            "user": user,
            "active_nav": "uploads",
            "report": report,
            "events": events,
            "original_fields": original_fields,
            "field_keys": FIELD_KEYS,
            "lifecycle_status": report.lifecycle_status.value,
            "produced_incident_report": produced_incident_report,
        },
    )
