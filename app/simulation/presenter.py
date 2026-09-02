"""
Resource Simulator — PRESENTATION layer for the results page.

Pure, DB-free view-model builder. It takes an engine result dict (see
app/simulation/engine.run_simulation) and returns plain dicts/lists shaped for
the template, so the business rules stay out of Jinja.

It calculates NOTHING new about readiness: coverage, gaps and statuses all come
straight from the engine result, and the readiness bands are re-read through
engine.readiness_status so a threshold change is picked up here automatically.
Everything added is presentational — friendly labels, whole-percent coverage,
ranking, and plain-language sentences built only from values already present.
"""

from app.simulation.engine import HAZARD_HISTORY_THRESHOLD, readiness_status

# Same display order the table, the PDF and the compare view already use.
DISPLAY_ORDER = ["water", "food", "blankets", "evac_capacity", "medicine", "vehicles"]

# Plain-language names shown to non-technical planners.
CLASS_LABEL = {
    "water": "Water supply",
    "food": "Food packs",
    "blankets": "Blankets and essential household kits",
    "evac_capacity": "Evacuation spaces",
    "medicine": "Medicine kits",
    "vehicles": "Response vehicles",
}

# The original technical terms, kept as supporting text (never replaced in the DB).
TECHNICAL_LABEL = {
    "water": "Water",
    "food": "Food Packs",
    "blankets": "Blankets / NFI Kits",
    "evac_capacity": "Evacuation Capacity",
    "medicine": "Medicine Kits",
    "vehicles": "Response Vehicles",
}

# Short forms used inside generated sentences.
SHORT_LABEL = {
    "water": "water",
    "food": "food packs",
    "blankets": "blankets and household kits",
    "evac_capacity": "evacuation spaces",
    "medicine": "medicine kits",
    "vehicles": "response vehicles",
}

# Engine status -> UI vocabulary. Labels match the PDF export and the compare
# view so one run reads the same everywhere. "Surplus" is a presentation-only
# refinement of Adequate (available exceeds the requirement); it never changes
# the engine status or any threshold.
STATUS_UI = {
    "Adequate": {"label": "Sufficient", "pill": "status-operational",
                 "bar": "bg-success", "accent": "rk-accent-ok"},
    "Partial":  {"label": "At Risk",    "pill": "status-maintenance",
                 "bar": "bg-warning",   "accent": "rk-accent-warn"},
    "Critical": {"label": "Critical",   "pill": "status-unavailable",
                 "bar": "bg-danger",    "accent": "rk-accent-crit"},
}

SURPLUS_UI = {"label": "Surplus", "pill": "status-operational",
              "bar": "bg-success", "accent": "rk-accent-ok"}

# Overall readiness band wording. Phrased as PLANNING PRIORITY rather than
# "risk": this is a preparedness gap for planners to work through, not a hazard
# forecast, and "High Risk" was being read as a warning about the disaster
# itself. The underlying engine status is unchanged.
BAND_UI = {
    "Adequate": {"label": "Low Priority",      "text": "rk-band-ok",
                 "accent": "rk-accent-ok",   "hero": "rk-hero-ok",
                 "icon": "bi-check-circle-fill"},
    "Partial":  {"label": "Moderate Priority", "text": "rk-band-warn",
                 "accent": "rk-accent-warn", "hero": "rk-hero-warn",
                 "icon": "bi-exclamation-circle-fill"},
    "Critical": {"label": "High Priority",     "text": "rk-band-crit",
                 "accent": "rk-accent-crit", "hero": "rk-hero-crit",
                 "icon": "bi-exclamation-triangle-fill"},
}

# The three readiness dimensions, mirroring the groups the page already showed.
DIMENSIONS = [
    {"key": "evacuation", "label": "Evacuation", "icon": "bi-house-heart-fill",
     "short": "evacuation spaces", "classes": ["evac_capacity"]},
    {"key": "supplies", "label": "Supplies", "icon": "bi-box-seam-fill",
     "short": "supplies", "classes": ["water", "food", "blankets", "medicine"]},
    {"key": "response", "label": "Response Capacity", "icon": "bi-truck",
     "short": "response vehicles", "classes": ["vehicles"]},
]

# How many concerns are shown before the "View all resource gaps" disclosure.
TOP_CONCERNS = 3

# Ranking: Critical before At Risk, then lowest coverage first. Absolute deficit
# is only a final tie-breaker (never a primary key — the classes use different
# units, so a litre gap is not comparable to a vehicle gap).
_STATUS_RANK = {"Critical": 0, "Partial": 1, "Adequate": 2}


def _n(value):
    """Coerce to a non-negative int; None/garbage -> 0."""
    try:
        out = int(value)
    except (TypeError, ValueError):
        return 0
    return out


