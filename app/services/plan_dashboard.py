"""Counting logic for the Admin dashboard and the BDRRMO dashboard
(Barangay Profile), plus the per-activity rows of both activity-list pages.

Every function takes plain inputs (a db session, model objects, `today`) and
returns plain dicts/lists, so each card group / chart can be tested on its own.

Rules shared by both dashboards
  * A barangay counts once: only its latest non-archived plan is used.
  * Proof state of an activity (see proof_state):
        approved  - CDRRMO approved the proof          (status approved)
        returned  - CDRRMO returned it, re-upload due  (status rejected)
        awaiting  - submitted, waiting for review      (is_proof_submitted, pending)
        none      - nothing submitted yet
  * "Upcoming" = dated today .. today + 6 (7 days, today included).
  * Every pie has exactly two slices: green first, yellow second.
"""

import re
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import or_, func
from sqlalchemy.orm import joinedload

from app.models import Activity, AuditLog, Planning, PlanningStatus


UPCOMING_DAYS = 7
PIE_GREEN = "#167736"
PIE_YELLOW = "#f5b800"
_PHT = timezone(timedelta(hours=8))

PROOF_APPROVED, PROOF_RETURNED, PROOF_AWAITING, PROOF_NONE = "approved", "returned", "awaiting", "none"
PROOF_LABELS = {
    PROOF_APPROVED: "Approved",
    PROOF_RETURNED: "Returned",
    PROOF_AWAITING: "Awaiting review",
    PROOF_NONE: "No proof yet",
}


# ─────────────────────────────────────────────────────────────────────
# Small shared helpers
# ─────────────────────────────────────────────────────────────────────

def _value(enum_or_str):
    """'approved' from PlanningStatus.approved / ActivityStatus.approved / 'approved'."""
    return getattr(enum_or_str, "value", enum_or_str) or ""


def plan_status(plan) -> str:
    return _value(plan.status) or "pending"


def proof_state(activity) -> str:
    status = _value(activity.status) or "pending"
    if status == "approved":
        return PROOF_APPROVED
    if status == "rejected":
        return PROOF_RETURNED
    if activity.is_proof_submitted:
        return PROOF_AWAITING
    return PROOF_NONE


def sort_by_date(activities) -> list:
    """By date ascending, undated last, then title for a stable order."""
    return sorted(
        activities,
        key=lambda a: (a.start_date is None, a.start_date or date.max, (a.title or "").lower(), a.id or 0),
    )


def pht_date(dt):
    """Calendar date in Philippine time of a stored (naive = UTC) timestamp."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_PHT).date()


def long_date(d: date) -> str:
    """'Friday, November 27, 2026' (no zero padding, works on Windows too)."""
    return f"{d:%A}, {d:%B} {d.day}, {d.year}"


def pie(green_label, green_count, yellow_label, yellow_count) -> dict:
    """Two-slice pie + legend data. Percentages always add up to 100."""
    total = green_count + yellow_count
    green_pct = round(green_count * 100 / total) if total else 0
    yellow_pct = 100 - green_pct if total else 0
    return {
        "total": total,
        "slices": [
            {"label": green_label, "count": green_count, "pct": green_pct, "color": PIE_GREEN},
            {"label": yellow_label, "count": yellow_count, "pct": yellow_pct, "color": PIE_YELLOW},
        ],
    }


def upcoming(activities, today: date, days: int = UPCOMING_DAYS) -> list:
    """Activities dated today .. today + days - 1, soonest first, with 'in N days'."""
    last = today + timedelta(days=days - 1)
    rows = []
    for a in sort_by_date(a for a in activities if a.start_date and today <= a.start_date <= last):
        n = (a.start_date - today).days
        rows.append({
            "activity": a,
            "date": a.start_date,
            "in_days": "today" if n == 0 else ("tomorrow" if n == 1 else f"in {n} days"),
        })
    return rows


# ─────────────────────────────────────────────────────────────────────
# Plans: latest non-archived plan per barangay
# ─────────────────────────────────────────────────────────────────────

def _not_archived():
    return or_(Planning.is_archived == False, Planning.is_archived.is_(None))  # noqa: E712


def latest_plans_by_barangay(db) -> dict:
    """{barangay_id: Planning} — each barangay's newest non-archived plan."""
    plans = (
        db.query(Planning)
        .options(joinedload(Planning.barangay))
        .filter(Planning.barangay_id.isnot(None), _not_archived())
        .order_by(Planning.barangay_id, Planning.created_at.desc(), Planning.id.desc())
        .all()
    )
    latest = {}
    for p in plans:
        latest.setdefault(p.barangay_id, p)
    return latest


