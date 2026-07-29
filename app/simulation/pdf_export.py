"""Saved-scenario PDF export.

Pure-Python via fpdf2 — NO system binaries, NO external font files, so it
deploys cleanly to Render (unlike WeasyPrint/wkhtmltopdf). Builds entirely from
a stored snapshot dict; it never recomputes anything and never calls Groq.

Core fonts (Helvetica) are latin-1 only, so `_ascii()` folds the unicode
punctuation our data contains (em dash, middot, ≥, °, →, ₱) down to safe ASCII
rather than shipping a TTF.
"""

import re

from fpdf import FPDF
from fpdf.enums import XPos, YPos

# Same class display order / labels as the on-screen results partial.
DISPLAY_ORDER = ["water", "food", "blankets", "evac_capacity", "medicine", "vehicles"]
CLASS_LABEL = {
    "water": "Water",
    "food": "Food Packs",
    "blankets": "Blankets / NFI Kits",
    "evac_capacity": "Evacuation Capacity",
    "medicine": "Medicine Kits",
    "vehicles": "Response Vehicles",
}
STATUS_LABEL = {"Adequate": "Sufficient", "Partial": "At Risk", "Critical": "Critical"}
READINESS_LABEL = {"Adequate": "Low Priority", "Partial": "Moderate Priority",
                   "Critical": "High Priority"}

_REPLACEMENTS = {
    "—": "-", "–": "-", "−": "-",   # em / en dash, minus
    "·": "-", "•": "-",                    # middot, bullet
    "≥": ">=", "≤": "<=",                  # >= / <=
    "→": "->", "←": "<-",                   # arrows
    "₱": "PHP ", "°": " deg",              # peso, degree
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "…": "...", " ": " ",
}


def _ascii(s) -> str:
    """Fold unicode punctuation to latin-1-safe ASCII for the core fonts."""
    s = "" if s is None else str(s)
    for k, v in _REPLACEMENTS.items():
        s = s.replace(k, v)
    return s.encode("latin-1", "replace").decode("latin-1")


def _strip_html(s) -> str:
    """Turn the sanitized AI-briefing HTML fragment into plain text: drop tags,
    keep list items / paragraphs as line breaks."""
    if not s:
        return ""
    s = re.sub(r"(?i)</(p|div|li|h[1-6])>", "\n", s)
    s = re.sub(r"(?i)<li[^>]*>", "- ", s)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = (s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
          .replace("&nbsp;", " ").replace("&#39;", "'").replace("&quot;", '"'))
    lines = [ln.strip() for ln in s.splitlines()]
    return "\n".join(ln for ln in lines if ln)


class _ScenarioPDF(FPDF):
    def __init__(self, footer_note: str):
        super().__init__(orientation="P", unit="mm", format="A4")
        self._footer_note = _ascii(footer_note)
        self.set_auto_page_break(auto=True, margin=20)

    def footer(self):
        # Called automatically on EVERY page.
        self.set_y(-15)
        self.set_font("Helvetica", "I", 7)
        self.set_text_color(120, 120, 120)
        self.multi_cell(0, 3.5, self._footer_note, align="C")