def _join(items):
    """Join names as 'a', 'a and b', 'a, b and c'."""
    items = [i for i in items if i]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


def coverage_percent(available, need):
    """Whole-percent coverage, clamped to 0–100.

    A zero requirement is fully covered by definition (mirrors the engine, which
    defines coverage as 1.0 when need == 0). Negative inputs clamp to 0.
    """
    need = _n(need)
    available = _n(available)
    if need <= 0:
        return 100
    if available <= 0:
        return 0
    pct = int(round(available / need * 100))
    return max(0, min(pct, 100))


def _row(cls, gap, status):
    """One resource row: raw values from the engine + presentation extras."""
    need = _n(gap.get("need"))
    available = _n(gap.get("available"))
    shortfall = _n(gap.get("gap"))
    surplus = max(0, available - need)
    pct = coverage_percent(available, need)
    ratio = (available / need) if need > 0 else 1.0

    is_surplus = status == "Adequate" and surplus > 0
    ui = SURPLUS_UI if is_surplus else STATUS_UI.get(status, STATUS_UI["Critical"])

    # Real (uncapped) percent for surplus rows, so a 233%-covered resource can
    # say "233%" instead of just filling the same bar as a 100%-covered one.
    pct_uncapped = int(round(available / need * 100)) if need > 0 else 100
    # Bar-fill floor: a resource with SOME stock (however little) should never
    # render an empty-looking bar identical to zero stock. 1% is invisible on
    # a 0-100 scale in practice, so floor any nonzero availability at 2%.
    bar_pct = pct if pct > 0 else (2 if available > 0 else 0)

    unit = gap.get("unit", "") or "units"
    if shortfall > 0:
        note = (
            f"An additional {shortfall:,} {unit} may be needed to meet the "
            "estimated requirement for this planning period."
        )
    elif surplus > 0:
        note = f"Recorded availability exceeds the estimated requirement by {surplus:,} {unit}."
    else:
        note = "Recorded availability matches the estimated requirement."

    return {
        "key": cls,
        "label": CLASS_LABEL.get(cls, cls.replace("_", " ").title()),
        "technical_label": TECHNICAL_LABEL.get(cls, cls),
        "short_label": SHORT_LABEL.get(cls, cls.replace("_", " ")),
        "unit": gap.get("unit", ""),
        "basis": gap.get("basis", "assumption"),
        "note": note,
        "need": need,
        "available": available,
        "gap": shortfall,
        "surplus": surplus,
        "pct": pct,
        "pct_uncapped": pct_uncapped,
        "bar_pct": bar_pct,
        # Honest label for a non-zero but sub-1% coverage, which rounds to 0%.
        "pct_label": ("less than 1%" if (0 < ratio < 0.005) else f"{pct}%"),
        "status": status,
        "status_label": ui["label"],
        "pill": ui["pill"],
        "bar": ui["bar"],
        "accent": ui["accent"],
        "is_surplus": is_surplus,
    }


def _dimension(dim, rows_by_key):
    """Readiness for one dimension: the worst coverage among its classes drives
    the band, exactly as the previous page did."""
    members = [rows_by_key[c] for c in dim["classes"] if c in rows_by_key]
    if not members:
        return None

    def _ratio(r):
        return (r["available"] / r["need"]) if r["need"] > 0 else 1.0

    worst = min(members, key=_ratio)
    status = readiness_status(_ratio(worst))
    ui = STATUS_UI.get(status, STATUS_UI["Critical"])

    short = [r for r in members if r["gap"] > 0]
    if dim["key"] == "evacuation":
        row = rows_by_key.get("evac_capacity")
        if row and row["need"] > 0:
            sentence = (
                f"{row['available']:,} evacuation spaces are recorded as available "
                f"for an estimated {row['need']:,} affected residents."
            )
        else:
            sentence = "No evacuation requirement was estimated for this scenario."
    elif dim["key"] == "response":
        row = rows_by_key.get("vehicles")
        if row:
            sentence = (
                f"{row['available']:,} response vehicles are recorded as available "
                f"against an estimated requirement of {row['need']:,}."
            )
        else:
            sentence = "No response-vehicle figures are available for this scenario."
    else:
        if short:
            names = _join([r["short_label"] for r in short])
            verb = "is" if len(short) == 1 else "are"
            sentence = (
                f"{names.capitalize()} {verb} below the estimated requirement "
                "for this planning period."
            )
        else:
            sentence = "Recorded stock covers the estimated supply requirements."

    return {
        "key": dim["key"],
        "label": dim["label"],
        "icon": dim["icon"],
        "short": dim["short"],
        "status": status,
        "status_label": ui["label"],
        "pill": ui["pill"],
        "bar": ui["bar"],
        "accent": ui["accent"],
        "pct": coverage_percent(worst["available"], worst["need"]),
        "sentence": sentence,
    }