def latest_plan_for_barangay(db, barangay_id):
    return (
        db.query(Planning)
        .filter(Planning.barangay_id == barangay_id, _not_archived())
        .order_by(Planning.created_at.desc(), Planning.id.desc())
        .first()
    )


def activities_for_plans(db, plan_ids) -> list:
    if not plan_ids:
        return []
    return (
        db.query(Activity)
        .options(
            joinedload(Activity.planning).joinedload(Planning.barangay),
            joinedload(Activity.proof),
        )
        .filter(Activity.planning_id.in_(list(plan_ids)))
        .all()
    )


def rescheduled_counts(db, activity_ids) -> dict:
    """{activity_id: number of 'rescheduled' audit entries} (Task 1 audit log)."""
    ids = [i for i in activity_ids if i is not None]
    if not ids:
        return {}
    rows = (
        db.query(AuditLog.target_id, func.count(AuditLog.id))
        .filter(
            AuditLog.action == "rescheduled",
            AuditLog.target_table == "Activity",
            AuditLog.target_id.in_(ids),
        )
        .group_by(AuditLog.target_id)
        .all()
    )
    return {target_id: n for target_id, n in rows}


# ─────────────────────────────────────────────────────────────────────
# Activity counts (used by both dashboards)
# ─────────────────────────────────────────────────────────────────────

def activity_cards(activities) -> dict:
    states = [proof_state(a) for a in activities]
    scheduled = sum(1 for a in activities if a.start_date)
    return {
        "total": len(activities),
        "scheduled": scheduled,
        "unscheduled": len(activities) - scheduled,
        "awaiting": states.count(PROOF_AWAITING),
        "returned": states.count(PROOF_RETURNED),
        "approved": states.count(PROOF_APPROVED),
    }


def progress_segments(activities) -> dict:
    """Exclusive buckets for a stacked bar: proof decides first, then the date."""
    seg = {"approved": 0, "submitted": 0, "scheduled": 0, "unscheduled": 0}
    for a in activities:
        state = proof_state(a)
        if state == PROOF_APPROVED:
            seg["approved"] += 1
        elif state in (PROOF_AWAITING, PROOF_RETURNED):
            seg["submitted"] += 1
        elif a.start_date:
            seg["scheduled"] += 1
        else:
            seg["unscheduled"] += 1
    seg["total"] = len(activities)
    return seg


# ─────────────────────────────────────────────────────────────────────
# ADMIN dashboard
# ─────────────────────────────────────────────────────────────────────

def admin_plan_cards(latest: dict, total_barangays: int, today: date) -> dict:
    by_status = {"approved": [], "pending": [], "rejected": []}
    for p in latest.values():
        by_status.setdefault(plan_status(p), []).append(p)
    pending_dates = [pht_date(p.created_at) for p in by_status["pending"] if p.created_at]
    return {
        "total_barangays": total_barangays,
        "approved": len(by_status["approved"]),
        "pending": len(by_status["pending"]),
        "returned": len(by_status["rejected"]),
        "no_plan": max(total_barangays - len(latest), 0),
        "oldest_pending_days": max((today - d).days for d in pending_dates) if pending_dates else None,
    }


def approved_plans(latest: dict) -> list:
    return [p for p in latest.values() if plan_status(p) == "approved"]


def admin_plan_pie(latest: dict, limit: int = 5) -> dict:
    """Approved vs pending barangays (returned / no plan are left out)."""
    rows = [
        {"barangay": p.barangay.name if p.barangay else "—", "status": plan_status(p), "plan_id": p.id}
        for p in latest.values() if plan_status(p) in ("approved", "pending")
    ]
    rows.sort(key=lambda r: (r["status"] != "pending", r["barangay"].lower()))
    approved = sum(1 for r in rows if r["status"] == "approved")
    return {
        "pie": pie("Approved", approved, "Pending review", len(rows) - approved),
        "rows": rows[:limit],
        "total_rows": len(rows),
    }