def build_scenario_pdf(scenario, result: dict, created_at_str: str, saved_by: str,
                       ai_actions=None) -> bytes:
    """Render the stored snapshot to PDF bytes. `scenario` is the SavedScenario
    row (for metadata); `result` is json.loads(result_json); `ai_actions` is the
    already-validated suggested-actions list (empty/None for older snapshots)."""
    inputs = result.get("inputs", {})
    footer_note = (
        "Advisory estimates only - validate with current conditions and "
        f"qualified CDRRMO personnel. Snapshot from {created_at_str}."
    )
    pdf = _ScenarioPDF(footer_note)
    pdf.set_title(_ascii(f"RisKonek Scenario - {scenario.name}"))
    pdf.add_page()

    # ── Title ────────────────────────────────────────────────────────────
    pdf.set_font("Helvetica", "B", 15)
    pdf.set_text_color(20, 20, 20)
    pdf.multi_cell(0, 8, _ascii(scenario.name), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(90, 90, 90)
    pdf.cell(0, 5, "RisKonek Disaster Simulation - saved scenario snapshot", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(2)

    # ── Metadata block ───────────────────────────────────────────────────
    meta = [
        ("Barangay", inputs.get("barangay_name", scenario.barangay_name)),
        ("Disaster Type", str(inputs.get("disaster_type", scenario.disaster_type)).title()),
        ("Planning Horizon",
         f"{result.get('duration_label', scenario.duration)} "
         f"({inputs.get('horizon_days', scenario.horizon_days)} days)"),
        ("Saved By", saved_by),
        ("Saved On", created_at_str),
    ]
    pdf.set_draw_color(220, 220, 220)
    for label, value in meta:
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_text_color(70, 70, 70)
        pdf.cell(38, 6, _ascii(label))
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(30, 30, 30)
        pdf.cell(0, 6, _ascii(value), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(2)

    # ── Readiness summary ────────────────────────────────────────────────
    readiness = result.get("overall_readiness", "Critical")
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_text_color(20, 20, 20)
    pdf.cell(0, 7, "Overall Readiness: "
             + _ascii(READINESS_LABEL.get(readiness, readiness)), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(70, 70, 70)
    pdf.cell(0, 5,
             f"Estimated affected: {result.get('estimated_affected', 0):,}"
             f"  |  Vulnerable: {result.get('estimated_vulnerable', 0):,}"
             f"  |  Population: {inputs.get('total_population', 0):,}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    if not result.get("hazard_recorded", True) and result.get("hazard_note"):
        pdf.set_text_color(150, 90, 0)
        pdf.multi_cell(0, 5, _ascii("Note: " + result["hazard_note"]),
                       new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(3)

    # ── Resource analysis table ──────────────────────────────────────────
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_text_color(20, 20, 20)
    pdf.cell(0, 7, "Resource Analysis", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    gaps = result.get("gaps", {})
    status_by_class = result.get("status_by_class", {})
    headers = ["Resource Class", "Required", "Available", "Gap/Surplus", "Basis", "Status"]
    widths = [50, 24, 24, 28, 26, 28]

    def _row(cells, fill, header=False):
        pdf.set_font("Helvetica", "B" if header else "", 8)
        if header:
            pdf.set_fill_color(240, 240, 240)
            pdf.set_text_color(40, 40, 40)
        else:
            pdf.set_fill_color(250, 250, 250)
            pdf.set_text_color(40, 40, 40)
        for w, (text, align) in zip(widths, cells):
            pdf.cell(w, 6, _ascii(text), border=1, align=align, fill=fill)
        pdf.ln(6)

    _row([(h, "L" if i == 0 else "R") for i, h in enumerate(headers)],
         fill=True, header=True)
    for c in DISPLAY_ORDER:
        if c not in gaps:
            continue
        g = gaps[c]
        gap = g.get("gap", 0)
        if gap > 0:
            gap_txt = f"-{gap:,}"
        else:
            gap_txt = f"+{g.get('available', 0) - g.get('need', 0):,}"
        basis = "Standard" if g.get("basis") == "standard" else "Assumption"
        status = STATUS_LABEL.get(status_by_class.get(c), status_by_class.get(c, "-"))
        _row([
            (f"{CLASS_LABEL.get(c, c)} ({g.get('unit', '')})", "L"),
            (f"{g.get('need', 0):,}", "R"),
            (f"{g.get('available', 0):,}", "R"),
            (gap_txt, "R"),
            (basis, "C"),
            (status, "C"),
        ], fill=True)
    pdf.set_font("Helvetica", "I", 7)
    pdf.set_text_color(110, 110, 110)
    pdf.multi_cell(0, 4,
                   "Standard figures follow Sphere / DSWD guidance; Assumption "
                   "figures are adjustable CDRRMO planning targets, not "
                   "standard-based requirements.",
                   new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(2)

    # ── Operational risks ────────────────────────────────────────────────
    risks = result.get("operational_risks") or []
    if risks:
        pdf.set_font("Helvetica", "B", 11)
        pdf.set_text_color(20, 20, 20)
        pdf.cell(0, 7, "Operational Risks", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(50, 50, 50)
        for r in risks:
            pdf.multi_cell(0, 5, _ascii("- " + str(r)),
                           new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(2)

    # ── AI briefing (only if one was saved) ──────────────────────────────
    briefing = _strip_html(scenario.ai_briefing)
    if briefing:
        pdf.set_font("Helvetica", "B", 11)
        pdf.set_text_color(20, 20, 20)
        pdf.cell(0, 7, "AI Generated Planning Summary", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_font("Helvetica", "I", 7)
        pdf.set_text_color(120, 120, 120)
        pdf.multi_cell(0, 4, "AI-generated - advisory - validate with current "
                             "conditions and expert judgment.",
                       new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(50, 50, 50)
        pdf.multi_cell(0, 5, _ascii(briefing), new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    # ── Suggested planning actions (only when a briefing was saved) ───────
    # The disclaimer is written here, never by the model — so it prints even
    # when the snapshot holds no actions.
    if briefing:
        pdf.ln(2)
        pdf.set_font("Helvetica", "B", 11)
        pdf.set_text_color(20, 20, 20)
        pdf.cell(0, 7, "Suggested Planning Actions (AI-Assisted)",
                 new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_font("Helvetica", "I", 7)
        pdf.set_text_color(120, 120, 120)
        pdf.multi_cell(0, 4, "For planning purposes only. Subject to CDRRMO validation.",
                       new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(50, 50, 50)
        if ai_actions:
            for i, a in enumerate(ai_actions, start=1):
                pdf.multi_cell(0, 5, _ascii(f"{i}. {a.get('action', '')}"),
                               new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                if a.get("basis"):
                    pdf.set_font("Helvetica", "I", 8)
                    pdf.set_text_color(110, 110, 110)
                    pdf.multi_cell(0, 4, _ascii("   Basis: " + a["basis"]),
                                   new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                    pdf.set_font("Helvetica", "", 9)
                    pdf.set_text_color(50, 50, 50)
        else:
            pdf.multi_cell(0, 5, "No additional AI-assisted planning actions were "
                                 "identified from the available data.",
                           new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    out = pdf.output()
    return bytes(out)