def _overall_sentence(dimensions, overall):
    below = [d["short"] for d in dimensions if d["status"] != "Adequate"]
    if not below:
        return ("Recorded resources meet the estimated requirements for this "
                "planning scenario.")
    return (
        f"Recorded {_join(below)} are below the estimated requirements for this "
        "planning scenario."
    )


def _consequences(result, rows, rows_by_key):
    """Grouped, plain-language 'what may happen if the gaps remain' messages.

    Built only from values already in the result, and phrased as planning
    possibilities ('may', 'estimated') — never as predictions.
    """
    out = []

    evac = rows_by_key.get("evac_capacity")
    if evac and evac["gap"] > 0:
        out.append({
            "label": "Evacuation",
            "text": (
                f"Recorded capacity may leave approximately {evac['gap']:,} "
                "affected residents without an assigned evacuation space."
            ),
        })

    supply_short = [
        r for r in rows
        if r["key"] in ("water", "food", "blankets", "medicine") and r["gap"] > 0
    ]
    if supply_short:
        names = _join([r["short_label"] for r in supply_short])
        verb = "is" if len(supply_short) == 1 else "are"
        out.append({
            "label": "Essential supplies",
            "text": (
                f"{names.capitalize()} {verb} below the estimated requirement and "
                "may need to be sourced within the planning period."
            ),
        })

    veh = rows_by_key.get("vehicles")
    if veh and veh["gap"] > 0:
        out.append({
            "label": "Response capacity",
            "text": (
                f"{veh['available']:,} response vehicles are currently recorded as "
                f"available against an estimated requirement of {veh['need']:,}."
            ),
        })

    fleet = result.get("fleet") or {}
    serviceable = _n(fleet.get("serviceable"))
    under_repair = _n(fleet.get("under_repair"))
    if (serviceable + under_repair) > 0 and under_repair >= serviceable:
        out.append({
            "label": "Fleet condition",
            "text": (
                f"{under_repair:,} vehicles are recorded as out of service against "
                f"{serviceable:,} serviceable, which may further reduce available "
                "response capacity."
            ),
        })

    history = _n(result.get("hazard_history_count"))
    if history >= HAZARD_HISTORY_THRESHOLD:
        out.append({
            "label": "Recurrent hazard",
            "text": (
                f"{history:,} incidents of this disaster type are recorded here; "
                "the exposure estimate was adjusted upward as a planning assumption."
            ),
        })

    return out


def build_view(result):
    """Build the whole view-model for one engine result.

    Tolerates a partial/missing result (a legacy saved snapshot, say): any absent
    key simply yields an empty section rather than raising.
    """
    result = result or {}
    gaps = result.get("gaps") or {}
    status_by_class = result.get("status_by_class") or {}

    rows = [
        _row(c, gaps[c], status_by_class.get(c, "Critical"))
        for c in DISPLAY_ORDER if c in gaps
    ]
    rows_by_key = {r["key"]: r for r in rows}

    dimensions = [d for d in (_dimension(dim, rows_by_key) for dim in DIMENSIONS) if d]

    concerns = sorted(
        [r for r in rows if r["gap"] > 0],
        key=lambda r: (_STATUS_RANK.get(r["status"], 0), r["pct"], -r["gap"]),
    )

    affected = _n(result.get("estimated_affected"))
    vulnerable = _n(result.get("estimated_vulnerable"))
    total_pop = _n((result.get("inputs") or {}).get("total_population"))
    veh = rows_by_key.get("vehicles")

    overall = result.get("overall_readiness") or "Critical"
    band = BAND_UI.get(overall, BAND_UI["Critical"])

    return {
        "rows": rows,
        "concerns": concerns[:TOP_CONCERNS],
        "more_concerns": concerns[TOP_CONCERNS:],
        "dimensions": dimensions,
        "consequences": _consequences(result, rows, rows_by_key),
        "overall": {
            "status": overall,
            "label": band["label"],
            "text_class": band["text"],
            "accent": band["accent"],
            "hero": band["hero"],
            "icon": band["icon"],
            "sentence": _overall_sentence(dimensions, overall),
        },
        "figures": {
            "affected": affected,
            "total_population": total_pop,
            "vulnerable": vulnerable,
            # Share of the AFFECTED population, matching the previous page.
            "vulnerable_pct": int(round(vulnerable / affected * 100)) if affected else 0,
            "critical_count": sum(1 for r in rows if r["status"] == "Critical"),
            "vehicles_available": veh["available"] if veh else 0,
            "vehicles_needed": veh["need"] if veh else 0,
        },
    }