def admin_proof_review(activities, limit: int = 5) -> dict:
    """Submitted proofs only: approved vs pending review, then per barangay."""
    per_plan = {}
    for a in activities:
        state = proof_state(a)
        if state not in (PROOF_APPROVED, PROOF_AWAITING):
            continue
        p = a.planning
        row = per_plan.setdefault(p.id, {
            "barangay": p.barangay.name if p.barangay else "—", "plan_id": p.id,
            "pending": 0, "approved": 0,
        })
        row["pending" if state == PROOF_AWAITING else "approved"] += 1
    rows = list(per_plan.values())
    for r in rows:
        r["submitted"] = r["pending"] + r["approved"]
    rows.sort(key=lambda r: (-r["pending"], r["barangay"].lower()))
    approved = sum(r["approved"] for r in rows)
    pending = sum(r["pending"] for r in rows)
    return {
        # the "Proofs awaiting review" card reads these same two numbers
        "approved": approved,
        "pending": pending,
        "pie": pie("Approved", approved, "Pending review", pending),
        "rows": rows[:limit],
        "total_rows": len(rows),
    }


def admin_progress_by_barangay(plans, activities) -> list:
    """One stacked bar per barangay with an approved plan, most activities first."""
    by_plan = {p.id: [] for p in plans}
    for a in activities:
        if a.planning_id in by_plan:
            by_plan[a.planning_id].append(a)
    rows = []
    for p in plans:
        acts = by_plan[p.id]
        not_done = [a for a in acts if proof_state(a) != PROOF_APPROVED]
        done = len(acts) - len(not_done)
        scheduled = sum(1 for a in not_done if a.start_date)
        rows.append({
            "barangay": p.barangay.name if p.barangay else "—",
            "plan_id": p.id,
            "done": done,                                   # proof approved
            "scheduled": scheduled,                         # has a date, not done yet
            "unscheduled": len(not_done) - scheduled,       # no date, not done yet
            "total": len(acts),
            "with_date_or_done": done + scheduled,          # the "N" in "N of M"
        })
    rows.sort(key=lambda r: (-r["total"], r["barangay"].lower()))
    return rows


def admin_needs_review(latest: dict, activities, limit: int = 8) -> dict:
    """Pending plans and proofs awaiting review, oldest first."""
    items = []
    for p in latest.values():
        if plan_status(p) == "pending":
            items.append({
                "kind": "plan", "barangay": p.barangay.name if p.barangay else "—",
                "item": p.file_name, "submitted": pht_date(p.created_at),
                "pill": "Plan · pending", "pill_class": "pending",
                # the plan list row carries the Change Status (approve / return) action
                "url": f"/admin/planning?status=pending&barangay_id={p.barangay_id}",
            })
    for a in activities:
        if proof_state(a) == PROOF_AWAITING:
            proof = a.proof[0] if a.proof else None
            reuploaded = bool(a.reason)  # a returned proof keeps its reason after re-upload
            items.append({
                "kind": "proof", "barangay": a.planning.barangay.name if a.planning.barangay else "—",
                "item": f"Proof · {a.title}", "submitted": pht_date(proof.created_at) if proof else None,
                "pill": "Proof · re-uploaded" if reuploaded else "Proof · pending", "pill_class": "awaiting",
                "url": f"/admin/planning/activities/{a.planning_id}",
            })
    items.sort(key=lambda i: (i["submitted"] is None, i["submitted"] or date.max))
    return {"rows": items[:limit], "total": len(items)}


_CHANGE_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2}|not yet scheduled) -> (\d{4}-\d{2}-\d{2}|not yet scheduled)"
)
_TITLE_RE = re.compile(r"Activity '(.*)' (?:re)?scheduled")


