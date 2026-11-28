"""Shared Contact Directory data builder.

Every role sees the same read-only, grouped-per-barangay directory of
officials and emergency responders. The data is the SAME Barangay record
each BDRRMO Chairperson maintains at /bdrrmo/contacts (captain_*,
chairperson_*, emergency_contacts) — a single source of truth, no separate
contacts table. This module turns those columns into flat contact rows
(one per person: barangay, name, role, number), and applies the search +
barangay filter + pagination the shared template renders.
"""
import re

from app.models import Barangay
from app.utils.pagination import (
    paginate, parse_per_page, parse_page, build_base_query, page_window,
)

# A run of digits (with the separators PH numbers commonly use) long enough
# to be a phone number — used to pull the number out of a free-text line.
_PHONE_RE = re.compile(r"\+?\d[\d\s\-().]{6,}\d")

# Separators between name / role segments on a responder line.
_SEGMENT_RE = re.compile(r"[—–|,/]")


def _split_responders(text):
    """Parse the free-text ``emergency_contacts`` block into structured rows.

    Convention (from the input placeholder): one responder per line, written
    as ``name — role — number``. The phone number is extracted with a regex
    and the remaining text split on common separators, so looser lines still
    degrade gracefully. Returns a list of ``{role, name, number}`` dicts.
    """
    entries = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue

        number = ""
        m = _PHONE_RE.search(line)
        if m:
            number = m.group(0).strip()
            line = line[:m.start()] + line[m.end():]

        parts = [p.strip(" —–|,/-") for p in _SEGMENT_RE.split(line)]
        parts = [p for p in parts if p]
        name = parts[0] if parts else ""
        role = " — ".join(parts[1:]) if len(parts) > 1 else "Emergency Responder"
        entries.append({"role": role, "name": name, "number": number})
    return entries


def _barangay_entries(b):
    """Build the ordered contact entries for one barangay: the captain and
    chairperson (only when something is on file) followed by each parsed
    emergency responder."""
    entries = []
    if b.captain_name or b.captain_contact:
        entries.append({
            "role": "Barangay Captain",
            "name": b.captain_name or "",
            "number": b.captain_contact or "",
        })
    if b.chairperson_name or b.chairperson_contact:
        entries.append({
            "role": "BDRRMO Chairperson",
            "name": b.chairperson_name or "",
            "number": b.chairperson_contact or "",
        })
    entries.extend(_split_responders(b.emergency_contacts))
    return entries


# The directory table shows 15 contact rows per page (roughly the height of the
# hotline panel beside it) unless ?per_page= says otherwise.
DIRECTORY_PER_PAGE = 15


def build_directory_context(db, *, q, brgy, page, per_page,
                            directory_url, active_nav):
    """Assemble the full template context for the shared directory page.

    The table is flat: one row per contact person (captain, chairperson,
    each parsed responder), ordered by barangay name and paginated by row.

    ``q``    — free-text search across each row's barangay name, role,
               name and number.
    ``brgy`` — exact barangay-name filter (dropdown).
    Returns everything ``shared/contact_directory.html`` needs except the
    ``user`` — each route adds that (and any role-specific extras) after its
    RBAC check.
    """
    q = (q or "").strip()
    brgy = (brgy or "").strip()

    all_barangays = db.query(Barangay).order_by(Barangay.name).all()
    barangay_names = [b.name for b in all_barangays]

    q_lower = q.lower()
    contact_rows = []
    for b in all_barangays:
        if brgy and b.name != brgy:
            continue
        for e in _barangay_entries(b):
            if q:
                haystack = f"{b.name} {e['role']} {e['name']} {e['number']}".lower()
                if q_lower not in haystack:
                    continue
            contact_rows.append({"barangay": b.name, **e})

    page_obj = paginate(
        contact_rows, parse_page(page), parse_per_page(per_page, DIRECTORY_PER_PAGE)
    )
    # Keep a non-default page size on every pager link.
    page_query = build_base_query({"q": q, "brgy": brgy})
    if page_obj.size_value != str(DIRECTORY_PER_PAGE):
        page_query = (page_query + "&" if page_query else "") + "per_page=" + page_obj.size_value

    return {
        "active_nav": active_nav,
        "directory_url": directory_url,
        "rows": page_obj.items,
        "page_obj": page_obj,
        "page_numbers": page_window(page_obj.page, page_obj.total_pages),
        "page_query": page_query,
        "barangay_names": barangay_names,
        "f_q": q,
        "f_brgy": brgy,
        # Only the BDRRMO route turns this on (Edit Contact modal).
        "can_edit_contacts": False,
    }