def _parse_date(s):
    try:
        return date.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def recent_date_changes(db, limit: int = 5) -> list:
    """Latest 'scheduled' / 'rescheduled' audit entries written by the
    schedule route: '... rescheduled: 2026-11-20 -> 2026-12-15. Reason: ...'."""
    logs = (
        db.query(AuditLog)
        .filter(AuditLog.action.in_(["scheduled", "rescheduled"]), AuditLog.target_table == "Activity")
        .order_by(AuditLog.timestamp.desc(), AuditLog.id.desc())
        .limit(limit)
        .all()
    )
    ids = {log.target_id for log in logs}
    acts = {
        a.id: a for a in (
            db.query(Activity)
            .options(joinedload(Activity.planning).joinedload(Planning.barangay))
            .filter(Activity.id.in_(ids)).all() if ids else []
        )
    }
    counts = rescheduled_counts(db, ids)
    rows = []
    for log in logs:
        a = acts.get(log.target_id)
        m = _CHANGE_RE.search(log.description or "")
        title_m = _TITLE_RE.search(log.description or "")
        rows.append({
            "title": a.title if a else (title_m.group(1) if title_m else "Activity"),
            "barangay": a.planning.barangay.name if a and a.planning and a.planning.barangay else "—",
            "rescheduled": counts.get(log.target_id, 0),
            "action": log.action,
            "old": _parse_date(m.group(1)) if m else None,
            "new": _parse_date(m.group(2)) if m else None,
            "reason": log.reason,
        })
    return rows


# ─────────────────────────────────────────────────────────────────────
# BDRRMO dashboard (one barangay, its latest plan)
# ─────────────────────────────────────────────────────────────────────

def bdrrmo_timing_pie(activities, today: date) -> dict:
    """Done = date has passed, Upcoming = today or later; undated not counted."""
    dated = [a for a in activities if a.start_date]
    done = sum(1 for a in dated if a.start_date < today)
    future = sort_by_date(a for a in dated if a.start_date >= today)
    return {
        "pie": pie("Done", done, "Upcoming", len(dated) - done),
        "done": done,
        "dated": len(dated),
        "unscheduled": len(activities) - len(dated),
        "next": future[0] if future else None,
    }


def bdrrmo_proof_pie(activities) -> dict:
    """Submitted (is_proof_submitted) vs none yet, plus the review outcome counts."""
    cards = activity_cards(activities)
    submitted = sum(1 for a in activities if a.is_proof_submitted)
    return {
        "pie": pie("Submitted", submitted, "None yet", len(activities) - submitted),
        "submitted": submitted,  # also the "Accomplishment Report" card number
        "approved": cards["approved"],
        "awaiting": cards["awaiting"],
        "returned": cards["returned"],
    }


def bdrrmo_activity_rows(activities, plan, rescheduled: dict) -> list:
    """Rows for the BDRRMO dashboard table and activities page, with the
    actions the routes would actually accept."""
    status = plan_status(plan)
    rows = []
    for a in sort_by_date(activities):
        state = proof_state(a)
        rows.append({
            "activity": a,
            "state": state,
            "label": PROOF_LABELS[state],
            "rescheduled": rescheduled.get(a.id, 0),
            # schedule route refuses returned plans and approved proofs
            "can_set_date": status != "rejected" and state != PROOF_APPROVED,
            # proof routes need an approved plan
            "can_upload": status == "approved" and state == PROOF_NONE,
            "can_reupload": status == "approved" and state == PROOF_RETURNED,
            "plan_not_approved": status != "approved" and state == PROOF_NONE,
        })
    return rows


def bdrrmo_attention(activities, plan) -> dict:
    status = plan_status(plan)
    cards = activity_cards(activities)
    returned = [a for a in sort_by_date(activities) if proof_state(a) == PROOF_RETURNED] \
        if status == "approved" else []
    unscheduled = cards["unscheduled"] if status != "rejected" else 0
    return {"unscheduled": unscheduled, "returned": returned, "any": bool(unscheduled or returned)}


def admin_activity_rows(activities) -> list:
    rows = []
    for a in sort_by_date(activities):
        state = proof_state(a)
        rows.append({
            "activity": a,
            "state": state,
            "label": PROOF_LABELS[state],
            "has_proof": bool(a.proof),
            "can_review": state == PROOF_AWAITING,
        })
    return rows
